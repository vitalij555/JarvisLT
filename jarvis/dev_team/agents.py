"""API-based agents for the dev team pipeline.

DevPMAgent      — translates voice request into spec + feature plan; handles retries.
SalesManagerAgent — post-success only; writes README.md + CHANGELOG.md, returns voice summary.

Both use direct gpt-4o-mini OpenAI calls (no MCP, no tools) via run_in_executor.
Each call uses a fresh context — no conversation bleed between agents.
"""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

_MODEL = "gpt-4o-mini"


async def _llm_call(system: str, user: str, max_tokens: int = 2048) -> str:
    """Single-turn OpenAI call. Runs synchronous SDK in executor."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    loop = asyncio.get_running_loop()

    def _call() -> str:
        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    return await loop.run_in_executor(None, _call)


# ── Project Manager ────────────────────────────────────────────────────────────

_PM_PLAN_SYSTEM = """You are the Project Manager for a local AI dev team.
Your job: turn an informal request into a formal spec and a concrete feature breakdown.

Output JSON with these keys:
  project_name: string (snake_case identifier, e.g. "word_count_cli")
  language: string (e.g. "python", "typescript")
  description: string (2-3 sentences on what the tool does)
  success_criteria: list of strings (testable acceptance criteria)
  features: list of objects, each with:
    name: string (snake_case, e.g. "cli_arg_parser")
    description: string (what to implement — be specific about inputs, outputs, edge cases)
    assigned_to: string ("claude" or "codex" — alternate if both available, assign complex features to claude)

Rules:
- Break the project into 2-5 focused features. Each feature must be independently implementable.
- If only one worker is available, assign all features to it.
- Features must not depend on each other being completed first (parallel-safe).
- Output only valid JSON, no markdown fences."""

_PM_REASSIGN_SYSTEM = """You are the Project Manager for a local AI dev team handling a retry.
Tests have failed. Your job: identify which features need to be fixed and reassign them.

Output JSON with ONE key:
  features: list of objects (only the features that need to be redone), each with:
    name: string (must match an existing feature name exactly)
    description: string (updated description — include specific fixes needed based on the test failures)
    assigned_to: string ("claude" or "codex")

Output only valid JSON, no markdown fences."""


class DevPMAgent:
    async def plan(
        self, raw_request: str, folder: str, available_workers: list[str]
    ) -> str:
        workers_note = (
            f"Available CLI workers: {', '.join(available_workers)}. "
            "Assign features only to available workers."
            if available_workers
            else "No CLI workers detected — assign all features to 'codex' as placeholder."
        )
        user = (
            f"TARGET FOLDER: {folder}\n\n"
            f"USER REQUEST: {raw_request}\n\n"
            f"{workers_note}\n\n"
            "Produce the spec + feature plan JSON."
        )
        result = await _llm_call(_PM_PLAN_SYSTEM, user)
        logger.info("DevPMAgent: planned project in %s", folder)
        return result

    async def reassign(
        self,
        spec_json: str,
        arch_doc: str,
        test_failure: str,
        available_workers: list[str],
    ) -> str:
        user = (
            f"ORIGINAL SPEC:\n{spec_json}\n\n"
            f"ARCHITECTURE:\n{arch_doc[:2000]}\n\n"
            f"TEST FAILURES:\n{test_failure}\n\n"
            f"Available workers: {', '.join(available_workers)}\n\n"
            "Which features need to be redone? Output the revised feature list."
        )
        result = await _llm_call(_PM_REASSIGN_SYSTEM, user)
        logger.info("DevPMAgent: reassigned features after test failure")
        return result


# ── Sales Manager (post-success deliverables) ─────────────────────────────────

_SALES_SYSTEM = """You are the Sales Manager / Technical Writer for a dev team.
The project has been successfully built. Your job:
1. Write a README.md for the project — include: what it does, installation, usage examples, feature list.
2. Write a CHANGELOG.md — initial version entry with date and feature list.
3. Write the files directly to the project folder.
4. Return a concise voice announcement (1-2 sentences) summarising what was built.

Output JSON with these keys:
  readme_content: string (full README.md content in Markdown)
  changelog_content: string (full CHANGELOG.md content in Markdown)
  voice_summary: string (spoken announcement, max 30 words, e.g. "Built word_count_cli with 3 features including a CLI parser and file reader. README and changelog are ready in the folder.")

Output only valid JSON, no markdown fences."""


class SalesManagerAgent:
    async def generate_deliverables(
        self,
        spec_json: str,
        arch_doc: str,
        feature_results: list[str],
        folder: str,
    ) -> str:
        """Generate README + CHANGELOG, write to folder, return voice_summary."""
        import json
        from pathlib import Path

        features_summary = "\n".join(
            f"- {r}" for r in feature_results if r
        ) or "(no feature details)"

        user = (
            f"PROJECT SPEC:\n{spec_json}\n\n"
            f"ARCHITECTURE:\n{arch_doc[:2000]}\n\n"
            f"COMPLETED FEATURES:\n{features_summary}\n\n"
            f"TARGET FOLDER: {folder}\n\n"
            "Generate the README, CHANGELOG, and voice summary."
        )
        raw = await _llm_call(_SALES_SYSTEM, user, max_tokens=3000)
        logger.info("SalesManagerAgent: generated deliverables for %s", folder)

        # Parse and write files
        try:
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
            folder_path = Path(folder)
            if data.get("readme_content"):
                (folder_path / "README.md").write_text(
                    data["readme_content"], encoding="utf-8"
                )
            if data.get("changelog_content"):
                (folder_path / "CHANGELOG.md").write_text(
                    data["changelog_content"], encoding="utf-8"
                )
            return data.get("voice_summary", "Project complete.")
        except Exception as exc:
            logger.warning("SalesManagerAgent could not write deliverables: %s", exc)
            return "Project complete. Could not write README."
