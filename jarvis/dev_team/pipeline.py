"""DevTeamPipeline — background orchestrator for the voice-triggered coding agent.

Flow:
  Phase 0: PM plans spec + features
  Phase 1: Architects design (Claude → Codex, sequential)
  Phase 2: Developers implement (parallel)
  Phase 3: Tester runs tests
  On pass: Sales Manager writes README + CHANGELOG, notify voice loop
  On fail: PM reassigns + retry (up to max_retries), then notify failure
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from jarvis.dev_team.agents import DevPMAgent, SalesManagerAgent
from jarvis.dev_team.project_store import (
    FEAT_DONE, FEAT_FAILED, FEAT_PENDING, FEAT_RUNNING,
    STATUS_ARCHITECTING, STATUS_DEVELOPING, STATUS_DONE,
    STATUS_FAILED, STATUS_PLANNING, STATUS_RETRYING, STATUS_TESTING,
    DevFeature, DevProject, ProjectStore, new_id,
)
from jarvis.outsourcing.workers.claude_file_worker import ClaudeFileWorker
from jarvis.outsourcing.workers.codex_worker import CodexWorker

logger = logging.getLogger(__name__)

_ARCH1_PROMPT = """\
You are Lead Architect. Your job: design the full project and scaffold it on disk.

TARGET FOLDER: {folder}

PROJECT SPEC:
{spec}

Tasks:
1. Analyse the spec and design the architecture.
2. Write ARCHITECTURE.md into {folder} containing:
   - Project overview
   - Directory/file tree
   - Module responsibilities
   - Key interfaces and data contracts
   - Technology choices
3. Create the directory structure and any empty __init__.py or index files needed.

Write all files directly to {folder}. Do not ask for confirmation.
"""

_ARCH2_PROMPT = """\
You are Architecture Reviewer. Your job: review, improve, and scaffold stub files.

TARGET FOLDER: {folder}

PROJECT SPEC:
{spec}

EXISTING ARCHITECTURE (ARCHITECTURE.md):
{arch_doc}

Tasks:
1. Review the architecture critically — add a "## Review Notes" section to ARCHITECTURE.md with any gaps or improvements.
2. Create stub files (correct imports, class/function signatures, docstrings — no implementation logic yet) for every module listed in the architecture.

Work in {folder}. Do not ask for confirmation.
"""

_DEV_PROMPT = """\
You are a Senior Developer implementing one feature.

TARGET FOLDER: {folder}

PROJECT SPEC:
{spec}

ARCHITECTURE:
{arch_doc}

YOUR FEATURE: {feature_name}
{feature_description}

{retry_section}\
Rules:
- Implement ONLY your assigned feature. Do not modify files owned by other features.
- Follow the interfaces defined in the architecture exactly.
- Write tests for your feature in {folder}/tests/test_{feature_name}.py.
- After writing, verify the code has no import errors.

Work in {folder}. Do not ask for confirmation.
"""

_TESTER_PROMPT = """\
You are the QA Engineer.

TARGET FOLDER: {folder}

PROJECT SPEC:
{spec}

ARCHITECTURE:
{arch_doc}

Tasks:
1. Read all source files in {folder}.
2. Run all tests (use pytest if Python, or the appropriate test runner).
3. Manually verify the tool against each success criterion in the spec.
4. Write {folder}/test_report.json with EXACTLY this structure:
   {{"passed": true/false, "failures": ["description of failure 1", ...]}}
5. If all tests pass: set passed=true and failures=[].

Work in {folder}. Do not ask for confirmation.
"""


class DevTeamPipeline:
    def __init__(
        self,
        store: ProjectStore,
        pending_queue: asyncio.Queue,
        config: dict,
    ) -> None:
        self._store = store
        self._queue = pending_queue
        self._config = config
        self._pm = DevPMAgent()
        self._sales = SalesManagerAgent()
        timeout = config.get("worker_timeout", 600)
        self._claude = ClaudeFileWorker(timeout=timeout)
        self._codex = CodexWorker(timeout=timeout)

    async def start_project(self, raw_request: str, folder: str) -> str:
        """Create project record, fire background pipeline, return project_id immediately."""
        project = DevProject(
            id=new_id(),
            folder=folder,
            raw_request=raw_request,
            max_retries=self._config.get("max_retries", 3),
        )
        await self._store.create_project(project)
        asyncio.create_task(self._run_pipeline(project.id))
        logger.info("DevTeam: project %s started, folder=%s", project.id[:8], folder)
        return project.id

    # ── Available workers ──────────────────────────────────────────────────────

    async def _available_workers(self) -> list[str]:
        result = []
        if await self._claude.is_available():
            result.append("claude")
        if await self._codex.is_available():
            result.append("codex")
        return result

    async def _run_worker(
        self, worker_name: str, prompt: str, cwd: str
    ) -> tuple[bool, str]:
        """Run a named worker in cwd. Returns (success, output_or_error)."""
        if worker_name == "claude":
            result = await self._claude.run(prompt, cwd=cwd)
        else:
            result = await self._codex.run(prompt, cwd=cwd)
        return result.success, result.output if result.success else result.error

    # ── Main pipeline ──────────────────────────────────────────────────────────

    async def _run_pipeline(self, project_id: str) -> None:
        project = await self._store.get_project(project_id)
        if not project:
            logger.error("DevTeam: project %s not found", project_id)
            return

        folder = project.folder
        Path(folder).mkdir(parents=True, exist_ok=True)

        try:
            # ── Phase 0: PM planning ──────────────────────────────────────────
            available = await self._available_workers()
            if not available:
                await self._fail(project, "No CLI workers available (claude/codex not in PATH)")
                return

            spec_raw = await self._pm.plan(project.raw_request, folder, available)
            spec_data = self._parse_json(spec_raw)
            project_name = spec_data.get("project_name", project_id[:8])
            features_data = spec_data.get("features", [])

            await self._store.update_project(
                project_id,
                name=project_name,
                spec=spec_raw,
                status=STATUS_PLANNING,
            )

            features = [
                DevFeature(
                    id=new_id(),
                    project_id=project_id,
                    name=f["name"],
                    description=f["description"],
                    assigned_to=f.get("assigned_to", available[0]),
                )
                for f in features_data
            ]
            for feat in features:
                await self._store.save_feature(feat)

            # ── Phase 1: Architecture ─────────────────────────────────────────
            await self._store.update_project(project_id, status=STATUS_ARCHITECTING)

            arch1_prompt = _ARCH1_PROMPT.format(folder=folder, spec=spec_raw)
            ok, out = await self._run_worker("claude" if "claude" in available else "codex",
                                             arch1_prompt, folder)
            if not ok:
                logger.warning("DevTeam: Arch1 failed: %s", out[:200])

            arch_doc = self._read_arch_doc(folder)

            arch2_prompt = _ARCH2_PROMPT.format(folder=folder, spec=spec_raw, arch_doc=arch_doc)
            await self._run_worker("codex" if "codex" in available else "claude",
                                   arch2_prompt, folder)

            arch_doc = self._read_arch_doc(folder)
            await self._store.update_project(project_id, arch_doc=arch_doc)

            # ── Retry loop (Phase 2 + 3) ──────────────────────────────────────
            last_failure = ""
            max_retries = project.max_retries

            for attempt in range(max_retries + 1):
                # Phase 2: Development (parallel)
                await self._store.update_project(project_id, status=STATUS_DEVELOPING)
                pending = [
                    f for f in await self._store.get_features(project_id)
                    if f.status in (FEAT_PENDING, FEAT_FAILED)
                ]
                if pending:
                    await asyncio.gather(
                        *[self._run_feature(f, spec_raw, arch_doc, folder, last_failure)
                          for f in pending],
                        return_exceptions=True,
                    )

                # Phase 3: Testing
                await self._store.update_project(project_id, status=STATUS_TESTING)
                test_prompt = _TESTER_PROMPT.format(
                    folder=folder, spec=spec_raw, arch_doc=arch_doc
                )
                _, test_out = await self._run_worker(
                    "codex" if "codex" in available else "claude", test_prompt, folder
                )

                passed, last_failure = self._check_tests(folder, test_out)

                if passed:
                    # Phase 4: Sales Manager deliverables
                    all_features = await self._store.get_features(project_id)
                    feature_results = [
                        f"{f.name}: {(f.output or '')[:200]}" for f in all_features
                    ]
                    voice_summary = await self._sales.generate_deliverables(
                        spec_raw, arch_doc, feature_results, folder
                    )
                    await self._store.update_project(project_id, status=STATUS_DONE)
                    await self._queue.put({
                        "type": "dev_team_done",
                        "success": True,
                        "project_name": project_name,
                        "folder": folder,
                        "summary": voice_summary,
                    })
                    logger.info("DevTeam: project %s DONE", project_id[:8])
                    return

                if attempt >= max_retries:
                    break

                # Retry: PM reassigns only failing features
                await self._store.update_project(
                    project_id, status=STATUS_RETRYING, retries=attempt + 1
                )
                logger.info(
                    "DevTeam: project %s retry %d/%d — %s",
                    project_id[:8], attempt + 1, max_retries, last_failure[:100]
                )
                revised_raw = await self._pm.reassign(
                    spec_raw, arch_doc, last_failure, available
                )
                revised_data = self._parse_json(revised_raw)
                revised_features = revised_data.get("features", [])

                existing_feats = {f.name: f for f in await self._store.get_features(project_id)}
                for f_data in revised_features:
                    feat = existing_feats.get(f_data["name"])
                    if feat:
                        feat.status = FEAT_PENDING
                        feat.attempt += 1
                        feat.assigned_to = f_data.get("assigned_to", feat.assigned_to)
                        feat.description = f_data.get("description", feat.description)
                        await self._store.save_feature(feat)

            # All retries exhausted
            await self._fail(project, last_failure, retries=max_retries)

        except Exception as exc:
            logger.exception("DevTeam pipeline error for %s: %s", project_id[:8], exc)
            await self._fail(project, str(exc))

    async def _run_feature(
        self,
        feature: DevFeature,
        spec: str,
        arch_doc: str,
        folder: str,
        last_failure: str,
    ) -> None:
        retry_section = (
            f"PREVIOUS ATTEMPT FAILED. Fix these issues:\n{last_failure}\n\n"
            if last_failure else ""
        )
        prompt = _DEV_PROMPT.format(
            folder=folder,
            spec=spec,
            arch_doc=arch_doc,
            feature_name=feature.name,
            feature_description=feature.description,
            retry_section=retry_section,
        )
        feature.status = FEAT_RUNNING
        await self._store.save_feature(feature)

        ok, out = await self._run_worker(feature.assigned_to, prompt, folder)

        feature.status = FEAT_DONE if ok else FEAT_FAILED
        feature.output = out[:2000] if ok else None
        feature.error = out[:1000] if not ok else None
        await self._store.save_feature(feature)

    async def _fail(
        self, project: DevProject, error: str, retries: int = 0
    ) -> None:
        await self._store.update_project(
            project.id, status=STATUS_FAILED, error=error, retries=retries
        )
        await self._queue.put({
            "type": "dev_team_done",
            "success": False,
            "project_name": project.name or project.id[:8],
            "folder": project.folder,
            "error": error[:200],
            "retries": retries,
        })
        logger.error("DevTeam: project %s FAILED: %s", project.id[:8], error[:100])

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _read_arch_doc(folder: str) -> str:
        arch_path = Path(folder) / "ARCHITECTURE.md"
        if arch_path.exists():
            return arch_path.read_text(encoding="utf-8", errors="replace")
        return "(No ARCHITECTURE.md found yet)"

    @staticmethod
    def _check_tests(folder: str, tester_output: str) -> tuple[bool, str]:
        """Read test_report.json if it exists; fall back to scanning tester output."""
        report_path = Path(folder) / "test_report.json"
        if report_path.exists():
            try:
                data = json.loads(report_path.read_text(encoding="utf-8"))
                passed = bool(data.get("passed", False))
                failures = data.get("failures", [])
                return passed, "; ".join(failures) if failures else ""
            except Exception:
                pass
        # Fallback: scan output
        lower = tester_output.lower()
        failed = any(kw in lower for kw in ("failed", "error", "traceback", "assert"))
        return not failed, tester_output[:500] if failed else ""

    @staticmethod
    def _parse_json(raw: str) -> dict:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        try:
            return json.loads(raw)
        except Exception as exc:
            logger.warning("DevTeam: could not parse JSON: %s — raw: %s", exc, raw[:200])
            return {}
