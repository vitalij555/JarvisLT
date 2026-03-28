"""Persistent storage for job listings and proposal briefs (outsourcing.db)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

_CREATE_LISTINGS = """
CREATE TABLE IF NOT EXISTS job_listings (
    id          TEXT PRIMARY KEY,
    portal      TEXT NOT NULL,
    title       TEXT,
    url         TEXT,
    raw_text    TEXT,
    fetched_at  TEXT NOT NULL,
    score       INTEGER,
    status      TEXT NOT NULL DEFAULT 'pending',
    rationale   TEXT
)
"""

_CREATE_BRIEFS = """
CREATE TABLE IF NOT EXISTS proposal_briefs (
    job_id          TEXT PRIMARY KEY REFERENCES job_listings(id),
    pm_assessment   TEXT,
    crm_draft       TEXT,
    sales_pitch     TEXT,
    worker_output   TEXT,
    full_brief      TEXT,
    created_at      TEXT NOT NULL
)
"""

_STATUS_PENDING            = "pending"
_STATUS_AWAITING_APPROVAL  = "awaiting_approval"
_STATUS_APPROVED           = "approved"
_STATUS_REJECTED           = "rejected"
_STATUS_SUBMITTED          = "submitted"
_STATUS_AUTH_REQUIRED      = "auth_required"


def listing_id(url: str) -> str:
    """Stable dedup key: SHA-256 of the listing URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobListing:
    portal: str
    title: str
    url: str
    raw_text: str
    id: str = field(default="")
    fetched_at: str = field(default_factory=_now)
    score: int | None = None
    status: str = _STATUS_PENDING
    rationale: str | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = listing_id(self.url)


@dataclass
class ProposalBrief:
    job_id: str
    pm_assessment: str = ""
    crm_draft: str = ""
    sales_pitch: str = ""
    worker_output: str = ""
    full_brief: str = ""
    created_at: str = field(default_factory=_now)


class JobStore:
    def __init__(self, db_path: str = "outsourcing.db") -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_LISTINGS)
            await db.execute(_CREATE_BRIEFS)
            await db.commit()

    # ── Listings ───────────────────────────────────────────────────────────────

    async def exists(self, listing_id: str) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT 1 FROM job_listings WHERE id=?", (listing_id,)
            )
            return await cur.fetchone() is not None

    async def save_listing(self, listing: JobListing) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO job_listings
                   (id, portal, title, url, raw_text, fetched_at, score, status, rationale)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       score=excluded.score,
                       status=excluded.status,
                       rationale=excluded.rationale""",
                (listing.id, listing.portal, listing.title, listing.url,
                 listing.raw_text, listing.fetched_at, listing.score,
                 listing.status, listing.rationale),
            )
            await db.commit()

    async def update_status(self, job_id: str, status: str, score: int | None = None) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            if score is not None:
                await db.execute(
                    "UPDATE job_listings SET status=?, score=? WHERE id=?",
                    (status, score, job_id),
                )
            else:
                await db.execute(
                    "UPDATE job_listings SET status=? WHERE id=?",
                    (status, job_id),
                )
            await db.commit()

    async def get_listing(self, job_id: str) -> JobListing | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM job_listings WHERE id=?", (job_id,)
            )
            row = await cur.fetchone()
        return _row_to_listing(row) if row else None

    async def list_by_status(self, *statuses: str) -> list[JobListing]:
        placeholders = ",".join("?" * len(statuses))
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                f"SELECT * FROM job_listings WHERE status IN ({placeholders})"
                " ORDER BY fetched_at DESC",
                statuses,
            )
            rows = await cur.fetchall()
        return [_row_to_listing(r) for r in rows]

    async def count_today_evaluations(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM job_listings WHERE fetched_at >= date('now')"
                " AND score IS NOT NULL"
            )
            row = await cur.fetchone()
        return row[0] if row else 0

    # ── Briefs ─────────────────────────────────────────────────────────────────

    async def save_brief(self, brief: ProposalBrief) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO proposal_briefs
                   (job_id, pm_assessment, crm_draft, sales_pitch, worker_output,
                    full_brief, created_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(job_id) DO UPDATE SET
                       pm_assessment=excluded.pm_assessment,
                       crm_draft=excluded.crm_draft,
                       sales_pitch=excluded.sales_pitch,
                       worker_output=excluded.worker_output,
                       full_brief=excluded.full_brief""",
                (brief.job_id, brief.pm_assessment, brief.crm_draft,
                 brief.sales_pitch, brief.worker_output,
                 brief.full_brief, brief.created_at),
            )
            await db.commit()

    async def get_brief(self, job_id: str) -> ProposalBrief | None:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM proposal_briefs WHERE job_id=?", (job_id,)
            )
            row = await cur.fetchone()
        if not row:
            return None
        return ProposalBrief(
            job_id=row["job_id"],
            pm_assessment=row["pm_assessment"] or "",
            crm_draft=row["crm_draft"] or "",
            sales_pitch=row["sales_pitch"] or "",
            worker_output=row["worker_output"] or "",
            full_brief=row["full_brief"] or "",
            created_at=row["created_at"],
        )

    async def list_opportunities(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return listings with scores for LLM presentation."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT l.id, l.portal, l.title, l.url, l.score, l.status,
                          l.rationale, l.fetched_at,
                          b.full_brief IS NOT NULL AS has_brief
                   FROM job_listings l
                   LEFT JOIN proposal_briefs b ON l.id = b.job_id
                   WHERE l.score IS NOT NULL
                   ORDER BY l.score DESC, l.fetched_at DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


def _row_to_listing(row: Any) -> JobListing:
    return JobListing(
        id=row["id"],
        portal=row["portal"],
        title=row["title"] or "",
        url=row["url"] or "",
        raw_text=row["raw_text"] or "",
        fetched_at=row["fetched_at"],
        score=row["score"],
        status=row["status"],
        rationale=row["rationale"],
    )
