"""Director Agent — orchestrates the full outsourcing pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from jarvis.outsourcing.agents import CRMAgent, PMAgent, SalesAgent
from jarvis.outsourcing.job_store import JobListing, JobStore, ProposalBrief
from jarvis.outsourcing.profile import OutsourcingProfile
from jarvis.outsourcing.workers.pool import WorkerPool

if TYPE_CHECKING:
    from jarvis.llm.claude_client import LLMClient
    from jarvis.llm.memory import ConversationMemory

logger = logging.getLogger(__name__)

_EVAL_MODEL = "gpt-4o-mini"
_DIRECTOR_MODEL = "gpt-4o"

_EVAL_SYSTEM = """You are a Director of a software freelancing team evaluating job listings.
Evaluate how well this listing matches the candidate's profile and skills.
Output JSON with these keys:
  score: integer 0-10 (0=terrible fit, 10=perfect fit)
  pursue: boolean (true if score >= 7)
  rationale: string (2-3 sentences explaining the score)
  needs_code_sample: boolean (true if the listing explicitly asks for a code sample or portfolio)
Output only valid JSON, no markdown fences."""


@dataclass
class EvalResult:
    score: int
    pursue: bool
    rationale: str
    needs_code_sample: bool = False

    @classmethod
    def from_json(cls, text: str) -> "EvalResult":
        try:
            # Strip markdown fences if LLM wrapped them
            text = text.strip()
            if text.startswith("```"):
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            return cls(
                score=int(data.get("score", 0)),
                pursue=bool(data.get("pursue", False)),
                rationale=str(data.get("rationale", "")),
                needs_code_sample=bool(data.get("needs_code_sample", False)),
            )
        except Exception as exc:
            logger.warning("Could not parse eval JSON: %s — raw: %s", exc, text[:200])
            return cls(score=0, pursue=False, rationale="Parse error: " + str(exc))


class DirectorAgent:
    def __init__(
        self,
        job_store: JobStore,
        profile: OutsourcingProfile,
        pending_queue: asyncio.Queue,
        llm: "LLMClient",
        config: dict,
    ) -> None:
        self._store = job_store
        self._profile = profile
        self._queue = pending_queue
        self._llm = llm
        self._config = config
        self._pm = PMAgent()
        self._crm = CRMAgent()
        self._sales = SalesAgent()
        self._workers = WorkerPool(timeout=config.get("worker_timeout", 300))

    # ── Evaluation ─────────────────────────────────────────────────────────────

    async def evaluate(self, listing: JobListing) -> EvalResult:
        """Quick cheap evaluation — gpt-4o-mini, returns score + pursue decision."""
        from openai import OpenAI
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        loop = asyncio.get_running_loop()

        user = (
            f"CANDIDATE PROFILE:\n{self._profile.to_prompt_context()}\n\n"
            f"JOB LISTING:\n<listing>\n{listing.raw_text[:3000]}\n</listing>\n\n"
            "Evaluate this listing."
        )

        def _call() -> str:
            resp = client.chat.completions.create(
                model=_EVAL_MODEL,
                max_tokens=300,
                messages=[
                    {"role": "system", "content": _EVAL_SYSTEM},
                    {"role": "user", "content": user},
                ],
            )
            return (resp.choices[0].message.content or "").strip()

        raw = await loop.run_in_executor(None, _call)
        result = EvalResult.from_json(raw)
        logger.info(
            "Director evaluated %s: score=%d pursue=%s",
            listing.id[:8], result.score, result.pursue,
        )
        return result

    # ── Proposal chain ─────────────────────────────────────────────────────────

    async def run_proposal_chain(self, listing: JobListing) -> ProposalBrief:
        """Run PM → CRM → Sales → (optional) Worker and return a compiled brief."""
        pm_result = await self._pm.assess(listing, self._profile)
        crm_result = await self._crm.draft_outreach(listing, pm_result, self._profile)
        sales_result = await self._sales.build_proposal(listing, pm_result, crm_result, self._profile)

        worker_output = ""
        eval_result = await self.evaluate(listing)  # re-evaluate to check code sample need
        if eval_result.needs_code_sample:
            worker_prompt = (
                f"A client is looking for: {listing.title}\n\n"
                f"Job description: {listing.raw_text[:1000]}\n\n"
                "Write a concise, relevant code sample (max 50 lines) that demonstrates "
                "the most relevant skill for this project. Include a brief comment explaining it."
            )
            worker_result = await self._workers.run(worker_prompt)
            if worker_result.success:
                worker_output = worker_result.output

        # Compile full brief
        full_brief = self._compile_brief(listing, pm_result, crm_result, sales_result, worker_output)

        brief = ProposalBrief(
            job_id=listing.id,
            pm_assessment=pm_result,
            crm_draft=crm_result,
            sales_pitch=sales_result,
            worker_output=worker_output,
            full_brief=full_brief,
        )
        await self._store.save_brief(brief)
        return brief

    def _compile_brief(
        self,
        listing: JobListing,
        pm: str, crm: str, sales: str, worker: str,
    ) -> str:
        parts = [
            f"# Brief: {listing.title}",
            f"Portal: {listing.portal}  |  URL: {listing.url}",
            "",
            "## PM Assessment",
            pm,
            "",
            "## Outreach Draft",
            crm,
            "",
            "## Sales Proposal",
            sales,
        ]
        if worker:
            parts += ["", "## Code Sample (from Worker)", worker]
        return "\n".join(parts)

    # ── Execution (after user approval) ────────────────────────────────────────

    async def execute(self, job_id: str) -> dict[str, Any]:
        """Submit application on portal + create Gmail draft. Called after user approval."""
        listing = await self._store.get_listing(job_id)
        brief = await self._store.get_brief(job_id)

        if not listing or not brief:
            return {"error": f"Job {job_id[:8]} not found or has no brief yet"}

        results: dict[str, Any] = {"job_id": job_id, "steps": []}

        # Parse CRM draft for email content
        crm_subject, crm_body = self._extract_crm_email(brief.crm_draft, listing)

        # Step 1: Create Gmail draft
        try:
            from jarvis.llm.memory import ConversationMemory
            memory = ConversationMemory(max_turns=5, persist_path=None)
            gmail_prompt = (
                f"Create a Gmail draft with subject: '{crm_subject}'\n"
                f"Body:\n{crm_body}\n\n"
                "Use the create_gmail_draft tool. Do not send it — draft only."
            )
            gmail_result = await self._llm.chat_async(gmail_prompt, memory)
            results["steps"].append({"step": "gmail_draft", "result": gmail_result})
            logger.info("Gmail draft created for job %s", job_id[:8])
        except Exception as exc:
            logger.error("Gmail draft failed for %s: %s", job_id[:8], exc)
            results["steps"].append({"step": "gmail_draft", "error": str(exc)})

        # Step 2: Submit on portal via Playwright
        try:
            memory = ConversationMemory(max_turns=10, persist_path=None)
            playwright_prompt = (
                f"Navigate to this job listing and submit an application:\n"
                f"URL: {listing.url}\n\n"
                f"Use this proposal text when filling in the application form:\n"
                f"{self._extract_pitch(brief.sales_pitch)}\n\n"
                "Use browser tools (browser_navigate, browser_snapshot, browser_click, "
                "browser_fill_form) to complete the application. "
                "If a login is required, stop and report that authentication is needed."
            )
            playwright_result = await self._llm.chat_async(playwright_prompt, memory)
            results["steps"].append({"step": "portal_submit", "result": playwright_result})
            logger.info("Portal submission attempted for job %s", job_id[:8])
        except Exception as exc:
            logger.error("Portal submit failed for %s: %s", job_id[:8], exc)
            results["steps"].append({"step": "portal_submit", "error": str(exc)})

        # Update status
        await self._store.update_status(job_id, "submitted")
        results["status"] = "submitted"
        return results

    def _extract_crm_email(self, crm_json: str, listing: JobListing) -> tuple[str, str]:
        """Parse CRM agent JSON output for subject + body."""
        try:
            data = json.loads(crm_json)
            return data.get("subject", f"Re: {listing.title}"), data.get("body", crm_json)
        except Exception:
            return f"Re: {listing.title}", crm_json

    def _extract_pitch(self, sales_json: str) -> str:
        try:
            data = json.loads(sales_json)
            return data.get("pitch", sales_json)
        except Exception:
            return sales_json
