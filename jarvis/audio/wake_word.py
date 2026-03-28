"""Wake word detection via Wyoming openwakeword server."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading

import numpy as np
import sounddevice as sd
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncClient
from wyoming.wake import Detect, Detection

logger = logging.getLogger(__name__)


class WakeWordDetector:
    def __init__(self, uri: str, wake_word: str, sample_rate: int = 16000, chunk_ms: int = 100) -> None:
        self.uri = uri
        self.wake_word = wake_word.lower()
        self.sample_rate = sample_rate
        self.chunk_size = int(sample_rate * chunk_ms / 1000)

    async def wait_for_wake_word(self) -> None:
        """Block until the configured wake word is detected."""
        logger.info("Listening for wake word '%s'...", self.wake_word)
        audio_queue: queue.Queue[bytes] = queue.Queue()

        def mic_callback(indata: np.ndarray, frames: int, time_info: object, status: sd.CallbackFlags) -> None:
            if status:
                logger.debug("Mic status: %s", status)
            audio_queue.put(indata.tobytes())

        async with AsyncClient.from_uri(self.uri) as client:
            await client.write_event(Detect(names=[self.wake_word]).event())
            await client.write_event(
                AudioStart(rate=self.sample_rate, width=2, channels=1).event()
            )

            with sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                blocksize=self.chunk_size,
                callback=mic_callback,
            ):
                while True:
                    # Drain mic chunks and send to Wyoming
                    try:
                        chunk_bytes = audio_queue.get_nowait()
                        await client.write_event(
                            AudioChunk(
                                audio=chunk_bytes,
                                rate=self.sample_rate,
                                width=2,
                                channels=1,
                            ).event()
                        )
                    except queue.Empty:
                        await asyncio.sleep(0.01)

                    # Check for detection event (non-blocking)
                    try:
                        event = await asyncio.wait_for(client.read_event(), timeout=0.01)
                    except asyncio.TimeoutError:
                        continue

                    if event is None:
                        continue

                    detection = Detection.from_event(event)
                    if detection is not None:
                        detected_name = (detection.name or "").lower()
                        logger.info("Wake word detected: %s", detection.name)
                        if self.wake_word in detected_name or detected_name in self.wake_word:
                            return
