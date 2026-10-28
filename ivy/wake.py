# ivy/wake.py
"""
Wake-word listener for IVY-1.

Uses livekit-wakeword with a custom "OK Ivy" model trained in Colab.
Fully offline, CPU-friendly. Only when the wake word fires do we start
recording audio for Whisper; while sleeping, audio is processed in RAM
and immediately discarded.
"""
import time
from pathlib import Path

from . import audio, config

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80 ms at 16 kHz

_DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "ok_ivy.onnx"

# The model scores pure digital silence (all-zero samples) at ~0.73, while
# any real mic noise scores ~0.005. macOS hands us all-zero buffers when the
# mic permission is missing or the stream is still warming up, so those
# windows must never reach the model.
SILENCE_RMS = 2
SILENCE_WARN_SEC = 3.0


class WakeListener:
    def __init__(self, wakeword: str = "ok_ivy", threshold: float = 0.5,
                 model_path: str | None = None):
        # Lazy import so the rest of IVY-1 still works if listener isn't installed.
        from livekit.wakeword import WakeWordModel

        path = Path(model_path or getattr(config, "WAKE_MODEL_PATH", _DEFAULT_MODEL))
        if not path.exists():
            raise RuntimeError(
                f"Wake word model not found at {path}. "
                "Place ok_ivy.onnx in ivy/models/."
            )

        self.model = WakeWordModel(models=[str(path)])
        self.model_name = path.stem
        self.wakeword = wakeword
        self.threshold = threshold

        # Rolling ~2 s window of 16 kHz int16 audio
        self.window_samples = SAMPLE_RATE * 2
        self._buffer = np.zeros(0, dtype=np.int16)

    def wait_for_wake(self) -> bool:
        """Block until wake word (or force-wake). Returns True on wake, False on abort."""
        pretty = self.wakeword.replace("_", " ")
        print(f"💤 Sleeping — say '{pretty}' ...")

        self._buffer = np.zeros(0, dtype=np.int16)

        if not audio.acquire_mic(timeout=5.0):
            print("(mic busy)")
            return False

        silent_since = time.time()
        warned_silent = False

        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=FRAME_SIZE,
            ) as stream:
                while True:
                    if audio.is_wake_aborted():
                        audio.clear_abort_wake()
                        return False
                    if audio.consume_force_wake():
                        print("👂 Forced wake")
                        return True

                    chunk, _ = stream.read(FRAME_SIZE)
                    audio_np = np.asarray(chunk, dtype=np.int16).flatten()
                    rms = int(np.sqrt(np.mean(audio_np.astype(np.float32) ** 2)))

                    # Dead-silent input: drop it, and tell the user once.
                    if rms < SILENCE_RMS:
                        self._buffer = np.zeros(0, dtype=np.int16)
                        if not warned_silent and time.time() - silent_since > SILENCE_WARN_SEC:
                            print("⚠️  Mic is delivering pure silence — check System Settings "
                                  "→ Privacy & Security → Microphone.")
                            warned_silent = True
                        continue
                    silent_since = time.time()

                    # Accumulate into a rolling ~2 second window
                    self._buffer = np.concatenate([self._buffer, audio_np])
                    if len(self._buffer) > self.window_samples:
                        self._buffer = self._buffer[-self.window_samples:]

                    if len(self._buffer) < self.window_samples:
                        continue

                    try:
                        prediction = self.model.predict(self._buffer)
                    except Exception:
                        continue

                    score = float(prediction.get(self.model_name, 0.0))

                    if getattr(config, "DEBUG_WAKE", False):
                        print(f"\r   rms={rms:5d}  score={score:.3f}  "
                              f"thr={self.threshold:.2f}",
                              end="", flush=True)

                    if score >= self.threshold:
                        if getattr(config, "DEBUG_WAKE", False):
                            print()
                        print(f"👂 Woken (score {score:.2f})")
                        return True
        finally:
            audio.release_mic()
