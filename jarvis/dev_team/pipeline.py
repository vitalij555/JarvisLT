"""DevTeamPipeline — background orchestrator for the voice-triggered coding agent.

Flow:
  Phase 0: PM plans spec + features
  Phase 1: Architects design (Claude → Codex, sequential)
  Phase 2: Development + peer review (parallel across features, sequential within each):
             implement → peer reviews → author revises  (one cycle, not a loop)
  Phase 3: Testing
  On pass: Sales Manager writes README + CHANGELOG, notify voice loop
  On fail: developers decide "fix code or fix test", re-test (up to max_retries=4)
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

# ── Prompt templates ───────────────────────────────────────────────────────────

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
1. Review the architecture critically — add a "## Review Notes" section to ARCHITECTURE.md
   with any gaps, missing edge cases, or improvements.
2. Create stub files (correct imports, class/function signatures, docstrings —
   no implementation logic yet) for every module listed in the architecture.

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

Rules:
- Implement ONLY your assigned feature. Do not modify files owned by other features.
- Follow the interfaces defined in the architecture exactly.
- Write tests for your feature in {folder}/tests/test_{feature_name}.py.
- After writing, verify the code has no import errors.

Work in {folder}. Do not ask for confirmation.
"""

_REVIEW_PROMPT = """\
You are a Code Reviewer. A colleague has implemented a feature — review their work.

TARGET FOLDER: {folder}

FEATURE UNDER REVIEW: {feature_name}
{feature_description}

ARCHITECTURE (for reference):
{arch_doc}

Tasks:
1. Read all source files related to this feature in {folder}.
2. Write your review to {review_file}. Structure it as:

   ## What works well
   (brief, honest)

   ## Issues found
   (bugs, missing edge cases, wrong interface usage, style problems —
    be specific: reference file names and approximate line numbers)

   ## Suggestions
   (concrete improvements the author should consider)

IMPORTANT: Do NOT modify any source files — write ONLY the review file.
Work in {folder}. Do not ask for confirmation.
"""

_REVISE_PROMPT = """\
You are a Senior Developer. A peer has reviewed your implementation of a feature.
Read the review and decide what to address — you are the author and have final say.

TARGET FOLDER: {folder}

YOUR FEATURE: {feature_name}
{feature_description}

PEER REVIEW (from {review_file}):
{review_content}

Tasks:
1. Read each issue and suggestion in the review.
2. Apply the changes you agree with.
3. For anything you disagree with or choose not to change, append a brief note
   to {review_file} under a "## Author Response" section explaining your reasoning.

This is your final implementation — make it solid.
Work in {folder}. Do not ask for confirmation.
"""

_FIX_AFTER_TEST_PROMPT = """\
You are a Senior Developer. Tests have failed after your feature was submitted.
Your job: investigate and fix — either the code or the test, whichever is wrong.

TARGET FOLDER: {folder}

YOUR FEATURE: {feature_name}
{feature_description}

ARCHITECTURE:
{arch_doc}

TEST FAILURES:
{test_failure}

Tasks:
1. Read the failing test output above. Identify which failures relate to your feature.
2. Read your implementation files AND the failing test files in {folder}/tests/.
3. Make a judgment call:
   - If your implementation is wrong: fix the code.
   - If the test has wrong expectations or tests the wrong thing: fix the test.
   - If unsure: fix the code (safer default).
4. Make only the necessary change — do not rewrite unrelated parts.

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
3. Verify the tool against each success criterion in the spec.
4. Write {folder}/test_report.json with EXACTLY this structure:
   {{"passed": true/false, "failures": ["description of failure 1", ...]}}
   If all tests pass: set passed=true and failures=[].

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
            max_retries=self._config.get("max_retries", 4),
        )
        await self._store.create_project(project)
        asyncio.create_task(self._run_pipeline(project.id))
        logger.info("DevTeam: project %s started, folder=%s", project.id[:8], folder)
        return project.id

    # ── Worker dispatch ────────────────────────────────────────────────────────

    async def _available_workers(self) -> list[str]:
        result = []
        if await self._claude.is_available():
            result.append("claude")
        if await self._codex.is_available():
            result.append("codex")
        return result

    async def _run_worker(self, worker_name: str, prompt: str, cwd: str) -> tuple[bool, str]:
        if worker_name == "claude":
            result = await self._claude.run(prompt, cwd=cwd)
        else:
            result = await self._codex.run(prompt, cwd=cwd)
        return result.success, result.output if result.success else result.error

    @staticmethod
    def _peer_of(assignee: str, available: list[str]) -> str | None:
        """Return the peer reviewer worker name, or None if no peer available."""
        if assignee == "claude" and "codex" in available:
            return "codex"
        if assignee == "codex" and "claude" in available:
            return "claude"
        return None

    # ── Main pipeline ──────────────────────────────────────────────────────────

    async def _run_pipeline(self, project_id: str) -> None:
        project = await self._store.get_project(project_id)
        if not project:
            logger.error("DevTeam: project %s not found", project_id)
            return

        folder = project.folder
        Path(folder).mkdir(parents=True, exist_ok=True)
        Path(folder, "reviews").mkdir(exist_ok=True)

        try:
            # ── Phase 0: PM planning ──────────────────────────────────────────
            available = await self._available_workers()
            if not available:
                await self._fail(project, "No CLI workers available (claude/codex not in PATH)")
                return

            spec_raw = await self._pm.plan(project.raw_request, folder, available)
            spec_data = self._parse_json(spec_raw)
            project_name = spec_data.get("project_name", project_id[:8])

            await self._store.update_project(
                project_id, name=project_name, spec=spec_raw, status=STATUS_PLANNING
            )

            features = [
                DevFeature(
                    id=new_id(),
                    project_id=project_id,
                    name=f["name"],
                    description=f["description"],
                    assigned_to=f.get("assigned_to", available[0]),
                )
                for f in spec_data.get("features", [])
            ]
            for feat in features:
                await self._store.save_feature(feat)

            # ── Phase 1: Architecture (Claude designs → Codex reviews + scaffolds) ──
            await self._store.update_project(project_id, status=STATUS_ARCHITECTING)

            arch1_worker = "claude" if "claude" in available else "codex"
            _, out = await self._run_worker(
                arch1_worker, _ARCH1_PROMPT.format(folder=folder, spec=spec_raw), folder
            )
            if not _:
                logger.warning("DevTeam: Arch1 warning: %s", out[:200])

            arch_doc = self._read_arch_doc(folder)

            arch2_worker = "codex" if "codex" in available else "claude"
            await self._run_worker(
                arch2_worker,
                _ARCH2_PROMPT.format(folder=folder, spec=spec_raw, arch_doc=arch_doc),
                folder,
            )
            arch_doc = self._read_arch_doc(folder)
            await self._store.update_project(project_id, arch_doc=arch_doc)

            # ── Phase 2+3: Develop → review → revise, then test with retries ──
            last_failure = ""
            max_retries = project.max_retries

            # Initial development pass (implement + peer review + author revision)
            await self._store.update_project(project_id, status=STATUS_DEVELOPING)
            all_features = await self._store.get_features(project_id)
            await asyncio.gather(
                *[self._develop_feature(f, spec_raw, arch_doc, folder, available)
                  for f in all_features],
                return_exceptions=True,
            )

            # Test + fix loop (up to max_retries)
            for attempt in range(max_retries + 1):
                await self._store.update_project(project_id, status=STATUS_TESTING)
                _, test_out = await self._run_worker(
                    "codex" if "codex" in available else "claude",
                    _TESTER_PROMPT.format(folder=folder, spec=spec_raw, arch_doc=arch_doc),
                    folder,
                )
                passed, last_failure = self._check_tests(folder, test_out)

                if passed:
                    # Phase 4: Sales Manager writes README + CHANGELOG
                    all_feats = await self._store.get_features(project_id)
                    feature_results = [
                        f"{f.name}: {(f.output or '')[:200]}" for f in all_feats
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

                # Tests failed: each developer decides fix code or fix test
                await self._store.update_project(
                    project_id, status=STATUS_RETRYING, retries=attempt + 1
                )
                logger.info(
                    "DevTeam: project %s test retry %d/%d",
                    project_id[:8], attempt + 1, max_retries
                )
                current_features = await self._store.get_features(project_id)
                await asyncio.gather(
                    *[self._fix_after_test(f, spec_raw, arch_doc, folder, last_failure)
                      for f in current_features],
                    return_exceptions=True,
                )

            await self._fail(project, last_failure, retries=max_retries)

        except Exception as exc:
            logger.exception("DevTeam pipeline error for %s: %s", project_id[:8], exc)
            await self._fail(project, str(exc))

    # ── Feature lifecycle ──────────────────────────────────────────────────────

    async def _develop_feature(
        self,
        feature: DevFeature,
        spec: str,
        arch_doc: str,
        folder: str,
        available: list[str],
    ) -> None:
        """Full feature lifecycle: implement → peer reviews → author revises."""
        # Step 1: Developer implements
        await self._implement_feature(feature, spec, arch_doc, folder)

        # Step 2: Peer reviews (if a different worker is available)
        reviewer = self._peer_of(feature.assigned_to, available)
        if reviewer:
            await self._review_feature(feature, arch_doc, folder, reviewer)
            # Step 3: Author reads review and decides what to adopt
            await self._revise_feature(feature, spec, arch_doc, folder)

    async def _implement_feature(
        self,
        feature: DevFeature,
        spec: str,
        arch_doc: str,
        folder: str,
    ) -> None:
        """Developer writes initial implementation."""
        prompt = _DEV_PROMPT.format(
            folder=folder,
            spec=spec,
            arch_doc=arch_doc,
            feature_name=feature.name,
            feature_description=feature.description,
        )
        feature.status = FEAT_RUNNING
        await self._store.save_feature(feature)

        ok, out = await self._run_worker(feature.assigned_to, prompt, folder)

        feature.status = FEAT_DONE if ok else FEAT_FAILED
        feature.output = out[:2000] if ok else None
        feature.error = out[:1000] if not ok else None
        await self._store.save_feature(feature)
        logger.info(
            "DevTeam: implemented %s (by %s) — %s",
            feature.name, feature.assigned_to, "ok" if ok else "failed"
        )

    async def _review_feature(
        self,
        feature: DevFeature,
        arch_doc: str,
        folder: str,
        reviewer: str,
    ) -> None:
        """Peer reviewer reads code and writes remarks to reviews/{feature_name}_review.md."""
        review_file = str(Path(folder) / "reviews" / f"{feature.name}_review.md")
        prompt = _REVIEW_PROMPT.format(
            folder=folder,
            feature_name=feature.name,
            feature_description=feature.description,
            arch_doc=arch_doc,
            review_file=review_file,
        )
        ok, out = await self._run_worker(reviewer, prompt, folder)
        logger.info(
            "DevTeam: reviewed %s (by %s) — review file: %s",
            feature.name, reviewer, review_file
        )
        if not ok:
            logger.warning("DevTeam: reviewer warning for %s: %s", feature.name, out[:200])

    async def _revise_feature(
        self,
        feature: DevFeature,
        spec: str,
        arch_doc: str,
        folder: str,
    ) -> None:
        """Author reads the peer review and decides what to adopt (one pass)."""
        review_file = Path(folder) / "reviews" / f"{feature.name}_review.md"
        review_content = (
            review_file.read_text(encoding="utf-8", errors="replace")
            if review_file.exists()
            else "(No review file found — skipping revision)"
        )
        if "(No review file found" in review_content:
            return

        prompt = _REVISE_PROMPT.format(
            folder=folder,
            feature_name=feature.name,
            feature_description=feature.description,
            review_file=str(review_file),
            review_content=review_content,
        )
        ok, out = await self._run_worker(feature.assigned_to, prompt, folder)
        if ok:
            feature.output = out[:2000]
            await self._store.save_feature(feature)
        logger.info(
            "DevTeam: revised %s (by %s) after peer review — %s",
            feature.name, feature.assigned_to, "ok" if ok else "warning"
        )

    async def _fix_after_test(
        self,
        feature: DevFeature,
        spec: str,
        arch_doc: str,
        folder: str,
        test_failure: str,
    ) -> None:
        """Developer reads test failures and decides: fix code or fix the test."""
        prompt = _FIX_AFTER_TEST_PROMPT.format(
            folder=folder,
            feature_name=feature.name,
            feature_description=feature.description,
            arch_doc=arch_doc,
            test_failure=test_failure,
        )
        feature.status = FEAT_RUNNING
        feature.attempt += 1
        await self._store.save_feature(feature)

        ok, out = await self._run_worker(feature.assigned_to, prompt, folder)

        feature.status = FEAT_DONE if ok else FEAT_FAILED
        feature.output = out[:2000] if ok else None
        feature.error = out[:1000] if not ok else None
        await self._store.save_feature(feature)
        logger.info(
            "DevTeam: fix-after-test %s (by %s, attempt %d) — %s",
            feature.name, feature.assigned_to, feature.attempt, "ok" if ok else "failed"
        )

    async def _fail(self, project: DevProject, error: str, retries: int = 0) -> None:
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
