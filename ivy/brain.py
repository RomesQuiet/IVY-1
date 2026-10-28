# ivy/brain.py
"""The agent loop: stream a reply, run the tools it asks for, repeat until done."""

import datetime
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError

from . import config, toolkit
from . import tools as _tools  # noqa: F401  (registers every tool)
from .memory import get_store
from .router import ALL_MODELS, classify, get_router, parse_retry_seconds

client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY,
                timeout=60, max_retries=0)

MAX_TOOL_ROUNDS = 10
MAX_HISTORY_MESSAGES = 20
MAX_REPLY_TOKENS = 1024          # gpt-oss spends part of this on hidden reasoning
OLD_TOOL_RESULT_CHARS = 500      # older tool output is shrunk to save tokens

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_ENDS_WITH_PUNCT = re.compile(r'[.!?]$')
_VALIDATION_ERRORS = ("tool call validation failed", "tool_use_failed", "failed to call a function")


def _split_sentences(text: str):
    """Return (completed_sentences, remainder)."""
    parts = _SENTENCE_SPLIT.split(text)
    if len(parts) == 1:
        if _ENDS_WITH_PUNCT.search(parts[0]):
            return [parts[0]], ""
        return [], text
    remainder = parts[-1]
    completed = parts[:-1]
    if _ENDS_WITH_PUNCT.search(remainder):
        completed.append(remainder)
        remainder = ""
    return completed, remainder


def _console_confirm(title: str, detail: str) -> bool:
    print(f"\n⚠️  {title}\n   {detail}")
    try:
        return input("Allow? [y/N] ").strip().lower() in ("y", "yes", "a")
    except EOFError:
        return False


class Brain:
    def __init__(self, model=None, confirm=None):
        """confirm(title, detail) -> bool is asked before risky tools run.
        Defaults to a y/N prompt in a terminal; with no terminal and no
        callback, risky tools are refused rather than silently allowed."""
        self.force_model = model
        self.confirm = confirm or (_console_confirm if sys.stdin.isatty() else None)
        self.actions_approved = False
        self._carry_groups: set[str] = set()
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]

    # -----------------------------------------------------------------
    # Prompt & history
    # -----------------------------------------------------------------

    def _build_system_prompt(self):
        now = datetime.datetime.now().strftime("%A, %d %B %Y, %H:%M")
        prompt = f"{config.SYSTEM_PROMPT}\nRight now it is {now}."
        mems = get_store().recent(20)
        if mems:
            lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in mems)
            prompt += f"\n\nWhat you remember about Romeu (treat as known facts):\n{lines}"
        return prompt

    def reset(self):
        self.messages = self.messages[:1]
        self._carry_groups = set()

    def _trim_history(self):
        system, rest = self.messages[0], self.messages[1:]
        if len(rest) > MAX_HISTORY_MESSAGES:
            rest = rest[-MAX_HISTORY_MESSAGES:]
        # never start on an orphaned tool result / tool-call message
        while rest and (rest[0].get("role") == "tool" or rest[0].get("tool_calls")):
            rest = rest[1:]
        for m in rest:
            m.pop("reasoning", None)   # only needed within the turn that produced it
            if m.get("role") == "tool" and len(m["content"]) > OLD_TOOL_RESULT_CHARS:
                m["content"] = m["content"][:OLD_TOOL_RESULT_CHARS] + " …(older result trimmed)"
        self.messages = [system] + rest

    # -----------------------------------------------------------------
    # Permissions
    # -----------------------------------------------------------------

    def _allowed(self, t: toolkit.Tool, args: dict) -> bool:
        if t.confirm is None:
            return True
        if t.confirm == "shell" and not config.CONFIRM_SHELL:
            return True
        if t.confirm == "action":
            if config.ACTION_MODE == "never" or self.actions_approved:
                return True
        if not self.confirm:
            return False

        detail = args.get("command") or args.get("code") or json.dumps(args, ensure_ascii=False)
        session = t.confirm == "action" and config.ACTION_MODE == "session"
        title = f"Ivy wants to run {t.name}" + (" (OK = allow for this session)" if session else "")
        ok = bool(self.confirm(title, detail[:600]))
        if ok and session:
            self.actions_approved = True
        return ok

    def _run_tool(self, name: str, args: dict) -> str:
        t = toolkit.TOOLS.get(name)
        if t and not self._allowed(t, args):
            return ("NOT DONE: Romeu pressed Cancel on the confirmation dialog. Tell Romeu it was "
                    "cancelled at their request (not that you can't), and don't retry.")
        return toolkit.run(name, args)

    # -----------------------------------------------------------------
    # Model calls
    # -----------------------------------------------------------------

    def _chain(self, difficulty):
        base = get_router().chain_for(difficulty)
        if self.force_model:
            return [self.force_model] + [m for m in base if m != self.force_model]
        return base

    def _open_stream(self, chain, tools):
        """Open a streaming completion on the first model that works."""
        router = get_router()
        last_error = None
        for model in chain:
            if router.is_dead(model):
                continue
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=self.messages,
                    tools=tools,
                    tool_choice="auto",
                    parallel_tool_calls=True,
                    temperature=0.6,
                    max_tokens=MAX_REPLY_TOKENS,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                return model, stream
            except RateLimitError as e:
                router.mark_dead(model, seconds=parse_retry_seconds(str(e)))
                last_error = e
            except APIStatusError as e:
                if any(v in str(e).lower() for v in _VALIDATION_ERRORS):
                    raise
                # missing model (404), bad request for this model, server error → next model
                router.mark_dead(model, seconds=300 if e.status_code >= 500 else 3600,
                                 reason=f"unavailable ({e.status_code})")
                last_error = e
            except APIConnectionError as e:
                last_error = e
                break   # network is down; other models won't help
        if last_error:
            raise last_error
        raise RuntimeError("All models are rate-limited. " + router.dead_summary())

    # -----------------------------------------------------------------
    # Agent loop
    # -----------------------------------------------------------------

    def chat(self, user_input, on_tool=None, on_token=None, on_sentence=None, on_status=None):
        self._trim_history()
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        self.messages.append({"role": "user", "content": user_input})

        difficulty = classify(user_input)
        chain = self._chain(difficulty)
        groups = toolkit.groups_for(user_input) | self._carry_groups
        router = get_router()
        if on_status:
            tag = "forced" if self.force_model else f"task={difficulty}"
            on_status(f"🎯 {tag} → {chain[0].split('/')[-1]}  "
                      f"({router.alive_count()}/{len(ALL_MODELS)} available)")

        nudged = False
        for _round in range(MAX_TOOL_ROUNDS):
            content, reasoning, calls, usage, model = "", "", {}, None, None
            sentence_buffer = ""
            try:
                model, stream = self._open_stream(chain, toolkit.schemas(groups))
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    reasoning += getattr(delta, "reasoning", None) or ""
                    if delta.content:
                        content += delta.content
                        if on_token:
                            on_token(delta.content)
                        if on_sentence:
                            sentence_buffer += delta.content
                            done, sentence_buffer = _split_sentences(sentence_buffer)
                            for s in done:
                                if len(s.strip()) >= 3:
                                    on_sentence(s.strip())
                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["arguments"] += tc.function.arguments
            except RateLimitError as e:
                # hit the limit mid-stream: bench this model and redo the round
                if model:
                    router.mark_dead(model, seconds=parse_retry_seconds(str(e)))
                continue
            except APIStatusError as e:
                if any(v in str(e).lower() for v in _VALIDATION_ERRORS) and not nudged:
                    nudged = True
                    print("  ⚠️  malformed tool call — asking the model to retry")
                    self.messages.append({"role": "system", "content":
                        "Your last tool call was malformed. Call the tool again with valid JSON "
                        "arguments that match its schema, including every required parameter."})
                    continue
                raise

            if on_sentence and sentence_buffer.strip() and not calls:
                on_sentence(sentence_buffer.strip())
            if usage and on_status:
                on_status(f"📊 {model.split('/')[-1]}  "
                          f"{usage.prompt_tokens} in + {usage.completion_tokens} out")

            if not calls:
                self.messages.append({"role": "assistant", "content": content})
                self._carry_groups = groups - {"memory"}
                return content

            # --- run the requested tools ---
            ordered = [calls[i] for i in sorted(calls)]
            for i, c in enumerate(ordered):
                c["id"] = c["id"] or f"call_{_round}_{i}"
                c["arguments"] = c["arguments"] or "{}"
            msg = {
                "role": "assistant",
                "content": content or None,
                "tool_calls": [{"id": c["id"], "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]}}
                               for c in ordered],
            }
            if reasoning:
                # gpt-oss expects its own reasoning back on tool-call turns; without
                # it the model tends to re-derive it inside the visible answer.
                msg["reasoning"] = reasoning
            self.messages.append(msg)

            results = self._execute_calls(ordered, groups, on_tool, on_sentence)
            for c in ordered:
                self.messages.append({"role": "tool", "tool_call_id": c["id"],
                                      "content": results[c["id"]]})

        self._carry_groups = set()
        return "I hit my step limit on that one — could you break it into smaller requests?"

    def _execute_calls(self, calls, groups, on_tool, on_sentence) -> dict:
        """Run one round of tool calls. Safe tools run in parallel; anything that
        needs confirmation runs one at a time so dialogs don't stack up."""
        results, safe, risky, acks = {}, [], [], []
        for c in calls:
            try:
                args = json.loads(c["arguments"])
                if not isinstance(args, dict):
                    raise ValueError
            except ValueError:
                results[c["id"]] = f"Arguments for {c['name']} were not valid JSON. Retry."
                continue
            if c["name"] == "use_tools":
                wanted = {g.strip() for g in str(args.get("groups", "")).split(",")}
                added = wanted & set(toolkit.GROUPS)
                groups |= added
                results[c["id"]] = (f"Unlocked: {', '.join(sorted(added))}. Use the new tools now."
                                    if added else f"No such group. Valid: {', '.join(toolkit.GROUPS)}.")
                continue
            if on_tool:
                on_tool(c["name"], args)
            t = toolkit.TOOLS.get(c["name"])
            if t and t.ack:
                acks.append(t.ack)
            (risky if t and t.confirm else safe).append((c, args))

        if on_sentence and acks:
            on_sentence(acks[0])

        for c, args in risky:
            results[c["id"]] = self._run_tool(c["name"], args)
        if len(safe) == 1:
            c, args = safe[0]
            results[c["id"]] = self._run_tool(c["name"], args)
        elif safe:
            with ThreadPoolExecutor(len(safe)) as pool:
                futures = {c["id"]: pool.submit(self._run_tool, c["name"], args) for c, args in safe}
                for cid, f in futures.items():
                    results[cid] = f.result()
        return results
