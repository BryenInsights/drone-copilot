"""Microphone audio capture using sounddevice."""

import asyncio
import logging

import sounddevice as sd

logger = logging.getLogger(__name__)


class AudioCapture:
    """Captures microphone audio at 16kHz, mono, int16, 100ms chunks.

    Pushes PCM bytes to an asyncio.Queue via loop.call_soon_threadsafe.
    """

    def __init__(self, sample_rate: int = 16000, chunk_ms: int = 100) -> None:
        self._sample_rate = sample_rate
        self._chunk_size = int(sample_rate * chunk_ms / 1000)  # samples per chunk
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._stream: sd.RawInputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _callback(self, indata: bytes, frames: int, time_info, status) -> None:
        if status:
            logger.warning("Audio capture status: %s", status)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, bytes(indata))

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._stream = sd.RawInputStream(
            samplerate=self._sample_rate,
            channels=1,
            dtype="int16",
            blocksize=self._chunk_size,
            callback=self._callback,
        )
        self._stream.start()
        logger.info(
            "Audio capture started: %dHz, %d samples/chunk",
            self._sample_rate, self._chunk_size,
        )

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("Audio capture stopped")

    @property
    def queue(self) -> asyncio.Queue[bytes]:
        return self._queue

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
