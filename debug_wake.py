import logging, asyncio, yaml
from dotenv import load_dotenv
load_dotenv()
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
from jarvis.audio.wake_word import WakeWordDetector
cfg = yaml.safe_load(open("config.yaml"))
d = WakeWordDetector(cfg["wyoming"]["wake_uri"], cfg["wyoming"]["wake_word"])
asyncio.run(d.wait_for_wake_word())
print("DETECTED!")
