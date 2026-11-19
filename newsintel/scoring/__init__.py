"""Final story score out of 100, using the configurable weights in settings.json."""
from __future__ import annotations

from ..config import settings
from ..models import Story


def total_score(story: Story, relevance: float) -> tuple[float, dict]:
    w = settings()["score_weights"]
    parts = {
        "importance": story.importance,
        "relevance": relevance,
        "credibility": story.credibility,
        "freshness": story.freshness,
        "practical": story.practical,
    }
    total = sum(w[k] * v for k, v in parts.items())
    return round(total, 1), {k: round(v, 1) for k, v in parts.items()}
