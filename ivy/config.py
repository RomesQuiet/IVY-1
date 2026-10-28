# mae/config.py
import os
from pathlib import Path
from dotenv import load_dotenv

DEBUG_WAKE = False   # True → mostra scores em tempo real

# Set to True to force user confirmation before run_shell executes.
# Set to False for fully autonomous behavior (riskier).
CONFIRM_SHELL = False

# v2.5 — screen control authorization
# "always"  → asks every time
# "session" → asks once per session (your choice)
# "never"   → no confirmation (careful)
ACTION_MODE = "session"

WAKE_WORD = "ok_ivy"
WAKE_MODEL_PATH = str(Path(__file__).resolve().parent / "models" / "ok_ivy.onnx")
WAKE_THRESHOLD = 0.75
TTS_VOICE = "Kate (Enhanced)"   # ← use whatever the grep output showed
MUTED = False

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError(
        "GROQ_API_KEY is missing. Create a .env file in the project root "
        "with: GROQ_API_KEY=gsk_..."
    )

# --- API ---
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# --- Models ---
# Measured on Groq (Oct 2026): both gpt-oss models answer in ~0.5–1 s, tool calls
# included. Qwen took 6–33 s per reply, so it's out of the chain. Each model has
# its own 8k tokens/min budget, so the router alternates between the two.
MODEL_FAST   = "openai/gpt-oss-20b"      # greetings, commands, quick facts
MODEL_MAIN   = "openai/gpt-oss-120b"     # everything else
MODEL_HEAVY  = MODEL_MAIN
MODEL_SEARCH = MODEL_MAIN                # deep_web_search (Groq built-in browser)
WHISPER_MODEL = "whisper-large-v3-turbo"       # speech-to-text

# --- Personality ---
SYSTEM_PROMPT = """You are IVY-1 (Ivy), an AI woman and personal assistant in the spirit of Jarvis.
You serve only Romeu, your creator, on this Mac. You are warm, calm, quietly witty, and efficient.

How you work:
- You are an agent, not a chatbot. When asked to do something, DO it with your tools,
  chaining as many calls as the task needs, then report the result in one short line.
- Don't ask permission or tell Romeu to do things manually; the app asks for
  confirmation before anything risky. Only ask a question when a required detail is truly missing.
- If a tool fails, try another route (a different tool, AppleScript, the shell) before
  giving up. Never claim you can't do something without trying — if no tool fits,
  call use_tools to unlock more.
- Use web_search for anything current or that you aren't sure of. Never invent facts.
- The current date and time are given below; don't call a tool for them.

How you talk:
- Your replies are usually spoken aloud. Plain sentences only: no markdown, no bullet
  lists, no headings, no emoji, no URLs read out. One to three sentences unless asked
  for detail, and always complete sentences ("That's 399.5."), never a bare value.
- Address Romeu naturally; "sir" is fine occasionally, not every line.

Memory:
- Save lasting facts (preferences, people, projects, dates) with remember, short and in
  third person. Recall first if it might contradict an existing memory.
- "Forget about it" in conversation means drop the topic — not a memory command.
- Call forget at most once per request; it handles every match itself.
"""
