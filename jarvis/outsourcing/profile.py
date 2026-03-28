"""User outsourcing profile — skills, rates, preferences, red flags."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "outsourcing_profile.json"

_DEFAULT_PROFILE = {
    "skills": ["Python", "FastAPI", "LLM integration", "REST APIs"],
    "min_rate_usd_hour": 80,
    "preferred_types": ["API development", "LLM/AI integration", "backend services"],
    "red_flags": ["WordPress", "PHP", "Magento", "fixed price under $500"],
    "max_evaluations_per_day": 20,
    "min_score_for_proposal": 7,
    "about_me": (
        "Experienced software engineer specialising in Python backend systems, "
        "LLM integrations, and API development. Available for freelance projects."
    ),
}


@dataclass
class OutsourcingProfile:
    skills: list[str] = field(default_factory=list)
    min_rate_usd_hour: int = 80
    preferred_types: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)
    max_evaluations_per_day: int = 20
    min_score_for_proposal: int = 7
    about_me: str = ""

    # ── persistence ───────────────────────────────────────────────────────────

    @classmethod
    def load_or_create(cls, path: str = _DEFAULT_PATH) -> "OutsourcingProfile":
        p = Path(path)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except Exception as exc:
                logger.warning("Could not load outsourcing profile from %s: %s — using defaults", path, exc)
        else:
            profile = cls(**_DEFAULT_PROFILE)
            profile.save(path)
            logger.info("Created default outsourcing profile at %s", path)
            return profile
        return cls(**_DEFAULT_PROFILE)

    def save(self, path: str = _DEFAULT_PATH) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    def to_prompt_context(self) -> str:
        """Compact text representation for LLM prompts."""
        return (
            f"Skills: {', '.join(self.skills)}\n"
            f"Preferred work types: {', '.join(self.preferred_types)}\n"
            f"Minimum rate: ${self.min_rate_usd_hour}/hour\n"
            f"Red flags (auto-reject if present): {', '.join(self.red_flags)}\n"
            f"About me: {self.about_me}"
        )

    def update_from_dict(self, updates: dict) -> None:
        """Apply partial updates (e.g. from voice command parsed by LLM)."""
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
