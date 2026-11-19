"""Stage 6: final quality gate. Fewer, better stories beat a full feed."""
from __future__ import annotations

import re

from .config import settings
from .models import Story

CLICKBAIT = [r"you won't believe", r"\bshocking\b", r"\bjaw-dropping\b", r"\bthis is why\b", r"!{2,}"]


def gate(story: Story, age_hours: float) -> Story:
    if story.rejected:
        return story
    q, f = settings()["quality"], settings()["freshness"]
    facts = story.factual_articles
    if age_hours > f["max_age_hours"]:
        story.reject("freshness", f"Old news: last reported {age_hours/24:.0f} days ago")
    elif story.credibility < q["min_credibility"]:
        story.reject("quality", f"Poorly sourced (credibility {story.credibility:.0f})")
    elif story.importance < q["min_importance"]:
        story.reject("quality", f"Too trivial to take your attention (importance {story.importance:.0f})")
    elif all(re.search("|".join(CLICKBAIT), a.title, re.I) for a in facts):
        story.reject("quality", "Clickbait headline in every source")
    elif max(len(a.extract.split()) for a in facts) < q["min_words"]:
        story.reject("quality", "Too little reporting to summarise")
    return story
