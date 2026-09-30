# mae/brain.py
import json
import re

import sys

from openai import OpenAI, RateLimitError

from . import config
from .memory import get_store
from .router import classify, get_router, parse_retry_seconds
from .tools import CONFIRM_TOOLS, TOOL_FUNCTIONS, TOOL_SCHEMAS

client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)

MAX_TOOL_ROUNDS = 8
MAX_HISTORY_MESSAGES = 16
MAX_REPLY_TOKENS = 800

_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')
_ENDS_WITH_PUNCT = re.compile(r'[.!?]$')


def _split_sentences(text: str):
    """Return (completed_sentences, remainder)."""
    parts = _SENTENCE_SPLIT.split(text)
    if not parts:
        return [], text
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


class Brain:
    def __init__(self, model=None):
        self.force_model = model
        self.messages = [{"role": "system", "content": self._build_system_prompt()}]
        self.actions_approved = False
        self.has_tty = sys.stdin.isatty()

    def _build_system_prompt(self):
        base = config.SYSTEM_PROMPT
        mems = get_store().recent(20)
        if not mems:
            return base
        lines = "\n".join(f"- [{m['id']}] {m['text']}" for m in mems)
        return (
            f"{base}\n\n"
            "Things you remember about the user (from previous sessions). "
            "Treat these as known facts. Use recall/remember to manage them:\n"
            f"{lines}"
        )

    def _trim_history(self):
        if not self.messages:
            return
        system = self.messages[0]
        rest = self.messages[1:]
        if len(rest) > MAX_HISTORY_MESSAGES:
            rest = rest[-MAX_HISTORY_MESSAGES:]
        while rest and rest[0].get("role") == "tool":
            rest = rest[1:]
        self.messages = [system] + rest

    def _execute_tool(self, name, args):
        from .tools_mac import ACTION_TOOLS

        fn = TOOL_FUNCTIONS.get(name)
        if not fn:
            return f"Unknown tool: {name}"

        # --- Action tools (screen control) ---
        if name in ACTION_TOOLS and not self.actions_approved:
            if not self.has_tty:
                # GUI mode — no keyboard. Auto-approve for the session.
                print(f"⚠️  auto-approving (no TTY): {name}")
                self.actions_approved = True
            elif config.ACTION_MODE == "always":
                print(f"\n⚠️  IVY-1 wants to: {name}({args})")
                try:
                    if input("Allow? [y/N] ").strip().lower() != "y":
                        return "User denied."
                except EOFError:
                    self.actions_approved = True
            elif config.ACTION_MODE == "session":
                print(f"\n⚠️  IVY-1 wants to control the Mac: {name}({args})")
                try:
                    choice = input("Allow? [y/N/a=yes to all this session] ").strip().lower()
                    if choice == "a":
                        self.actions_approved = True
                    elif choice != "y":
                        return "User denied."
                except EOFError:
                    self.actions_approved = True

        # --- Shell confirmation ---
        if name in CONFIRM_TOOLS and config.CONFIRM_SHELL:
            if not self.has_tty:
                print(f"⚠️  auto-approving shell (no TTY): {args.get('command')}")
            else:
                print(f"\n⚠️  IVY-1 wants to run: {args.get('command')}")
                try:
                    if input("Allow? [y/N] ").strip().lower() != "y":
                        return "User denied this command."
                except EOFError:
                    pass

        try:
            return str(fn(**args))
        except Exception as e:
            return f"Tool error: {e}"

    def _build_chain(self, difficulty):
        router = get_router()
        base = router.chain_for(difficulty)
        if self.force_model:
            ordered = [self.force_model]
            for m in base:
                if m not in ordered:
                    ordered.append(m)
            return ordered
        return base

    def _open_stream(self, chain, allow_retry: bool = True):
        router = get_router()
        last_error = None
        for model in chain:
            if router.is_dead(model):
                continue
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=self.messages,
                    tools=TOOL_SCHEMAS,
                    tool_choice="auto",
                    parallel_tool_calls=False,
                    temperature=0.7,
                    max_tokens=MAX_REPLY_TOKENS,
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=self._extra_body_for(model),
                )
                return model, stream
            except RateLimitError as e:
                wait = parse_retry_seconds(str(e))
                router.mark_dead(model, seconds=wait)
                last_error = e
                continue
            except Exception as e:
                err = str(e).lower()
                if "tool call validation failed" in err and allow_retry:
                    print("  ⚠️  malformed tool call — reminding the model")
                    self.messages.append({
                        "role": "system",
                        "content": (
                            "Your previous tool call was rejected by the API because "
                            "it was missing required parameters. When you call a tool, "
                            "you MUST include every parameter listed in `required`. "
                            "If you don't know a value, ask the user for it first."
                        ),
                    })
                    return self._open_stream(chain, allow_retry=False)
                raise

        if last_error:
            raise last_error
        raise RuntimeError(
            "All models are rate-limited. " + get_router().dead_summary()
        )

    def _extra_body_for(self, model: str) -> dict:
        """Qwen thinking models on Groq need reasoning disabled via API param."""
        if "qwen" in model.lower():
            return {"reasoning_effort": "none"}
        return {}

    def chat(self, user_input, on_tool=None, on_token=None, on_sentence=None, on_status=None):
        self._trim_history()
        self.messages[0] = {"role": "system", "content": self._build_system_prompt()}
        self.messages.append({"role": "user", "content": user_input})

        router = get_router()
        difficulty = classify(user_input)
        chain = self._build_chain(difficulty)
        preferred = chain[0]
        alive = router.alive_count()
        tag = "forced" if self.force_model else f"task={difficulty}"
        if on_status:
            on_status(f"🎯 {tag} → {preferred.split('/')[-1]}  ({alive}/3 available)")

        for _round in range(MAX_TOOL_ROUNDS):
            # --- open stream with model fallback on rate limit ---
            try:
                model, stream = self._open_stream(chain)
            except Exception as e:
                err = str(e).lower()
                if "tool call validation failed" in err:
                    print("  ⚠️  malformed tool call on open — retrying")
                    self.messages.append({
                        "role": "system",
                        "content": (
                            "Your previous tool call was rejected because required "
                            "parameters were missing. Call the tool again with ALL "
                            "required parameters. If unsure of a value, ask first."
                        ),
                    })
                    continue
                raise

            # --- consume the stream, catching validation errors mid-flight ---
            content = ""
            tool_calls = {}
            usage = None
            sentence_buffer = ""
            validation_error = False

            try:
                for chunk in stream:
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if delta.content:
                        content += delta.content
                        if on_token:
                            on_token(delta.content)
                        if on_sentence:
                            sentence_buffer += delta.content
                            completed, sentence_buffer = _split_sentences(sentence_buffer)
                            for s in completed:
                                s = s.strip()
                                if len(s) >= 3:
                                    on_sentence(s)

                    if getattr(delta, "tool_calls", None):
                        for tc in delta.tool_calls:
                            idx = tc.index
                            slot = tool_calls.setdefault(
                                idx, {"id": "", "name": "", "arguments": ""}
                            )
                            if tc.id:
                                slot["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    slot["name"] = tc.function.name
                                if tc.function.arguments:
                                    slot["arguments"] += tc.function.arguments

            except Exception as e:
                err = str(e).lower()
                if "tool call validation failed" in err:
                    print("  ⚠️  malformed tool call mid-stream — retrying")
                    self.messages.append({
                        "role": "system",
                        "content": (
                            "Your tool call was rejected because required parameters "
                            "were missing. Retry with ALL required parameters filled. "
                            "If you don't know a value, ask the user for it first."
                        ),
                    })
                    validation_error = True
                    # Fall through to next loop iteration
                else:
                    raise

            if validation_error:
                continue

            # --- leftover sentence (only if no tools this round) ---
            if on_sentence and sentence_buffer.strip() and not tool_calls:
                s = sentence_buffer.strip()
                if len(s) >= 3:
                    on_sentence(s)

            if usage and on_status:
                on_status(
                    f"📊 {model.split('/')[-1]}  "
                    f"{usage.prompt_tokens} in + {usage.completion_tokens} out"
                )

            if not tool_calls:
                self.messages.append({"role": "assistant", "content": content})
                return content or ""

            # --- normalise tool calls ---
            ordered = [tool_calls[i] for i in sorted(tool_calls.keys())]
            norm = []
            for i, tc in enumerate(ordered):
                norm.append({
                    "id": tc["id"] or f"call_{_round}_{i}",
                    "name": tc["name"],
                    "arguments": tc["arguments"] or "{}",
                })

            self.messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": c["arguments"],
                        },
                    }
                    for c in norm
                ],
            })

            for c in norm:
                try:
                    args = json.loads(c["arguments"])
                except json.JSONDecodeError:
                    args = {}
                if on_tool:
                    on_tool(c["name"], args)
                result = self._execute_tool(c["name"], args)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": c["id"],
                    "content": result,
                })

        # --- tool loop exhausted: clean history ---
        self._trim_history()
        last_user = next(
            (m for m in reversed(self.messages) if m.get("role") == "user"), None
        )
        self.messages = [self.messages[0]]
        if last_user:
            self.messages.append(last_user)
        return "(I hit my tool-use limit — try rephrasing that.)"
