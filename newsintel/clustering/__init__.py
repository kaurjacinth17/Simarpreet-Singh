"""Stage 2: duplicate detection and story clustering.

Articles about the same event are linked when their TF-IDF text vectors
are close, or when they share many named entities and are at least
loosely similar. Connected components become stories. Incoming articles
can also be attached to stories already in the database, so a developing
event updates one card instead of spawning new ones.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import timedelta

from ..config import settings
from ..models import Article, Story

STOPWORDS = set("""a an the and or but of to in on at for with by from as is are was were be been has have had
it its this that these those after before over under into about than then said says say will would could
can may also not no more most new up down out who what when where why how which while amid ahead near
per cent percent year years day days week mr ms us he she they them their his her our we you i""".split())


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9\-\.%$₹]*", text.lower())
    return [w.strip(".") for w in words if w not in STOPWORDS and len(w) > 2]


def _tfidf(docs: list[list[str]]) -> list[dict[str, float]]:
    df = Counter(t for d in docs for t in set(d))
    n = len(docs)
    vecs = []
    for d in docs:
        tf = Counter(d)
        v = {t: (c / len(d)) * math.log((1 + n) / (1 + df[t]) + 1) for t, c in tf.items()} if d else {}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm for t, x in v.items()})
    return vecs


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def story_id(articles: list[Article]) -> str:
    anchor = min(articles, key=lambda a: (a.published, a.url))
    return "s_" + hashlib.sha1(anchor.url.encode()).hexdigest()[:10]


def _entity_overlap(a: set, b: set, idf: dict[str, float]) -> float:
    """Shared entities weighted by rarity, relative to the smaller article's entity mass."""
    if not a or not b:
        return 0.0
    w = lambda s: sum(idf.get(e, 0.0) for e in s)
    return w(a & b) / (min(w(a), w(b)) or 1.0)


def strong_link(cos: float, ent: float, cfg: dict) -> bool:
    """Clearly the same event: similar text, or rare shared names plus some textual overlap."""
    return cos >= cfg["cosine_link"] or (ent >= cfg["entity_link"] and cos >= cfg["cosine_with_entity"])


def weak_link(cos: float, ent: float, topic_j: float, cfg: dict) -> bool:
    """Plausibly the same event (typical of live blogs that each cover a different angle)."""
    return ent >= cfg["entity_topic_link"] and topic_j >= cfg["topic_link"] and cos + ent >= cfg["combined_floor"]


def cluster(articles: list[Article]) -> list[Story]:
    cfg = settings()["clustering"]
    live = [a for a in articles if not a.rejected]
    vecs = _tfidf([_tokens(f"{a.title} {a.title} {a.extract}") for a in live])  # title weighted x2
    edf = Counter(e for a in live for e in a.entities)
    n = max(len(live), 1)
    idf = {e: math.log(n / c) for e, c in edf.items()}
    window = timedelta(hours=cfg["window_hours"])

    strong, weak = set(), set()
    for i in range(len(live)):
        for j in range(i + 1, len(live)):
            a, b = live[i], live[j]
            if abs(a.published - b.published) > window:
                continue
            cos = _cos(vecs[i], vecs[j])
            ent = _entity_overlap(a.entities, b.entities, idf)
            if strong_link(cos, ent, cfg):
                strong.add((i, j))
            elif weak_link(cos, ent, _jaccard(set(a.topics), set(b.topics)), cfg):
                weak.add((i, j))

    # Phase 1: strong links form the cores (connected components).
    parent = list(range(len(live)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in strong:
        parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(live)):
        groups.setdefault(find(i), []).append(i)
    clusters = list(groups.values())

    # Phase 2: weak links merge two clusters only when they cover most member pairs,
    # so one loosely related article cannot chain two different events together.
    def coverage(x: list[int], y: list[int]) -> float:
        hits = sum(1 for i in x for j in y if (min(i, j), max(i, j)) in weak or (min(i, j), max(i, j)) in strong)
        return hits / (len(x) * len(y))

    while True:
        best, pair = 0.0, None
        for p in range(len(clusters)):
            for q in range(p + 1, len(clusters)):
                c = coverage(clusters[p], clusters[q])
                if c > best:
                    best, pair = c, (p, q)
        if pair is None or best < cfg["merge_coverage"]:
            break
        p, q = pair
        clusters[p] = clusters[p] + clusters[q]
        del clusters[q]

    groups = {k: [live[i] for i in c] for k, c in enumerate(clusters)}

    stories = []
    for members in groups.values():
        members.sort(key=lambda a: a.published)
        stories.append(Story(id=story_id(members), articles=members,
                             first_seen=members[0].published, last_updated=members[-1].published))
    # A cluster made only of opinion/analysis has no reporting to summarise.
    for s in stories:
        if not s.factual_articles:
            s.reject("cluster", "Only opinion or analysis, no factual reporting")
    return stories
