#!/usr/bin/env python3
"""IVY-1 — Personal AI Assistant (Groq-powered, streaming)."""

from ivy.brain import Brain
from ivy import voice

BANNER = r"""
  __  __    _    _____     ____
 |  \/  |  / \  | ____|   |___ \
 | |\/| | / _ \ |  _|       __) |
 | |  | |/ ___ \| |___     / __/
 |_|  |_/_/   \_\_____|   |_____|
       Personal AI Assistant — v2.3.5
"""


def main():
    print(BANNER)
    print("Commands:  /voice  /wake  /text  /reset  /memories  /model <name>  /quit\n")

    brain = Brain()
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
                        listener = WakeListener(wakeword="hey_jarvis", threshold=0.5)
                    listener.wait_for_wake()
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
                print(f"   ↪ exit-check: {voice.should_exit_voice(user_input)}")   # ← add

                if voice.should_exit_voice(user_input):
                    awake = False
                    voice.speak("Going to sleep.")
                    voice.wait_until_silent()
                    print("💤 Sleeping — say 'Hey Jarvis' to wake me.\n")
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
                brain.messages = brain.messages[:1]
                print("Memory cleared.")
            elif cmd == "/memories":
                from ivy.memory import get_store
                mems = get_store().all()
                if not mems:
                    print("(no memories yet)")
                else:
                    for m in mems:
                        print(f"  [{m['id']}] {m['text']}  ({m['updated']})")
            elif cmd == "/wake":
                mode = "wake"
                awake = False
                print("🔮 Wake-word mode ON. Say 'Hey Jarvis'. Ctrl+C to stop.\n")
            elif cmd == "/text":
                mode = "text"
                awake = False
                print("⌨️  Text mode ON.\n")
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
                print("MAE-2: ", end="", flush=True)
                printed_prefix["done"] = True
            print(t, end="", flush=True)

        def status(msg):
            print(f"  {msg}")

        on_sentence = voice.speak_async if mode in ("voice", "wake") else None

        try:
            brain.chat(
                user_input,
                on_tool=lambda name, args: print(f"\n  ⚙️  {name}({args})"),
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
            print("MAE-2: (no reply)")
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
