"""Main assistant orchestration loop."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis.audio.listener import SpeechListener
from jarvis.audio.speaker import Speaker
from jarvis.audio.wake_word import WakeWordDetector
from jarvis.connectors.home_assistant import HomeAssistantConnector
from jarvis.connectors.places_tools import PLACES_TOOLS, PlacesToolHandler
from jarvis.connectors.search_tools import SEARCH_TOOLS, SearchToolHandler
from jarvis.connectors.web_tools import WEB_TOOLS, WebToolHandler
from jarvis.llm.claude_client import LLMClient
from jarvis.llm.memory import ConversationMemory
from jarvis.memory.memory_manager import MemoryManager
from jarvis.memory.memory_tools import MEMORY_TOOLS, MemoryToolHandler
from jarvis.dev_team.dev_team_tools import DEV_TEAM_TOOLS, DevTeamToolHandler
from jarvis.outsourcing.outsourcing_tools import OUTSOURCING_TOOLS, OutsourcingToolHandler
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
            speech_timeout=audio_cfg.get("speech_timeout", 8.0),
        )
        self._conversation_timeout = audio_cfg.get("conversation_timeout", 60.0)

        self.speaker = Speaker(
            base_url=speaches_cfg["base_url"],
            model=speaches_cfg["tts_model"],
            voice=speaches_cfg["tts_voice"],
            multilingual_voices=speaches_cfg.get("multilingual_voices", {}),
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
        self.llm.register_local_tools(PLACES_TOOLS, PlacesToolHandler())

        self.pending_notifications: asyncio.Queue = asyncio.Queue()

        outsourcing_cfg = config.get("outsourcing", {})
        if outsourcing_cfg.get("enabled", False):
            outsourcing_handler = OutsourcingToolHandler(
                outsourcing_cfg, self.llm, self.pending_notifications
            )
            self.llm.register_local_tools(OUTSOURCING_TOOLS, outsourcing_handler)
            logger.info("Outsourcing department enabled")

        dev_team_cfg = config.get("dev_team", {})
        if dev_team_cfg.get("enabled", False):
            dev_team_handler = DevTeamToolHandler(dev_team_cfg, self.pending_notifications)
            self.llm.register_local_tools(DEV_TEAM_TOOLS, dev_team_handler)
            logger.info("Dev team enabled")

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

                # Inner conversation loop — stays active until timeout or error.
                # First listen uses the short speech_timeout (8s).
                # After each response, follow-ups use conversation_timeout (60s).
                current_timeout = None  # None = use listener's default speech_timeout
                in_conversation = False

                while True:
                    text = await self.listener.listen(timeout=current_timeout)
                    if not text.strip():
                        if in_conversation:
                            logger.info("Conversation timed out, going back to sleep.")
                            await self.speaker.speak(
                                "Going to sleep. Say 'Hey Jarvis' to wake me up."
                            )
                        else:
                            logger.info("No speech detected, going back to sleep.")
                        break

                    logger.info("User said: %r", text)
                    self.memory.add_turn("user", text)

                    # Auto-recall: search long-term memory for context relevant to
                    # this request and inject it before the LLM sees the message.
                    # The raw `text` is stored in conversation history; only the
                    # enriched version is sent to the LLM — no history pollution.
                    try:
                        recall = await self.memory_manager.recall_about(text)
                        _no_result = recall.startswith("I don't have")
                    except Exception:
                        recall = ""
                        _no_result = True

                    if recall and not _no_result:
                        llm_input = (
                            f"[Relevant memory — use if helpful for this request]\n"
                            f"{recall}\n\n"
                            f"[User request]\n{text}"
                        )
                        logger.debug("Auto-recall injected %d chars of memory context", len(recall))
                    else:
                        llm_input = text

                    response = await self.llm.chat_async(llm_input, self.memory)
                    logger.info("Jarvis: %r", response)

                    self.memory.add_turn("assistant", response)
                    self.memory.save()

                    asyncio.create_task(
                        self.memory_manager.summarize_and_store(self.memory.get_context())
                    )

                    await self.speaker.speak(response)

                    # Announce any background notifications (e.g. new job opportunities)
                    while not self.pending_notifications.empty():
                        note = self.pending_notifications.get_nowait()
                        if note["type"] == "opportunity":
                            await self.speaker.speak(
                                f"By the way, I found a promising job. "
                                f"{note['preview']}. Say 'show opportunities' to review."
                            )
                        elif note["type"] == "auth_required":
                            await self.speaker.speak(
                                f"{note['portal'].capitalize()} requires login for job scanning."
                            )
                        elif note["type"] == "dev_team_done":
                            if note.get("success"):
                                summary = note.get("summary", "Project complete.")
                                await self.speaker.speak(
                                    f"Your dev team has finished. {summary}"
                                )
                            else:
                                name = note.get("project_name", "the project")
                                retries = note.get("retries", 0)
                                error = note.get("error", "unknown error")
                                await self.speaker.speak(
                                    f"Your dev team ran into trouble with {name}. "
                                    f"They tried {retries} times but couldn't complete it. "
                                    f"{error}"
                                )

                    # Switch to long timeout for follow-ups
                    current_timeout = self._conversation_timeout
                    in_conversation = True

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
