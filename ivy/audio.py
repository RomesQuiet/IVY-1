# mae/audio.py
"""Audio coordination — one mic owner, wake abort, force-wake signals."""

import threading

_mic_lock = threading.Lock()
_abort_wake = threading.Event()
_force_wake = threading.Event()


def acquire_mic(timeout=None):
    return _mic_lock.acquire(timeout=timeout) if timeout else _mic_lock.acquire()


def release_mic():
    try:
        _mic_lock.release()
    except RuntimeError:
        pass


def abort_wake():
    _abort_wake.set()


def clear_abort_wake():
    _abort_wake.clear()


def is_wake_aborted():
    return _abort_wake.is_set()


def request_force_wake():
    _force_wake.set()


def consume_force_wake():
    if _force_wake.is_set():
        _force_wake.clear()
        return True
    return False
