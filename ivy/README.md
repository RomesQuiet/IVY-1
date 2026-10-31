# Ivy

A voice-and-text assistant for macOS that works like a coding agent on your whole Mac:
it looks, acts, then checks what actually happened.

```
you ──▶ intents.py ──(instant: open, play, volume, timers, weather, time)──▶ done in ms, 0 tokens
          │ not a simple command
          ▼
        senses.py  live reading of the Mac: front app, window, tab, music, volume
          ▼
        brain.py  agent loop ──▶ router.py picks the model with token budget left
          │                       (waits out short limits, shrinks oversized requests)
          │  every action comes back with "[Observed: what actually changed]"
          ▼
        toolkit.py  ~70 tools: core set always sent, groups unlocked by keywords
          │          or by the model itself (use_tools)
          ├─ tools.py        web, weather, apps, browser, shell, Python, files, packages, memory
          ├─ tools_media.py  YouTube, Spotify / Apple Music
          ├─ tools_life.py   Messages, Contacts, Mail, Calendar, Reminders, Notes, system switches
          ├─ tools_mac.py    AppleScript, UI clicks, keyboard, browser JavaScript
          ├─ skills/*.py     abilities Ivy wrote herself (create_skill) or you added
          └─ mcp_client.py   tools from any MCP server in mcp_servers.json
```

## Run

```bash
source .venv/bin/activate && python ivy_gui.py     # window
python ivy.py                                     # terminal
```

Esc or the ■ button stops a request. 💤 turns on "OK Ivy" wake-word mode.
`/doctor` shows what she can see and do right now, and tests it live.

## Always there

- **⌥Space** shows or hides Ivy from any app (`HOTKEY` in `config.py`). It needs no permission.
- She lives in the menu bar. The red window button hides her; **Quit Ivy** in the menu exits.
- **Settings** (⚙, ⌘, or the menu bar): neutral themes, plus sliders for the accent and
  background colours. Saved in `settings.json`.
- She speaks up on her own (`ALERTS` in `config.py`): low battery, calendar events, finished
  downloads, low disk space. Alerts stay silent during quiet hours and while you're talking.
  Recurring calendar events are missed, because Calendar's scripting doesn't expand them.
- Ask "tell me when…" and she writes a background rule as a skill. See the top of `ivy/watcher.py`.

## "This"

Say "summarise this page", "explain this", "translate this" or "reply to this email" and Ivy
reads what's in front: selected text, the email selected in Mail, the browser page, files
selected in Finder, or the window's text. Reading logged-in pages needs "Allow JavaScript
from Apple Events" in the browser (Brave: View → Developer); otherwise she downloads the
page without your login. Email replies stay as drafts unless you ask her to send.

## Giving Ivy eyes and hands

macOS blocks both until you allow them, for whichever app launches her (Terminal, Python
or Ivy.app). Ivy offers a **Fix this** button when they're off; restart her afterwards.

| Permission | What it enables |
|---|---|
| Screen Recording | window titles, `read_screen`, `see_screen`, `click_text` |
| Accessibility | clicking, typing, key presses |

- `read_screen` uses Apple's on-device OCR. Nothing leaves the Mac.
- `see_screen` uploads a screenshot to Groq so a vision model can look at it. It asks once
  per session. Set `CLOUD_VISION = "off"` in `config.py` to disable it.

## MCP servers

Create `mcp_servers.json` in the project root, same format as Claude Desktop (local/stdio
servers). Each server's tools load on demand and ask before acting unless the server has
`"trusted": true`. See the top of `ivy/mcp_client.py`.

## Tests

```bash
python -m tests.run            # offline: free, can't touch your Mac
python -m tests.run --live     # also asks the real model (uses Groq quota)
python -m tests.run --gui      # also drives the real app (a window flashes)
```

## Keys (`.env`)

| Key | Needed | What it adds |
|---|---|---|
| `GROQ_API_KEY` | yes | the models (free tier: 8k tokens/minute per model) |
| `CEREBRAS_API_KEY` | optional | much more room before Ivy has to wait (free at cloud.cerebras.ai) |
| `OPENROUTER_API_KEY` | optional | another free fallback |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | optional | exact song search on Spotify (free at developer.spotify.com); without it Ivy finds artists and albums |

## Safety

Tools that change things (AppleScript, UI control, messages, email, events, installs,
power, new skills) ask first — once per session by default (`ACTION_MODE` in
`config.py`). `run_shell` / `run_python` ask only if `CONFIRM_SHELL = True`.

Web pages, screens and emails can contain text aimed at Ivy, and in testing the model did
try to obey it. So once a request has read outside content, shell, Python, AppleScript,
messages and other powerful tools ask every time, whatever the settings above say. Clicks
and typing are exempt so screen work isn't one dialog per click.

## Teaching Ivy

Correct her ("no, I meant Safari", "from now on use metric") and she keeps lasting lessons
as memories, tells you in the chat, and forgets one if you say "forget that". Only your own
words are studied, never web pages or the screen.

Ask her: *"learn a skill that …"*. She writes `skills/<name>.py`, it's validated by
importing it, and the new tool is usable immediately and after restarts. You can
write skills by hand too — see `skills/crypto_prices.py`.
