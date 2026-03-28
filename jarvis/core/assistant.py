"""Main assistant orchestration loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis.audio.listener import SpeechListener
from jarvis.audio.speaker import Speaker
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.connectors.home_assistant import HomeAssistantConnector
from jarvis.connectors.search_tools import SEARCH_TOOLS, SearchToolHandler
from jarvis.connectors.web_tools import WEB_TOOLS, WebToolHandler
from jarvis.llm.claude_client import LLMClient
from jarvis.llm.memory import ConversationMemory
from jarvis.memory.memory_manager import MemoryManager
from jarvis.memory.memory_tools import MEMORY_TOOLS, MemoryToolHandler
from jarvis.scheduler.task_runner import TaskRunner
from jarvis.scheduler.task_tools import TASK_TOOLS, TaskToolHandler

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

        self.memory_manager = MemoryManager(config)
        self.memory_manager.set_llm(self.llm)
        self.llm.register_local_tools(MEMORY_TOOLS, MemoryToolHandler(self.memory_manager))

        self.task_runner = TaskRunner(
            static_tasks=config.get("scheduled_tasks", {}),
            llm=self.llm,
            db_path=config.get("task_db", {}).get("path", "jarvis_tasks.db"),
            memory_manager=self.memory_manager,
        )
        self.llm.register_local_tools(TASK_TOOLS, TaskToolHandler(self.task_runner))
        self.llm.register_local_tools(WEB_TOOLS, WebToolHandler())
        self.llm.register_local_tools(SEARCH_TOOLS, SearchToolHandler())

    async def run(self) -> None:
        """Main loop: wake word → listen → think → speak → repeat."""
        await self.llm.start()
        await self.memory_manager.init()
        await self.task_runner.start()
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

                # Async background summarisation — does not block the loop
                asyncio.create_task(
                    self.memory_manager.summarize_and_store(self.memory.get_context())
                )

                await self.speaker.speak(response)

            except KeyboardInterrupt:
                logger.info("Shutting down.")
                await self.task_runner.stop()
                await self.llm.stop()
                await self.memory_manager.close()
                break
            except Exception as exc:
                logger.exception("Unexpected error in main loop: %s", exc)
                try:
                    await self.speaker.speak("Sorry, I encountered an error.")
                except Exception:
                    pass
