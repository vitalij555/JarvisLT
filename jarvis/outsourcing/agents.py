"""Subordinate agents: Project Manager, Client Relationship, Sales.

These agents use direct OpenAI API calls (gpt-4o-mini) with role-specific system
prompts. They do pure LLM reasoning — no tools, no MCP — so they don't need the
shared LLMClient. Each call uses a fresh context with no conversation history bleed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from jarvis.outsourcing.job_store import JobListing
from jarvis.outsourcing.profile import OutsourcingProfile

logger = logging.getLogger(__name__)

_MODEL = "gpt-4o-mini"


async def _llm_call(system: str, user: str) -> str:
    """Single-turn OpenAI call. Runs synchronous SDK in executor."""
    from openai import OpenAI
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    loop = asyncio.get_running_loop()

    def _call() -> str:
        response = client.chat.completions.create(
            model=_MODEL,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (response.choices[0].message.content or "").strip()

    return await loop.run_in_executor(None, _call)


# ── Project Manager ────────────────────────────────────────────────────────────

_PM_SYSTEM = """You are a Project Manager in a software freelancing team.
Your role: analyse a job listing and produce a structured assessment.
Output JSON with these keys:
  scope: string (2-3 sentences on what needs to be built)
  tech_stack: list of strings
  estimated_hours: integer (realistic effort estimate)
  timeline: string (e.g. "2-4 weeks")
  red_flags: list of strings (scope creep risks, vague requirements, etc.)
  fit_notes: string (why this matches or doesn't match the candidate's profile)
Be concise and factual. Output only valid JSON, no markdown fences."""


class PMAgent:
    async def assess(self, listing: JobListing, profile: OutsourcingProfile) -> str:
        user = (
            f"CANDIDATE PROFILE:\n{profile.to_prompt_context()}\n\n"
            f"JOB LISTING:\n<listing>\n{listing.raw_text[:3000]}\n</listing>\n\n"
            "Produce the JSON assessment."
        )
        result = await _llm_call(_PM_SYSTEM, user)
        logger.info("PMAgent assessed listing %s", listing.id[:8])
        return result


# ── Client Relationship Manager ────────────────────────────────────────────────

_CRM_SYSTEM = """You are a Client Relationship Manager for a software freelancing team.
Your role: write a personalised, professional outreach message for the job listing.
Output JSON with these keys:
  subject: string (email subject line, max 60 chars)
  body: string (outreach message, 3-5 short paragraphs, warm but professional)
  tone_notes: string (why you chose this tone for this particular client)
The message should feel personal, not templated. Reference specifics from the listing.
Output only valid JSON, no markdown fences."""


class CRMAgent:
    async def draft_outreach(
        self, listing: JobListing, pm_assessment: str, profile: OutsourcingProfile
    ) -> str:
        user = (
            f"CANDIDATE PROFILE:\n{profile.to_prompt_context()}\n\n"
            f"JOB LISTING:\n<listing>\n{listing.raw_text[:2000]}\n</listing>\n\n"
            f"PM ASSESSMENT:\n{pm_assessment}\n\n"
            "Write the personalised outreach message."
        )
        result = await _llm_call(_CRM_SYSTEM, user)
        logger.info("CRMAgent drafted outreach for listing %s", listing.id[:8])
        return result


# ── Sales Agent ────────────────────────────────────────────────────────────────

_SALES_SYSTEM = """You are a Sales specialist for a software freelancing team.
Your role: craft a compelling proposal / bid for the job listing.
Output JSON with these keys:
  pitch: string (main proposal body, 3-5 paragraphs — value proposition, approach, why us)
  rate_justification: string (1-2 sentences justifying the hourly rate)
  portfolio_pointer: string (optional: what kind of past work to highlight)
  call_to_action: string (closing sentence)
Be persuasive but honest. Do not over-promise.
Output only valid JSON, no markdown fences."""


class SalesAgent:
    async def build_proposal(
        self,
        listing: JobListing,
        pm_assessment: str,
        crm_draft: str,
        profile: OutsourcingProfile,
    ) -> str:
        user = (
            f"CANDIDATE PROFILE:\n{profile.to_prompt_context()}\n\n"
            f"JOB LISTING:\n<listing>\n{listing.raw_text[:2000]}\n</listing>\n\n"
            f"PM ASSESSMENT:\n{pm_assessment}\n\n"
            f"CRM OUTREACH DRAFT:\n{crm_draft}\n\n"
            "Build the sales proposal."
        )
        result = await _llm_call(_SALES_SYSTEM, user)
        logger.info("SalesAgent built proposal for listing %s", listing.id[:8])
        return result
