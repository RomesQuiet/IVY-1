# IVY-1

A personal, Jarvis-style AI assistant for macOS. Ivy works on your whole Mac like a coding agent:
she looks at what's really there, acts, then checks what actually happened.

## What she can do

- **Talk:** text or voice, with the wake word "OK Ivy" and interruption while she speaks.
- **See the Mac:** read the screen on the device, and know the front app, window, tab, music and volume.
- **Act:** open apps, click and type, run commands, write files and build projects.
- **Browse:** use her own browser, with a separate profile from yours.
- **Manage daily life:** Messages, Mail, Calendar, Reminders, Notes and music.
- **Work in the background:** run long research and build tasks, then report back.
- **Remember and learn:** keep facts about you and lessons from your corrections.
- **Speak up:** alert you about your battery, calendar, downloads and disk space.
- **Stay close:** live in the menu bar, with ⌥Space to show or hide her from any app.

## How she thinks

Every request takes the same steps, and only the depth changes:

1. **Router:** picks the model and how hard to think.
2. **Rules and context:** sized to the request.
3. **Think and decide:** answer, do it, say then do, plan, ask or decline.
4. **Act and check:** tools report what really happened.

Opening an app costs under 500 tokens. Building a project gets the strongest route and full context.

## Safety

- She asks before anything irreversible, and only uninstalls apps you named.
- Commands that would delete apps or system folders are refused outright.
- Files she removes are kept so they can be restored.
- Text from web pages, the screen, files and emails is treated as information, never as instructions.
- Every action is written to a private log at `~/Library/Logs/Ivy/actions.jsonl`.

## Requirements

- macOS (developed on macOS 13, Intel)
- Python 3.11
- A [Groq](https://groq.com) API key

## Setup

```bash
git clone https://github.com/RomesQuiet/IVY-1.git
cd IVY-1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
echo "GROQ_API_KEY=your_key" > .env
```

Run her in the terminal:

```bash
python ivy.py
```

Or run the desktop window:

```bash
python ivy_gui.py
```

To build `/Applications/Ivy.app`, run the script below. It uses a fixed project path, so change
`PROJECT_DIR` at the top first if your copy is somewhere else.

```bash
./make_app.sh
```

macOS will ask for Microphone, Screen Recording and Accessibility permissions. Type `/doctor` to
see what's working.

## Commands

| Command | What it does |
|---|---|
| `/doctor` | Check permissions and health |
| `/model <name>` | Choose a model, or `auto` for the router |
| `/tasks` | Show background tasks |
| `/help` | List commands in the app |

## Tests

```bash
python -m tests.run
python tests/mutation_check.py
```

The first runs the test suite without touching your Mac. The second puts past bugs back into a
throwaway copy to prove the tests catch them, and takes about twenty minutes.

## Limits

Groq's free plan allows 8,000 tokens per minute per model, so long tasks sometimes wait.
