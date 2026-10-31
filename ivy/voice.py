# ivy/voice.py
import re

from . import audio

import subprocess
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import webrtcvad
from openai import OpenAI

from . import config


def _sd():
    import sounddevice      # takes about 7 s to load, and only voice needs it
    return sounddevice


_client = OpenAI(base_url=config.GROQ_BASE_URL, api_key=config.GROQ_API_KEY)

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SIZE = int(SAMPLE_RATE * FRAME_MS / 1000)  # 480 samples per frame

_TMP_WAV = Path(tempfile.gettempdir()) / "ivy_input.wav"

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
    silence_sec: float = 0.8,
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
        with _sd().InputStream(
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
            prompt="Ivy, OK Ivy, Romeu.",   # spelling hints for names
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

# ---------- speech (queued, serial, one persistent synthesizer) ----------
#
# `say` reloads the voice for every sentence (~1.4 s before each one on this Mac).
# A single NSSpeechSynthesizer keeps the voice loaded: speech starts in a few ms
# and stops instantly. Falls back to `say` if AppKit isn't available.

import queue as _queue
import threading as _threading

_say_queue = _queue.Queue()
_say_thread = None
_generation = 0                  # bumped by stop_speaking(); older sentences are dropped
_busy = _threading.Event()        # set while a sentence is being spoken
_MD = re.compile(r"[*#`_>|~]+|\[(.*?)\]\(.*?\)|https?://\S+")


def _make_synth():
    try:
        import AppKit
    except Exception:
        return None
    wanted = getattr(config, "TTS_VOICE", "")
    voice_id = None
    for v in AppKit.NSSpeechSynthesizer.availableVoices():
        name = AppKit.NSSpeechSynthesizer.attributesForVoice_(v).get("VoiceName", "")
        if name == wanted or (voice_id is None and name.split(" (")[0] == wanted.split(" (")[0]):
            voice_id = v
    return AppKit.NSSpeechSynthesizer.alloc().initWithVoice_(voice_id)


def _speak_with_say(text: str, gen: int):
    proc = subprocess.Popen(["say", "-v", getattr(config, "TTS_VOICE", "Samantha"), text])
    while proc.poll() is None:
        if gen != _generation:
            proc.kill()
            return
        time.sleep(0.02)


def _say_worker():
    synth = _make_synth()          # the worker thread owns the synthesizer
    while True:
        gen, text = _say_queue.get()
        try:
            if gen != _generation:
                continue
            _busy.set()
            if synth is None:
                _speak_with_say(text, gen)
            else:
                synth.startSpeakingString_(text)
                t0 = time.time()
                while not synth.isSpeaking() and time.time() - t0 < 1.0:
                    time.sleep(0.005)
                while synth.isSpeaking():
                    if gen != _generation:
                        synth.stopSpeaking()
                        break
                    time.sleep(0.02)
        except Exception as e:
            print(f"[speech error] {e}")
        finally:
            _busy.clear()
            _say_queue.task_done()


def _ensure_say_thread():
    global _say_thread
    if _say_thread is None or not _say_thread.is_alive():
        _say_thread = _threading.Thread(target=_say_worker, daemon=True, name="tts")
        _say_thread.start()


def speak_async(text: str, voice: str = None):
    if not text or getattr(config, "MUTED", False):
        return
    clean = re.sub(r"\s+", " ", _MD.sub(lambda m: m.group(1) or "", text)).strip()
    if not clean:
        return
    _ensure_say_thread()
    _say_queue.put((_generation, clean))


def speak(text: str, voice: str = None):
    """Blocking speak — wait for the queue to drain."""
    speak_async(text)
    _say_queue.join()


def stop_speaking():
    """Cut the current sentence and drop anything queued."""
    global _generation
    _generation += 1
    while True:
        try:
            _say_queue.get_nowait()
            _say_queue.task_done()
        except _queue.Empty:
            break


def warm_up():
    """Load the voice and open the Groq connection before the first request."""
    _ensure_say_thread()
    try:
        _client.models.list()
    except Exception:
        pass

def wait_until_silent(timeout: float = 30.0):
    """Block until `say` has finished the whole queue. Prevents self-echo."""
    start = time.time()
    while time.time() - start < timeout:
        if not is_speaking():
            time.sleep(0.25)   # small grace period to let the speaker go quiet
            return
        time.sleep(0.03)

def is_speaking() -> bool:
    """True if `say` is currently speaking or has sentences queued."""
    return _busy.is_set() or _say_queue.unfinished_tasks > 0

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
        with _sd().InputStream(
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
