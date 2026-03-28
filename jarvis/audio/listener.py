"""Speech-to-text via Wyoming faster-whisper server with silence-based VAD."""

from __future__ import annotations

import asyncio
import logging
import queue

import numpy as np
import sounddevice as sd
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

logger = logging.getLogger(__name__)


class SpeechListener:
    def __init__(
        self,
        uri: str,
        sample_rate: int = 16000,
        chunk_ms: int = 100,
        silence_threshold: int = 200,
        silence_duration: float = 1.5,
    ) -> None:
        self.uri = uri
        self.sample_rate = sample_rate
        self.chunk_size = int(sample_rate * chunk_ms / 1000)
        self.silence_threshold = silence_threshold
        self.silence_chunks = int(silence_duration * 1000 / chunk_ms)

    async def listen(self) -> str:
        """Capture speech from microphone until silence, return transcript."""
        logger.info("Listening for speech...")
        audio_queue: queue.Queue[bytes] = queue.Queue()

        def mic_callback(indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
            if status:
                logger.debug("Mic status: %s", status)
            audio_queue.put(indata.tobytes())

        async with AsyncClient.from_uri(self.uri) as client:
            await client.write_event(Transcribe().event())
            await client.write_event(
                AudioStart(rate=self.sample_rate, width=2, channels=1).event()
            )

            silent_chunks = 0
            got_speech = False

            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.chunk_size,
                callback=mic_callback,
            ):
                while True:
                    try:
                        chunk_bytes = audio_queue.get(timeout=0.2)
                    except queue.Empty:
                        continue

                    audio_array = np.frombuffer(chunk_bytes, dtype=np.int16)
                    rms = int(np.sqrt(np.mean(audio_array.astype(np.float32) ** 2)))

                    await client.write_event(
                        AudioChunk(
                            audio=chunk_bytes,
                            rate=self.sample_rate,
                            width=2,
                            channels=1,
                        ).event()
                    )

                    if rms > self.silence_threshold:
                        got_speech = True
                        silent_chunks = 0
                    elif got_speech:
                        silent_chunks += 1
                        if silent_chunks >= self.silence_chunks:
                            break

            await client.write_event(AudioStop().event())

            # Read transcript
            while True:
                event = await asyncio.wait_for(client.read_event(), timeout=30.0)
                if event is None:
                    break
                transcript = Transcript.from_event(event)
                if transcript is not None:
                    text = transcript.text.strip()
                    logger.info("Transcript: %r", text)
                    return text

        return ""
