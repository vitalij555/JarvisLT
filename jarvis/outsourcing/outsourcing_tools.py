"""LLM tool schemas and handler for the outsourcing department."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from jarvis.outsourcing.director import DirectorAgent
from jarvis.outsourcing.job_store import JobStore
from jarvis.outsourcing.profile import OutsourcingProfile
from jarvis.outsourcing.scraper import JobScraper

if TYPE_CHECKING:
    from jarvis.llm.claude_client import LLMClient

logger = logging.getLogger(__name__)

OUTSOURCING_TOOLS = [
    {
        "name": "outsourcing_scan_jobs",
        "description": (
            "Scan configured job portals for new listings, evaluate each one against "
            "the user's skills and criteria, and run the proposal pipeline on promising "
            "matches. Results are stored and announced to the user."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "portal": {
                    "type": "string",
                    "description": "Portal to scan: 'toptal', 'upwork', or 'all' (default)",
                    "enum": ["toptal", "upwork", "all"],
                },
            },
        },
    },
    {
        "name": "outsourcing_list_opportunities",
        "description": (
            "List evaluated job opportunities with their scores and pursuit status. "
            "Use this when the user asks to see available jobs, pending approvals, or recent results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "description": "Filter by status: 'awaiting_approval', 'approved', 'submitted', 'all' (default)",
                },
            },
        },
    },
    {
        "name": "outsourcing_get_brief",
        "description": "Get the full Director brief (PM assessment, outreach draft, proposal) for a specific job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID (first 8 chars is enough — will match by prefix)",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "outsourcing_approve",
        "description": (
            "Approve a job opportunity. Triggers the Director to submit the application "
            "on the portal and create a Gmail draft. Call this when the user says "
            "'approve', 'go ahead', 'submit this one', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to approve (prefix match supported)",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "outsourcing_reject",
        "description": "Reject/dismiss a job opportunity. Marks it as rejected in the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID to reject (prefix match supported)",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "outsourcing_update_profile",
        "description": (
            "Update the user's outsourcing profile (skills, preferred rate, red flags, etc.). "
            "Use this when the user says things like 'add Go to my skills', "
            "'set minimum rate to 100 dollars', 'add PHP to red flags'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "object",
                    "description": (
                        "Dict of fields to update. Valid keys: skills (list), "
                        "min_rate_usd_hour (int), preferred_types (list), "
                        "red_flags (list), about_me (str), max_evaluations_per_day (int)"
                    ),
                },
            },
            "required": ["updates"],
        },
    },
]


class OutsourcingToolHandler:
    def __init__(
        self,
        config: dict,
        llm: "LLMClient",
        pending_queue: asyncio.Queue,
    ) -> None:
        self._config = config
        profile_path = config.get("profile_path", "outsourcing_profile.json")
        db_path = config.get("db_path", "outsourcing.db")

        self._profile = OutsourcingProfile.load_or_create(profile_path)
        self._store = JobStore(db_path)
        self._scraper = JobScraper(self._store, pending_queue, config)
        self._director = DirectorAgent(self._store, self._profile, pending_queue, llm, config)
        self._pending_queue = pending_queue
        self._initialized = False

    async def _ensure_init(self) -> None:
        if not self._initialized:
            await self._store.init()
            self._initialized = True

    async def handle_tool_call(self, tool_name: str, tool_input: dict[str, Any]) -> Any:
        await self._ensure_init()
        try:
            if tool_name == "outsourcing_scan_jobs":
                return await self._scan(tool_input.get("portal", "all"))
            if tool_name == "outsourcing_list_opportunities":
                return await self._list(tool_input.get("status_filter", "all"))
            if tool_name == "outsourcing_get_brief":
                return await self._get_brief(tool_input["job_id"])
            if tool_name == "outsourcing_approve":
                return await self._approve(tool_input["job_id"])
            if tool_name == "outsourcing_reject":
                return await self._reject(tool_input["job_id"])
            if tool_name == "outsourcing_update_profile":
                return await self._update_profile(tool_input["updates"])
            raise ValueError(f"Unknown tool: {tool_name}")
        except Exception as exc:
            logger.error("Outsourcing tool %s error: %s", tool_name, exc)
            return {"error": str(exc)}

    # ── Tool implementations ───────────────────────────────────────────────────

    async def _scan(self, portal: str) -> dict:
        max_evals = self._config.get("max_evaluations_per_day", 20)
        today_count = await self._store.count_today_evaluations()
        if today_count >= max_evals:
            return {"status": "skipped", "reason": f"Daily evaluation limit ({max_evals}) reached"}

        portals = ["toptal", "upwork"] if portal == "all" else [portal]
        total_new = 0
        total_promising = 0

        for p in portals:
            portal_cfg = self._config.get("portals", {}).get(p, {})
            if not portal_cfg.get("enabled", True):
                continue

            listings = await self._scraper.fetch_new(p)
            total_new += len(listings)

            for listing in listings:
                if today_count >= max_evals:
                    break
                today_count += 1

                eval_result = await self._director.evaluate(listing)
                listing.score = eval_result.score
                listing.rationale = eval_result.rationale

                min_score = self._config.get("min_score", 7)
                if eval_result.pursue and eval_result.score >= min_score:
                    listing.status = "awaiting_approval"
                    await self._store.save_listing(listing)

                    # Run proposal chain
                    brief = await self._director.run_proposal_chain(listing)
                    total_promising += 1

                    # Notify main loop
                    await self._pending_queue.put({
                        "type": "opportunity",
                        "count": total_promising,
                        "portal": p,
                        "job_id": listing.id,
                        "preview": f"{listing.title} on {p.capitalize()}, score {eval_result.score}/10",
                    })
                else:
                    listing.status = "pending"
                    await self._store.save_listing(listing)

        return {
            "new_listings": total_new,
            "promising": total_promising,
            "evaluated_today": today_count,
        }

    async def _list(self, status_filter: str) -> str:
        opportunities = await self._store.list_opportunities()
        if not opportunities:
            return "No evaluated job opportunities found yet. Run a scan first."

        if status_filter != "all":
            opportunities = [o for o in opportunities if o["status"] == status_filter]
            if not opportunities:
                return f"No opportunities with status '{status_filter}'."

        lines = ["**Job Opportunities:**\n"]
        for opp in opportunities:
            status_emoji = {
                "awaiting_approval": "⏳",
                "approved": "✅",
                "submitted": "🚀",
                "rejected": "❌",
                "pending": "🔍",
            }.get(opp["status"], "•")
            lines.append(
                f"{status_emoji} **{opp['title']}** [{opp['portal']}]\n"
                f"   Score: {opp['score']}/10 | Status: {opp['status']}\n"
                f"   ID: {opp['id'][:8]} | {opp['rationale'] or ''}\n"
            )
        return "\n".join(lines)

    async def _get_brief(self, job_id_prefix: str) -> str:
        job_id = await self._resolve_id(job_id_prefix)
        if not job_id:
            return f"No job found matching ID prefix '{job_id_prefix}'"

        brief = await self._store.get_brief(job_id)
        listing = await self._store.get_listing(job_id)
        if not brief:
            return f"No brief found for job {job_id[:8]}. It may not have been evaluated yet."
        return f"**{listing.title if listing else job_id}**\n\n{brief.full_brief}"

    async def _approve(self, job_id_prefix: str) -> str:
        job_id = await self._resolve_id(job_id_prefix)
        if not job_id:
            return f"No job found matching ID prefix '{job_id_prefix}'"

        await self._store.update_status(job_id, "approved")
        result = await self._director.execute(job_id)

        steps = result.get("steps", [])
        summary = []
        for step in steps:
            if "error" in step:
                summary.append(f"⚠️ {step['step']}: {step['error']}")
            else:
                summary.append(f"✅ {step['step']}: done")

        return f"Job approved and executed.\n" + "\n".join(summary)

    async def _reject(self, job_id_prefix: str) -> str:
        job_id = await self._resolve_id(job_id_prefix)
        if not job_id:
            return f"No job found matching ID prefix '{job_id_prefix}'"
        await self._store.update_status(job_id, "rejected")
        return f"Job {job_id[:8]} marked as rejected."

    async def _update_profile(self, updates: dict) -> str:
        self._profile.update_from_dict(updates)
        self._profile.save(self._config.get("profile_path", "outsourcing_profile.json"))
        # Reload director's reference
        self._director._profile = self._profile
        return f"Profile updated: {', '.join(updates.keys())}"

    async def _resolve_id(self, prefix: str) -> str | None:
        """Find a full job ID from a prefix (first 8+ chars)."""
        opportunities = await self._store.list_opportunities(limit=100)
        for opp in opportunities:
            if opp["id"].startswith(prefix):
                return opp["id"]
        # Try awaiting_approval listings too
        listings = await self._store.list_by_status("awaiting_approval", "approved")
        for listing in listings:
            if listing.id.startswith(prefix):
                return listing.id
        return None
