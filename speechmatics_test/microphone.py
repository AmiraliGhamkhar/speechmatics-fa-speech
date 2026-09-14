"""Microphone capture for realtime streaming.

Design rules
------------
- 16 kHz mono 16-bit PCM chunks (``CHUNK`` = 200 ms).
- Chunks exist only in memory while they are being streamed. This module
  never writes, saves, or retains audio on disk (no WAV, no temp files).
- Proper context manager: the input stream and the PyAudio instance are
  released on ``__exit__`` for normal exit, errors, and Ctrl+C.
- ``device_index`` selects the input device (``None`` = system default).
"""

from __future__ import annotations

from typing import Optional

try:  # PyAudio is optional at import time so the package stays importable
    import pyaudio  # type: ignore  # without audio hardware/PyAudio installed.
except ImportError:  # pragma: no cover - depends on environment
    pyaudio = None  # type: ignore


class MicrophoneError(RuntimeError):
    """Raised when the microphone cannot be opened or is misused."""


class MicrophoneRecorder:
    """In-memory microphone stream (context manager, no audio persistence)."""

    RATE = 16000
    CHANNELS = 1
    CHUNK = 3200          # samples per read (200 ms)
    CHUNK_BYTES = CHUNK * 2  # 16-bit samples

    def __init__(
        self,
        device_index: Optional[int] = None,
        pyaudio_module=None,
    ) -> None:
        module = pyaudio_module if pyaudio_module is not None else pyaudio
        if module is None:
            raise MicrophoneError(
                "PyAudio is required to record from the microphone. "
                "Install it with: pip install PyAudio "
                "(Linux: sudo apt install portaudio19-dev first)"
            )
        self.device_index = device_index
        self._pyaudio = module
        self.audio = module.PyAudio()
        self.stream = None
        self._closed = False

    # -------------------------------------------------------- context mgmt

    def __enter__(self) -> "MicrophoneRecorder":
        if self._closed or self.stream is not None:
            raise MicrophoneError("MicrophoneRecorder is already started")
        try:
            self.stream = self.audio.open(
                format=self._pyaudio.paInt16,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK,
                input_device_index=self.device_index,
            )
        except Exception:
            # Release PyAudio even when the stream itself fails to open.
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Always release the stream + PyAudio; never suppress exceptions.
        self.close()
        return False

    # ------------------------------------------------------------- reading

    def read(self) -> bytes:
        """Read one 200 ms PCM16 chunk (in memory only)."""
        if self.stream is None:
            raise MicrophoneError("MicrophoneRecorder is not started")
        return self.stream.read(self.CHUNK, exception_on_overflow=False)

    # -------------------------------------------------------------- close

    def close(self) -> None:
        """Release the input stream and the PyAudio instance (idempotent)."""
        if self._closed:
            return
        if self.stream is not None:
            for op in ("stop_stream", "close"):
                try:
                    getattr(self.stream, op)()
                except Exception:
                    pass
            self.stream = None
        try:
            self.audio.terminate()
        except Exception:
            pass
        self._closed = True
