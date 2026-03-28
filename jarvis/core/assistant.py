"""Main assistant orchestration loop."""

from __future__ import annotations

import logging
from typing import Any

from jarvis.audio.listener import SpeechListener
from jarvis.audio.speaker import Speaker
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.connectors.home_assistant import HomeAssistantConnector
from jarvis.llm.claude_client import LLMClient
from jarvis.llm.memory import ConversationMemory

logger = logging.getLogger(__name__)


class Assistant:
    def __init__(self, config: dict[str, Any]) -> None:
        wyoming_cfg = config["wyoming"]
        speaches_cfg = config["speaches"]
        audio_cfg = config["audio"]
        llm_cfg = config["llm"]
        memory_cfg = config["memory"]
        ha_cfg = config.get("home_assistant", {})

        self.wake_detector = WakeWordDetector(
            uri=wyoming_cfg["wake_uri"],
            wake_word=wyoming_cfg["wake_word"],
            sample_rate=audio_cfg["sample_rate"],
            chunk_ms=audio_cfg["chunk_ms"],
        )

        self.listener = SpeechListener(
            uri=wyoming_cfg["stt_uri"],
            sample_rate=audio_cfg["sample_rate"],
            chunk_ms=audio_cfg["chunk_ms"],
            silence_threshold=audio_cfg["silence_threshold"],
            silence_duration=audio_cfg["silence_duration"],
        )

        self.speaker = Speaker(
            base_url=speaches_cfg["base_url"],
            model=speaches_cfg["tts_model"],
            voice=speaches_cfg["tts_voice"],
        )

        self.memory = ConversationMemory(
            max_turns=memory_cfg["max_turns"],
            persist_path=memory_cfg["persist_path"],
        )

        ha_connector: HomeAssistantConnector | None = None
        if ha_cfg.get("enabled") and ha_cfg.get("url"):
            import os
            token = ha_cfg.get("token") or os.environ.get("HA_TOKEN", "")
            if token:
                ha_connector = HomeAssistantConnector(url=ha_cfg["url"], token=token)
                logger.info("Home Assistant connector enabled: %s", ha_cfg["url"])
            else:
                logger.warning("HA enabled but HA_TOKEN not set — skipping HA connector")

        self.llm = LLMClient(
            model=llm_cfg["model"],
            system_prompt=llm_cfg["system_prompt"],
            max_tokens=llm_cfg.get("max_tokens", 1024),
            mcp_servers=config.get("mcp_servers"),
            ha_connector=ha_connector,
        )

    async def run(self) -> None:
        """Main loop: wake word → listen → think → speak → repeat."""
        await self.llm.start()
        logger.info("Jarvis is ready. Waiting for wake word...")
        await self.speaker.speak("Jarvis online. Say 'Hey Jarvis' to activate.")

        while True:
            try:
                await self.wake_detector.wait_for_wake_word()
                await self.speaker.speak("Yes?")

                text = await self.listener.listen()
                if not text.strip():
                    logger.info("No speech detected, going back to sleep.")
                    continue

                logger.info("User said: %r", text)
                self.memory.add_turn("user", text)

                response = await self.llm.chat_async(text, self.memory)
                logger.info("Jarvis: %r", response)

                self.memory.add_turn("assistant", response)
                self.memory.save()

                await self.speaker.speak(response)

            except KeyboardInterrupt:
                logger.info("Shutting down.")
                await self.llm.stop()
                break
            except Exception as exc:
                logger.exception("Unexpected error in main loop: %s", exc)
                try:
                    await self.speaker.speak("Sorry, I encountered an error.")
                except Exception:
                    pass
