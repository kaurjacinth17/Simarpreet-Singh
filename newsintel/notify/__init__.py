"""Breaking-news alerts.

An alert goes out only for a story flagged breaking, at most once per story
unless a significant new development is added to its timeline, within the
user's chosen frequency and a daily cap. Routine updates never alert.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..models import Story
from ..users import UserProfile

DAILY_CAP = {"off": 0, "major": 3, "all": 6}


@dataclass
class AlertLog:
    sent: dict[str, int] = field(default_factory=dict)       # story_id -> timeline length when alerted
    per_day: dict[str, int] = field(default_factory=dict)    # "user:YYYY-MM-DD" -> count


def alerts_for(user: UserProfile, stories: list[Story], log: AlertLog, now: datetime) -> list[dict]:
    cap = DAILY_CAP.get(user.breaking_alerts, 0)
    day_key = f"{user.user_id}:{now.date()}"
    out = []
    for s in sorted(stories, key=lambda s: s.importance, reverse=True):
        if not s.breaking or s.rejected:
            continue
        if user.breaking_alerts == "major" and s.importance < 80:
            continue
        timeline_len = len(s.summary.timeline) if s.summary else 0
        if s.id in log.sent and timeline_len <= log.sent[s.id]:
            continue  # already alerted and nothing significant has been added
        if log.per_day.get(day_key, 0) >= cap:
            break
        log.sent[s.id] = timeline_len
        log.per_day[day_key] = log.per_day.get(day_key, 0) + 1
        out.append({"story_id": s.id, "title": s.summary.headline if s.summary else s.articles[0].title,
                    "label": s.importance_label})
    return out
