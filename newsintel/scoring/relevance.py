"""Stage 4: personal relevance.

Relevance is computed from the story's topics and regions against the
user's interests (mapped to topic groups in settings.json), priority
interests, regions, excluded topics and a slowly-learned topic affinity
from feedback. A keyword in the headline alone is never enough: the
story's topic set has to overlap the interest's topic group.
"""
from __future__ import annotations

from ..config import settings
from ..models import Story
from ..users import UserProfile


def story_topics(story: Story) -> set[str]:
    """The summariser's topic list reflects the whole event; keyword tags from individual
    articles are only a fallback, because one passing mention ("Iran" in a summit story)
    would otherwise make the story look relevant to the wrong readers."""
    if story.summary and story.summary.topics:
        return set(story.summary.topics)
    t: set[str] = set()
    for a in story.factual_articles:
        t |= set(a.topics)
    return t


def story_regions(story: Story) -> set[str]:
    regions = set(story.summary.regions) if story.summary and story.summary.regions else set()
    if "india" in story_topics(story):
        regions.add("India")
    return regions or {"World"}


def relevance(story: Story, user: UserProfile) -> tuple[float, list[str]]:
    groups = settings()["interests"]
    topics = story_topics(story)
    reasons: list[str] = []
    if topics & set(user.excluded_topics):
        return 0.0, ["Matches a topic you excluded"]

    best = 0.0
    for interest in user.interests:
        group = set(groups.get(interest, []))
        overlap = topics & group
        if not overlap:
            continue
        # Strength grows with how much of the story sits inside the interest.
        strength = 55 + 15 * min(len(overlap), 3)
        if interest in user.priority_interests:
            strength *= 1.25
            reasons.append(f"Priority interest: {interest}")
        else:
            reasons.append(f"You follow {interest}")
        best = max(best, strength)

    regions = story_regions(story)
    if user.regions and regions & set(user.regions):
        best += 10
        reasons.append("In a region you chose: " + ", ".join(sorted(regions & set(user.regions))))

    learned = sum(user.affinity.get(t, 0.0) for t in topics)
    learned = max(-0.35, min(0.35, learned))
    if abs(learned) > 0.02:
        best *= 1 + learned
        reasons.append("Adjusted by your feedback" if learned > 0 else "Down-weighted by your feedback")

    if user.followed_topics and topics & set(user.followed_topics):
        best += 10
        reasons.append("A topic you follow")
    return round(max(0.0, min(100.0, best)), 1), reasons
