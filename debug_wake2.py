"""Send mic audio to openwakeword and print all events received."""
import asyncio
import queue
import threading
import numpy as np
import sounddevice as sd
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncClient
from wyoming.wake import Detect

SAMPLE_RATE = 16000
CHUNK_MS = 100
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_MS / 1000)

audio_queue: queue.Queue = queue.Queue()

def mic_callback(indata, frames, time_info, status):
    audio_queue.put(indata.tobytes())

async def main():
    print("Connecting to openwakeword...")
    async with AsyncClient.from_uri("tcp://localhost:10400") as client:
        await client.write_event(Detect(names=["hey_jarvis"]).event())
        await client.write_event(AudioStart(rate=SAMPLE_RATE, width=2, channels=1).event())
        print("Sending audio — say 'Hey Jarvis'...")

        async def send_audio():
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                                blocksize=CHUNK_SIZE, callback=mic_callback):
                while True:
                    try:
                        chunk = audio_queue.get_nowait()
                        await client.write_event(AudioChunk(
                            audio=chunk, rate=SAMPLE_RATE, width=2, channels=1).event())
                    except queue.Empty:
                        await asyncio.sleep(0.01)

        async def recv_events():
            while True:
                event = await client.read_event()
                if event is None:
                    print("Connection closed")
                    return
                print(f"EVENT: type={event.type} data={event.data}")

        await asyncio.gather(send_audio(), recv_events())

asyncio.run(main())
