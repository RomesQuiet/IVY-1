#!/usr/bin/env python3
"""IVY-1 — Personal AI Assistant (Groq-powered, streaming)."""

import threading

from ivy.brain import Brain
from ivy import config, mcp_client, senses, voice
from ivy.toolkit import describe_call

BANNER = r"""
  ___ __     __ __   __       _
 |_ _|\ \   / / \ \ / /      / |
  | |  \ \ / /   \ V /_____  | |
  | |   \ V /     | |_____| | |
 |___|   \_/      |_|       |_|
       Personal AI Assistant
"""


def main():
    print(BANNER)
    print("Commands:  /voice  /wake  /text  /reset  /memories  /model <name>  /doctor  /quit\n")

    brain = Brain()
    for warm_up in (voice.warm_up, senses.warm_up, mcp_client.load_all):
        threading.Thread(target=warm_up, daemon=True).start()
    for line in senses.doctor():
        if "OFF" in line or "MISSING" in line:
            print(f"⚠️  {line}")
    mode = "text"          # "text" | "voice" | "wake"
    listener = None
    awake = False          # sub-state for wake mode: False=sleeping, True=conversing

    while True:
        user_input = ""

        # =================================================================
        # WAKE MODE
        # =================================================================
        if mode == "wake":
            # --- SLEEPING: wait for the wake word ---
            if not awake:
                try:
                    if listener is None:
                        from ivy.wake import WakeListener
                        listener = WakeListener(
                            wakeword=config.WAKE_WORD,
                            threshold=config.WAKE_THRESHOLD,
                        )
                    if not listener.wait_for_wake():
                        continue
                    voice.chime()
                    awake = True
                    print("👂 Awake — I'm listening. "
                          "Say 'stop listening' or 'goodbye' to sleep again.\n")
                except KeyboardInterrupt:
                    mode = "text"
                    awake = False
                    print("\n🛑 Wake mode OFF.\n")
                    continue
                except Exception as e:
                    print(f"[wake error] {e}")
                    mode = "text"
                    awake = False
                    continue
                continue

            # --- AWAKE: wait for her reply to finish, then listen ---
            try:
                voice.wait_until_silent()          # no self-echo
                wav = voice.record_until_silence()
                user_input = voice.transcribe(wav)
                if not user_input:
                    continue
                print(f"You (voice): {user_input}")

                if voice.should_exit_voice(user_input):
                    awake = False
                    voice.speak("Going to sleep.")
                    voice.wait_until_silent()
                    continue
            except KeyboardInterrupt:
                mode = "text"
                awake = False
                voice.stop_speaking()
                print("\n🛑 Wake mode OFF.\n")
                continue
            except Exception as e:
                print(f"[wake error] {e}")
                continue

        # =================================================================
        # VOICE MODE
        # =================================================================
        elif mode == "voice":
            try:
                voice.wait_until_silent()          # no self-echo
                wav = voice.record_until_silence()
                user_input = voice.transcribe(wav)
                if not user_input:
                    continue
                print(f"You (voice): {user_input}")

                if voice.should_exit_voice(user_input):
                    mode = "text"
                    print("⌨️  Text mode ON.\n")
                    continue
            except KeyboardInterrupt:
                mode = "text"
                voice.stop_speaking()
                print("\n⌨️  Text mode ON.\n")
                continue
            except Exception as e:
                print(f"[voice error] {e}")
                continue

        # =================================================================
        # TEXT MODE
        # =================================================================
        else:
            try:
                user_input = input("You: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nGoodbye.")
                break
            if not user_input:
                continue

        # =================================================================
        # SLASH COMMANDS
        # =================================================================
        if user_input.startswith("/"):
            cmd, _, arg = user_input.partition(" ")
            if cmd == "/quit":
                print("Goodbye.")
                break
            elif cmd == "/reset":
                brain.reset()
                print("Conversation cleared.")
            elif cmd == "/memories":
                from ivy.memory import get_store
                mems = get_store().all()
                if not mems:
                    print("(no memories yet)")
                else:
                    for m in mems:
                        print(f"  [{m['id']}] {m['text']}  ({m['updated']})")
            elif cmd == "/voice":
                mode = "voice"
                awake = False
                print("🎙️  Voice mode ON. Say 'stop listening' or Ctrl+C for text.\n")
            elif cmd == "/wake":
                mode = "wake"
                awake = False
                pretty = config.WAKE_WORD.replace("_", " ")
                print(f"🔮 Wake-word mode ON. Say '{pretty}'. Ctrl+C to stop.\n")
            elif cmd == "/text":
                mode = "text"
                awake = False
                print("⌨️  Text mode ON.\n")
            elif cmd == "/doctor":
                for line in senses.doctor() + [f"MCP — {m}" for m in mcp_client.status()]:
                    print(f"  {line}")
            elif cmd == "/model":
                name = arg.strip()
                if not name or name == "auto":
                    brain.force_model = None
                    print("Model: auto (router decides per task)")
                else:
                    brain.force_model = name
                    print(f"Model: forced to {name}")
            else:
                print(f"Unknown command: {cmd}")
            continue

        # =================================================================
        # STREAM FROM BRAIN
        # =================================================================
        printed_prefix = {"done": False}

        def stream_token(t):
            if not printed_prefix["done"]:
                print("IVY-1: ", end="", flush=True)
                printed_prefix["done"] = True
            print(t, end="", flush=True)

        def status(msg):
            print(f"  {msg}")

        on_sentence = voice.speak_async if mode in ("voice", "wake") else None

        try:
            brain.chat(
                user_input,
                on_tool=lambda name, args: print(f"\n  {describe_call(name, args)}"),
                on_token=stream_token,
                on_sentence=on_sentence,
                on_status=status,
            )
        except KeyboardInterrupt:
            voice.stop_speaking()
            print("\n(aborted)\n")
            continue
        except Exception as e:
            print(f"\n[error] {e}\n")
            continue

        if not printed_prefix["done"]:
            print("IVY-1: (no reply)")
        print("\n")

        # --- barge-in monitor (voice/wake only) ---
        # Only arm barge-in after she's been speaking for a moment, to avoid
        # catching the tail of her own first word through the mic.
        if mode in ("voice", "wake") and voice.is_speaking():
            import time as _t
            _t.sleep(1.0)
            if voice.is_speaking():
                barged = voice.monitor_for_bargein()
                if barged:
                    print("  🔇 (interrupted — go ahead)\n")

if __name__ == "__main__":
    main()
