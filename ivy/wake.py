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


def _sd():
    import sounddevice      # takes about 7 s to load, and only voice needs it
    return sounddevice



SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80 ms at 16 kHz

_DEFAULT_MODEL = Path(__file__).resolve().parent / "models" / "ok_ivy.onnx"

# The model scores pure digital silence (all-zero samples) at ~0.73, while
# any real mic noise scores ~0.005. macOS hands us all-zero buffers when the
# mic permission is missing or the stream is still warming up, so those
# windows must never reach the model.
SILENCE_RMS = 2
SILENCE_WARN_SEC = 3.0

MEL_CONTEXT = 480            # samples of overlap so each frame yields 8 clean mel frames
MEL_FRAMES_PER_FRAME = 8     # 1280 samples / 160 hop
EMBED_WINDOW = 76            # mel frames per embedding
EMBEDDINGS = 16              # classifier input length


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
        self._classifier, self._classifier_input = self.model._classifiers[self.model_name]
        self._reset_stream()

    # WakeWordModel.predict() recomputes 16 embeddings over a 2 s window every
    # call — 90–200 ms on this Mac, longer than the 80 ms between frames, so the
    # listener fell behind the mic. Each new frame only adds one embedding, so
    # we keep a rolling mel/embedding history and score in ~5 ms instead.
    # (Scores match the full-window predict to within ~0.04.)

    def _reset_stream(self):
        self._tail = np.zeros(0, dtype=np.float32)      # last 480 samples of context
        self._mel = np.zeros((0, 32), dtype=np.float32)  # last 76 mel frames
        self._embeddings: list[np.ndarray] = []          # last 16 embeddings

    def _score_frame(self, frame: np.ndarray) -> float:
        x = frame.astype(np.float32) / 32768.0
        chunk = np.concatenate([self._tail, x])
        self._tail = x[-MEL_CONTEXT:]
        mel = self.model._mel_frontend(chunk)[0][-MEL_FRAMES_PER_FRAME:]
        self._mel = np.concatenate([self._mel, mel])[-EMBED_WINDOW:]
        if len(self._mel) < EMBED_WINDOW:
            return 0.0
        self._embeddings.append(self.model._speech_embedding(self._mel[np.newaxis])[0])
        self._embeddings = self._embeddings[-EMBEDDINGS:]
        if len(self._embeddings) < EMBEDDINGS:
            return 0.0
        emb = np.stack(self._embeddings)[np.newaxis].astype(np.float32)
        return float(self._classifier.run(None, {self._classifier_input: emb})[0][0, 0])

    def wait_for_wake(self) -> bool:
        """Block until wake word (or force-wake). Returns True on wake, False on abort."""
        pretty = self.wakeword.replace("_", " ")
        print(f"💤 Sleeping — say '{pretty}' ...")

        self._reset_stream()

        if not audio.acquire_mic(timeout=5.0):
            print("(mic busy)")
            return False

        silent_since = time.time()
        warned_silent = False

        try:
            with _sd().InputStream(
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
                        self._reset_stream()
                        if not warned_silent and time.time() - silent_since > SILENCE_WARN_SEC:
                            print("⚠️  Mic is delivering pure silence — check System Settings "
                                  "→ Privacy & Security → Microphone.")
                            warned_silent = True
                        continue
                    silent_since = time.time()

                    try:
                        score = self._score_frame(audio_np)
                    except Exception:
                        self._reset_stream()
                        continue

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
