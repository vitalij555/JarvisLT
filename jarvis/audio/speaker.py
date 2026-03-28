"""Text-to-speech via speaches REST API + sounddevice playback."""

from __future__ import annotations

import io
import logging
import wave

import httpx
import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)


class Speaker:
    def __init__(self, base_url: str, model: str, voice: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice

    async def speak(self, text: str) -> None:
        """Convert text to speech and play it through the default audio output."""
        if not text.strip():
            return
        logger.info("TTS: %r", text)
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.base_url}/v1/audio/speech",
                json={
                    "model": self.model,
                    "voice": self.voice,
                    "input": text,
                    "response_format": "wav",
                },
            )
            response.raise_for_status()
            audio_bytes = response.content

        self._play_wav(audio_bytes)

    @staticmethod
    def _play_wav(wav_bytes: bytes) -> None:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            frames = wf.readframes(wf.getnframes())

        dtype_map = {1: np.int8, 2: np.int16, 4: np.int32}
        dtype = dtype_map.get(sample_width, np.int16)
        audio = np.frombuffer(frames, dtype=dtype)
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels)

        sd.play(audio, samplerate=sample_rate)
        sd.wait()
