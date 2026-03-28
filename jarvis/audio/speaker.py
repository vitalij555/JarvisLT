"""Text-to-speech: speaches/Kokoro for English, edge-tts for other languages."""

from __future__ import annotations

import asyncio
import io
import logging
import wave

import edge_tts
import httpx
import miniaudio
import numpy as np
import sounddevice as sd
from langdetect import DetectorFactory, LangDetectException, detect

# Make langdetect deterministic
DetectorFactory.seed = 0

logger = logging.getLogger(__name__)


class Speaker:
    def __init__(
        self,
        base_url: str,
        model: str,
        voice: str,
        multilingual_voices: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.voice = voice
        self.multilingual_voices = multilingual_voices or {}

    def _detect_language(self, text: str) -> str:
        """Return ISO 639-1 language code, defaulting to 'en' on short/mixed/ambiguous text.

        We only switch away from English when the text is long enough for reliable detection
        and does not contain a significant amount of English words mixed in.
        """
        stripped = text.strip()
        # Too short to detect reliably — keep English TTS
        if len(stripped) < 60:
            return "en"
        # Count rough English word ratio: if >30% ASCII alpha words, treat as English
        words = stripped.split()
        ascii_words = sum(1 for w in words if w.isascii() and w.isalpha())
        if len(words) > 0 and ascii_words / len(words) > 0.3:
            return "en"
        try:
            return detect(stripped)
        except LangDetectException:
            return "en"

    async def speak(self, text: str) -> None:
        """Detect language, route to edge-tts or speaches, and play audio."""
        if not text.strip():
            return
        lang = self._detect_language(text)
        edge_voice = self.multilingual_voices.get(lang)
        if edge_voice:
            logger.info("TTS [%s → edge-tts %s]: %r", lang, edge_voice, text[:80])
            await self._speak_edge_tts(text, edge_voice)
        else:
            logger.info("TTS [speaches]: %r", text[:80])
            await self._speak_speaches(text)

    async def _speak_speaches(self, text: str) -> None:
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
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_wav, audio_bytes)

    async def _speak_edge_tts(self, text: str, voice: str) -> None:
        communicate = edge_tts.Communicate(text, voice)
        mp3_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
        mp3_bytes = b"".join(mp3_chunks)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._play_mp3, mp3_bytes)

    @staticmethod
    def _play_mp3(mp3_bytes: bytes) -> None:
        decoded = miniaudio.decode(mp3_bytes, output_format=miniaudio.SampleFormat.SIGNED16)
        audio = np.frombuffer(decoded.samples, dtype=np.int16)
        if decoded.nchannels > 1:
            audio = audio.reshape(-1, decoded.nchannels)
        sd.play(audio, samplerate=decoded.sample_rate)
        sd.wait()

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
