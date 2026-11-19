"""User preferences and feedback learning.

Feedback nudges a per-topic affinity by a small step, capped, and the
affinity decays back toward zero over time, so one tap never permanently
reshapes the feed and every change is reversible (undo removes the event).
Explicit choices (exclude a topic, hide a source) are stored separately
and can be switched off in settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..config import settings
from ..digest import DEFAULT_DIGESTS

FEEDBACK_EFFECT = {
    "relevant": +1.0,
    "more_like_this": +1.5,
    "not_relevant": -1.0,
    "too_trivial": -0.75,
    "too_repetitive": -0.5,   # mainly marks the story seen; light topic effect
}


@dataclass
class FeedbackEvent:
    story_id: str
    kind: str
    topics: list[str]
    at: datetime
    source_id: str | None = None


@dataclass
class UserProfile:
    user_id: str = "local"
    interests: list[str] = field(default_factory=lambda: ["World affairs", "India", "Economy"])
    priority_interests: list[str] = field(default_factory=list)
    excluded_topics: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=lambda: ["India", "World"])
    breaking_alerts: str = "major"          # off | major | all
    digests: list[dict] = field(default_factory=lambda: [dict(d) for d in DEFAULT_DIGESTS])
    timezone: str = "Asia/Kolkata"
    digest_channel: str = "push"            # push | email | both
    email: str | None = None
    email_confirmed: bool = False           # set only by clicking the link in the confirmation email
    personal_share: float | None = None     # overrides settings feed_mix when set
    followed_topics: list[str] = field(default_factory=list)
    hidden_sources: list[str] = field(default_factory=list)
    hidden_stories: list[str] = field(default_factory=list)
    saved: list[str] = field(default_factory=list)
    last_seen: datetime | None = None
    feedback: list[FeedbackEvent] = field(default_factory=list)

    @property
    def affinity(self) -> dict[str, float]:
        """Topic affinity rebuilt from the feedback log with exponential decay."""
        cfg = settings()["feedback"]
        now = datetime.now(timezone.utc)
        out: dict[str, float] = {}
        for ev in self.feedback:
            effect = FEEDBACK_EFFECT.get(ev.kind)
            if not effect:
                continue
            age_days = (now - ev.at).total_seconds() / 86400
            weight = cfg["step"] * effect * 0.5 ** (age_days / cfg["half_life_days"])
            for t in ev.topics:
                out[t] = out.get(t, 0.0) + weight
        cap = cfg["max_affinity"]
        return {t: max(-cap, min(cap, v)) for t, v in out.items()}

    def record(self, story_id: str, kind: str, topics: list[str], source_id: str | None = None) -> None:
        if kind == "hide_topic":
            self.excluded_topics = sorted(set(self.excluded_topics) | set(topics[:1]))
        elif kind == "hide_source" and source_id:
            self.hidden_sources = sorted(set(self.hidden_sources) | {source_id})
        elif kind == "follow_topic":
            self.followed_topics = sorted(set(self.followed_topics) | set(topics[:1]))
        elif kind == "hide_similar":
            self.hidden_stories.append(story_id)
            self.feedback.append(FeedbackEvent(story_id, "not_relevant", topics, datetime.now(timezone.utc)))
        else:
            self.feedback.append(FeedbackEvent(story_id, kind, topics, datetime.now(timezone.utc), source_id))

    def undo(self, story_id: str) -> None:
        self.feedback = [f for f in self.feedback if f.story_id != story_id]
        self.hidden_stories = [s for s in self.hidden_stories if s != story_id]
