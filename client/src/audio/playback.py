"""Audio playback with barge-in support using sounddevice."""

import asyncio
import logging
import time

import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioPlayback:
    """Plays AI voice responses at 24kHz, mono, int16.

    Supports barge-in by clearing the queue when interrupted.
    """

    def __init__(self, sample_rate: int = 24000, chunk_ms: int = 100) -> None:
        self._sample_rate = sample_rate
        self._chunk_size = int(sample_rate * chunk_ms / 1000)
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream: sd.RawOutputStream | None = None
        self._last_audio_time: float = 0.0

    def _callback(self, outdata: bytearray, frames: int, time_info, status) -> None:
        if status:
            logger.warning("Audio playback status: %s", status)
        try:
            data = self._queue.get_nowait()
            # Pad or trim to match expected frame size
            expected = frames * 2  # int16 = 2 bytes per sample
            if len(data) < expected:
                outdata[:len(data)] = data
                outdata[len(data):] = b'\x00' * (expected - len(data))
            else:
                outdata[:] = data[:expected]
            self._last_audio_time = time.monotonic()
        except asyncio.QueueEmpty:
            outdata[:] = b'\x00' * len(outdata)

    @property
    def is_playing(self) -> bool:
        """True if audio is actively being output (or was within 150ms)."""
        if not self._queue.empty():
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
        """Clear audio queue for barge-in handling (R6)."""
        cleared = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                cleared += 1
            except asyncio.QueueEmpty:
                break
        if cleared:
            logger.info("Barge-in: cleared %d audio chunks", cleared)

    def enqueue(self, pcm_bytes: bytes) -> None:
        """Add PCM audio to playback queue."""
        self._queue.put_nowait(pcm_bytes)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
