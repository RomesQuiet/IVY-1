# mae/voice.py
import re

from . import audio

import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import webrtcvad
from openai import OpenAI

from . import config

_client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)

TTS_VOICE = "Kate (Enhanced)"   # try also: Serena, Karen, Moira, Tessa, Fiona
SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480 samples per frame

_TMP_WAV = Path(tempfile.gettempdir()) / "mae_input.wav"

# Say any of these to leave voice mode.

# Phrases that trigger exit when they appear in a SHORT utterance.
# All must be 2+ words, so they can't fire on a single common word.
EXIT_PHRASES = [
    "stop listening",
    "stop listening please",
    "stop listening now",
    "exit voice",
    "exit voice mode",
    "switch to text",
    "go to sleep",
    "going to sleep",
    "goodbye ivy",
    "bye ivy",
    "that's all",
    "thats all",
    "that will be all",
    "shut down",
    "shut up",
    "sleep now",
    "pause listening",
]

# Single words that trigger ONLY when they are the entire utterance.
EXIT_EXACT = {
    "stop",
    "sleep",
    "goodbye",
    "bye",
    "standby",
    "stand by",
}

# Max words in an utterance for it to be considered a command.
# Longer than this = conversation, not a command.
EXIT_MAX_WORDS = 6


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", (text or "").lower()).strip()


def should_exit_voice(text: str) -> bool:
    t = _normalize(text)
    if not t:
        return False

    words = t.split()

    # Long sentences are never exit commands.
    if len(words) > EXIT_MAX_WORDS:
        return False

    # Exact single-word exits — must be the whole utterance.
    if t in EXIT_EXACT:
        return True

    # Multi-word phrases — must appear as a contiguous block.
    for p in EXIT_PHRASES:
        np = _normalize(p)
        if len(np.split()) >= 2 and np in t:
            return True

    return False



def record_until_silence(
    silence_sec: float = 1.0,
    max_sec: float = 12.0,
    start_timeout_sec: float = 6.0,
    aggressiveness: int = 3,          # 3 = strict, filters background noise
    min_speech_frames: int = 12,      # ~360 ms minimum actual speech
    rms_floor: int = 250,             # ignore anything quieter than this
) -> str:
    """
    Record until the speaker stops. Returns WAV path, or '' if nothing
    meaningful was captured (silence, too short, or just noise).
    """
    vad = webrtcvad.Vad(aggressiveness)
    silence_limit = int(silence_sec * 1000 / FRAME_MS)

    print("🎙️  Listening...")
    frames: list[np.ndarray] = []
    speech_frames = 0
    triggered = False
    silence_count = 0
    t0 = time.time()

    if not audio.acquire_mic(timeout=3.0):
        print("(mic busy — try again)")
        return ""
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16"
        ) as stream:

            while True:
                if audio.is_wake_aborted():
                    audio.clear_abort_wake()
                    return ""
                elapsed = time.time() - t0
                if elapsed > max_sec:
                    break
                if not triggered and elapsed > start_timeout_sec:
                    break

                chunk, _ = stream.read(FRAME_SIZE)
                pcm = chunk.tobytes()
                rms = int(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

                # Require both VAD speech AND sufficient energy
                try:
                    is_speech = vad.is_speech(pcm, SAMPLE_RATE) and rms >= rms_floor
                except Exception:
                    is_speech = False

                if is_speech:
                    triggered = True
                    speech_frames += 1
                    silence_count = 0
                elif triggered:
                    silence_count += 1

                if triggered:
                    frames.append(chunk.copy())
                    if silence_count >= silence_limit:
                        break

    except KeyboardInterrupt:
        raise
    finally:
        audio.release_mic()

    # Reject anything that wasn't real speech


    # Reject anything that wasn't real speech
    if not triggered or speech_frames < min_speech_frames or not frames:
        print("(didn't catch anything — listening again)")
        return ""

    audio_data = np.concatenate(frames, axis=0)
    with wave.open(str(_TMP_WAV), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_data.tobytes())
    return str(_TMP_WAV)


# Phrases Whisper invents on silence. Case-insensitive, stripped of punctuation.
_WHISPER_HALLUCINATIONS = {
    # YouTube-caption hallucinations Whisper invents on silence — never spoken by you
    "thanks for watching", "thanks for watching!",
    "thank you for watching", "thank you for watching.",
    "please subscribe", "subscribe",
    "subtitles by", "subtitle", "subtitles",
    # Single-token noise (still nonsense if it arrives alone)
    "you", "bye", "bye.", ".", "...", "♪", "[music]", "[silence]",
    "so", "uh", "um",
}

def _dedupe_repeats(text: str) -> str:
    """Whisper às vezes repete a mesma frase 2-5 vezes. Isto remove duplicados."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    if len(parts) < 2:
        return text
    seen = set()
    unique = []
    for p in parts:
        key = p.lower().strip(" .!?,;:")
        if key and key not in seen:
            seen.add(key)
            unique.append(p)
    if len(unique) < len(parts):
        print(f"(whisper repetiu {len(parts) - len(unique)}x — colapsado)")
        return " ".join(unique)
    return text

def transcribe(wav_path: str) -> str:
    if not wav_path:
        return ""

    with open(wav_path, "rb") as f:
        result = _client.audio.transcriptions.create(
            model=config.WHISPER_MODEL,
            file=f,
            language="en",
            temperature=0.0,
        )

    text = (result.text or "").strip()
    if not text:
        return ""

    text = _dedupe_repeats(text)

    normalized = text.lower().strip().rstrip(".!?,;:")
    if normalized in _WHISPER_HALLUCINATIONS:
        print("(ignored a Whisper hallucination)")
        return ""

    # Single-word exit commands ("stop", "goodbye") must survive the length filter.
    if _normalize(text) in EXIT_EXACT:
        return text

    if len(normalized.split()) < 2 and len(normalized) < 8:
        print("(too short — ignoring)")
        return ""

    return text

# ---------- speech (queued, serial, one process per sentence) ----------

import queue as _queue
import threading as _threading

_say_queue = _queue.Queue()
_say_thread = None
_say_lock = _threading.Lock()
_current_proc = None


def _say_worker():
    global _current_proc
    while True:
        text = _say_queue.get()
        if text is None:
            break
        try:
            with _say_lock:
                _current_proc = subprocess.Popen(
                    ["say", "-v", getattr(config, "TTS_VOICE", "Karen (Enhanced)"), text]
                )
            _current_proc.wait()
        except Exception as e:
            print(f"[say error] {e}")
        finally:
            with _say_lock:
                _current_proc = None
            _say_queue.task_done()


def _ensure_say_thread():
    global _say_thread
    if _say_thread is None or not _say_thread.is_alive():
        _say_thread = _threading.Thread(target=_say_worker, daemon=True)
        _say_thread.start()

def speak_async(text: str, voice: str = None):
    if not text:
        return
    if getattr(config, "MUTED", False):
        return
    clean = text.replace("*", "").replace("#", "").replace("`", "").strip()
    if not clean:
        return
    _ensure_say_thread()
    _say_queue.put(clean)




def speak(text: str, voice: str = None):
    """Blocking speak — wait for the queue to drain."""
    speak_async(text)
    _say_queue.join()


def stop_speaking():
    """Kill the current utterance and drop anything queued."""
    global _current_proc
    with _say_lock:
        if _current_proc and _current_proc.poll() is None:
            try:
                _current_proc.kill()
            except Exception:
                pass
        _current_proc = None

    while not _say_queue.empty():
        try:
            _say_queue.get_nowait()
            _say_queue.task_done()
        except Exception:
            break

def wait_until_silent(timeout: float = 30.0):
    """Block until `say` has finished the whole queue. Prevents self-echo."""
    start = time.time()
    while time.time() - start < timeout:
        with _say_lock:
            proc_alive = _current_proc is not None and _current_proc.poll() is None
        if not proc_alive and _say_queue.empty():
            time.sleep(0.3)   # small grace period to let the speaker go quiet
            return
        time.sleep(0.05)

def is_speaking() -> bool:
    """True if `say` is currently speaking or has sentences queued."""
    with _say_lock:
        proc_alive = _current_proc is not None and _current_proc.poll() is None
    return proc_alive or not _say_queue.empty()

def monitor_for_bargein(
    aggressiveness: int = 3,
    rms_floor: int = 2500,              # ← era 700. Muito mais estrito.
    grace_sec: float = 3.0,             # ← era 1.2. Espera 2s antes de escutar.
    timeout_sec: float = 60.0,
    min_sustained_frames: int = 12,      # ← NOVO: ~180ms de fala contínua
) -> bool:
    """Watch the mic while `say` is speaking. Requires sustained loud speech."""
    vad = webrtcvad.Vad(aggressiveness)
    start = time.time()
    speech_streak = 0

    time.sleep(grace_sec)

    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=FRAME_SIZE,
        ) as stream:
            while True:
                if time.time() - start > timeout_sec:
                    return False
                if not is_speaking():
                    return False

                try:
                    chunk, _ = stream.read(FRAME_SIZE)
                except Exception:
                    return False

                rms = int(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                if rms < rms_floor:
                    speech_streak = 0
                    continue

                try:
                    if vad.is_speech(chunk.tobytes(), SAMPLE_RATE):
                        speech_streak += 1
                        if speech_streak >= min_sustained_frames:
                            stop_speaking()
                            return True
                    else:
                        speech_streak = 0
                except Exception:
                    speech_streak = 0
    except Exception:
        return False


def chime():
    if getattr(config, "MUTED", False):
        return
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
