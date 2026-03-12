"""Client-side Voice Activity Detection using webrtcvad."""

import enum
import logging
import time

import webrtcvad

logger = logging.getLogger(__name__)

# 20ms sub-frame at 16kHz = 320 samples = 640 bytes
_SUBFRAME_BYTES = 640
_SUBFRAMES_PER_CHUNK = 5  # 100ms chunk / 20ms sub-frame


class _State(enum.Enum):
    IDLE = "idle"
    ACTIVE = "active"
    HANGOVER = "hangover"


class VoiceActivityDetector:
    """Filters silent audio chunks before sending to Gemini.

    Splits 100ms PCM chunks (3200 bytes at 16kHz int16) into 5x 20ms
    sub-frames for webrtcvad analysis. Includes a hangover window to
    capture trailing speech after the last detected voice frame.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        aggressiveness: int = 2,
        hangover_max: int = 10,
    ) -> None:
        self._vad = webrtcvad.Vad(aggressiveness)
        self._sample_rate = sample_rate
        self._hangover_max = hangover_max
        self._hangover_remaining = 0
        self._state = _State.IDLE
        self._last_speech_time: float = 0.0

    def should_forward(self, pcm_bytes: bytes) -> bool:
        """Return True if the audio chunk should be forwarded to Gemini.

        Splits the 100ms chunk into 5x 20ms sub-frames and checks each
        for speech. Returns True if any sub-frame has speech or the
        hangover window is active.
        """
        speech_detected = False
        for i in range(_SUBFRAMES_PER_CHUNK):
            start = i * _SUBFRAME_BYTES
            end = start + _SUBFRAME_BYTES
            sub_frame = pcm_bytes[start:end]
            if len(sub_frame) < _SUBFRAME_BYTES:
                break
            if self._vad.is_speech(sub_frame, self._sample_rate):
                speech_detected = True
                break

        if speech_detected:
            if self._state != _State.ACTIVE:
                logger.debug("VAD: %s -> ACTIVE", self._state.value)
            self._state = _State.ACTIVE
            self._hangover_remaining = self._hangover_max
            self._last_speech_time = time.time()
            return True

        if self._hangover_remaining > 0:
            self._hangover_remaining -= 1
            if self._state != _State.HANGOVER:
                logger.debug("VAD: ACTIVE -> HANGOVER (%d remaining)", self._hangover_remaining)
                self._state = _State.HANGOVER
            if self._hangover_remaining == 0:
                logger.debug("VAD: HANGOVER -> IDLE")
                self._state = _State.IDLE
            return True

        if self._state != _State.IDLE:
            logger.debug("VAD: %s -> IDLE", self._state.value)
            self._state = _State.IDLE
        return False

    @property
    def last_speech_time(self) -> float:
        return self._last_speech_time

    def reset(self) -> None:
        """Reset state to IDLE and clear hangover counter."""
        if self._state != _State.IDLE or self._hangover_remaining > 0:
            logger.debug(
                "VAD: reset (%s, hangover=%d) -> IDLE",
                self._state.value, self._hangover_remaining,
            )
        self._state = _State.IDLE
        self._hangover_remaining = 0
