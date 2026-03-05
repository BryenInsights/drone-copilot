"""Audio playback with barge-in support using sounddevice."""

import logging
import threading
import time

import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioPlayback:
    """Plays AI voice responses at 24kHz, mono, int16.

    Uses a continuous byte buffer instead of a per-chunk queue so the
    sounddevice callback always reads exactly the bytes it needs,
    eliminating silence gaps from variable-size Gemini audio chunks.
    """

    def __init__(self, sample_rate: int = 24000, chunk_ms: int = 100) -> None:
        self._sample_rate = sample_rate
        self._chunk_size = int(sample_rate * chunk_ms / 1000)
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None
        self._last_audio_time: float = 0.0

    def _callback(self, outdata: bytearray, frames: int, time_info, status) -> None:
        if status:
            logger.warning("Audio playback status: %s", status)
        expected = frames * 2  # int16 = 2 bytes per sample
        with self._lock:
            available = len(self._buffer)
            if available >= expected:
                outdata[:] = bytes(self._buffer[:expected])
                del self._buffer[:expected]
                self._last_audio_time = time.monotonic()
            elif available > 0:
                outdata[:available] = bytes(self._buffer)
                outdata[available:] = b'\x00' * (expected - available)
                self._buffer.clear()
                self._last_audio_time = time.monotonic()
            else:
                outdata[:] = b'\x00' * expected

    @property
    def is_playing(self) -> bool:
        """True if audio is actively being output (or was within 150ms)."""
        with self._lock:
            has_data = len(self._buffer) > 0
        if has_data:
            return True
        return (time.monotonic() - self._last_audio_time) < 0.15

    def start(self) -> None:
        self._stream = sd.RawOutputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._chunk_size,
            callback=self._callback,
        )
        self._stream.start()
        logger.info("Audio playback started: %dHz", self._sample_rate)

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("Audio playback stopped")

    def clear_queue(self) -> None:
        """Clear audio buffer for barge-in handling (R6)."""
        with self._lock:
            size = len(self._buffer)
            self._buffer.clear()
        if size:
            logger.info("Barge-in: cleared %d bytes of audio", size)

    def enqueue(self, pcm_bytes: bytes) -> None:
        """Add PCM audio to playback buffer."""
        with self._lock:
            self._buffer.extend(pcm_bytes)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
