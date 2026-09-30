# mae/wake.py
"""
Wake-word listener for IVY-1.

Uses livekit-wakeword with a custom "OK Ivy" model trained in Colab.
Fully offline, CPU-friendly. Only when the wake word fires do we start
recording audio for Whisper; while sleeping, audio is processed in RAM
and immediately discarded.
"""
from pathlib import Path

from . import audio, config

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80 ms at 16 kHz

_DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "ok_ivy.onnx"


class WakeListener:
    def __init__(self, wakeword: str = "ok_ivy", threshold: float = 0.5,
                 model_path: str | None = None):
        # Lazy import so the rest of MAE-2 still works if listener isn't installed.
        from livekit.wakeword import WakeWordModel

        path = Path(model_path) if model_path else _DEFAULT_MODEL
        if not path.exists():
            raise RuntimeError(
                f"Wake word model not found at {path}. "
                "Place ok_ivy.onnx in mae/models/."
            )

        self.model = WakeWordModel(models=[str(path)])
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

                    if isinstance(prediction, dict):
                        score = float(max(prediction.values())) if prediction else 0.0
                    else:
                        score = float(prediction)

                    if score >= self.threshold:
                        print(f"👂 Woken (score {score:.2f})")
                        return True
        finally:
            audio.release_mic()