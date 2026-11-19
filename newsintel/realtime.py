"""Near-real-time loop: refresh, diff, stream to open apps, push alerts.

Cadence (config/settings.json -> "refresh"):
  * every `normal_minutes` (default 5) the pipeline pulls all feeds;
  * while any story is flagged breaking, it tightens to `breaking_minutes` (default 2);
  * each run only summarises clusters whose article set changed (the summary
    cache is keyed by article set), so frequent runs stay cheap.

After each run the new result is compared with the previous one:
  * new_story      - an event that passed every filter for the first time
  * development    - an existing story gained a timeline entry or a new independent source
  * breaking       - a story became breaking
Open apps receive these over Server-Sent Events (/api/stream) and show
"N new stories" without reshuffling the card under the reader's thumb.
Push notifications go only to users whose alert setting allows it, through
notify.alerts_for (breaking only, once per story unless it develops, daily cap).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field

from .config import settings
from .models import Story

log = logging.getLogger("realtime")


def fingerprint(s: Story) -> dict:
    return {"owners": len(s.independent_owners), "timeline": len(s.summary.timeline) if s.summary else 0,
            "breaking": s.breaking}


def diff(old: dict[str, dict], new_stories: list[Story]) -> list[dict]:
    events = []
    for s in new_stories:
        if s.rejected:
            continue
        fp, head = fingerprint(s), s.summary.headline if s.summary else ""
        before = old.get(s.id)
        if before is None:
            events.append({"type": "breaking" if s.breaking else "new_story", "id": s.id, "headline": head})
        elif fp["breaking"] and not before["breaking"]:
            events.append({"type": "breaking", "id": s.id, "headline": head})
        elif fp["timeline"] > before["timeline"] or fp["owners"] > before["owners"]:
            events.append({"type": "development", "id": s.id, "headline": head})
    return events


@dataclass
class Hub:
    """Fan-out of events to every connected SSE client."""
    queues: set = field(default_factory=set)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.queues.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self.queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled client misses events; it re-syncs via /api/bundle


class PushSender:
    """Web Push (works for installed PWAs on Android, iOS 16.4+ and desktop browsers).

    Needs VAPID keys in VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY and `pip install pywebpush`.
    A native app would swap this class for FCM/APNs with the same send() signature.
    """

    def __init__(self, public: str | None = None, private: str | None = None, subject: str | None = None):
        if public is None and private is None:
            from . import serverkeys
            public, private = serverkeys.vapid_keys()
            subject = subject or serverkeys.vapid_subject()
        self.public, self.private = public, private
        self.subject = subject or os.environ.get("VAPID_SUBJECT", "mailto:alerts@example.com")
        self.gone: list[str] = []

    @property
    def enabled(self) -> bool:
        return bool(self.private and self.public)

    def send(self, subscription: dict, payload: dict) -> bool:
        if not self.enabled:
            log.info("push (dry run): %s", payload["title"])
            return False
        try:
            from pywebpush import webpush
            webpush(subscription, json.dumps(payload), vapid_private_key=self.private,
                    vapid_claims={"sub": self.subject}, ttl=12 * 3600)
            return True
        except Exception as exc:  # expired subscriptions are normal; never stop the loop
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                self.gone.append(subscription.get("endpoint"))  # unsubscribed or uninstalled: forget it
            log.warning("push failed (%s): %s", status, exc)
            return False


def _forget_gone(store, pusher) -> None:
    for endpoint in pusher.gone:
        store.remove_push_subscription(endpoint)
    pusher.gone.clear()


async def run_loop(refresh, store, hub: Hub, alert_log, pusher: PushSender) -> None:
    """refresh() runs the pipeline and returns a Result. Runs forever."""
    from .notify import alerts_for

    cfg = settings()["refresh"]
    previous: dict[str, dict] = {}
    first = True
    while True:
        try:
            result = await asyncio.to_thread(refresh)
            events = diff(previous, result.stories)
            previous = {s.id: fingerprint(s) for s in result.stories if not s.rejected}
            if not first:  # the first run only establishes the baseline
                for ev in events:
                    hub.publish(ev)
                for user_id, sub in store.push_subscriptions():
                    user = store.load_user(user_id)
                    for alert in alerts_for(user, result.stories, alert_log, result.now):
                        pusher.send(sub, {"title": alert["title"], "body": alert["label"],
                                          "url": f"/#story-{alert['story_id']}"})
                _forget_gone(store, pusher)
            first = False
            breaking = any(s.breaking for s in result.live)
            wait = cfg["breaking_minutes"] if breaking else cfg["normal_minutes"]
        except Exception as exc:
            log.exception("refresh failed: %s", exc)
            wait = cfg["normal_minutes"]
        await asyncio.sleep(wait * 60)


async def digest_loop(current, store, pusher: PushSender, mailer=None, base_url: str = "") -> None:
    """Checks every minute whose briefing time has arrived and delivers it by app notification,
    email or both, as each user chose. `current()` returns the latest pipeline Result."""
    from datetime import datetime, timezone

    from . import digest
    from .feed import rank

    mailer = mailer or digest.EmailSender()
    while True:
        try:
            now = datetime.now(timezone.utc)
            result = current()
            subs: dict[str, list[dict]] = {}
            for uid, sub in store.push_subscriptions():
                subs.setdefault(uid, []).append(sub)
            for user_id in store.user_ids():
                user = store.load_user(user_id)
                for d in digest.due(user.digests, user.timezone, now, store.digests_sent(user_id)):
                    store.mark_digest(user_id, d["key"])          # mark first: never a retry storm
                    since = digest.previous_time(user.digests, user.timezone, now)
                    rows = rank(result.stories, user)
                    if user.digest_channel in ("push", "both"):
                        payload = digest.build(d, rows, since)
                        for sub in subs.get(user_id, []):
                            if payload:
                                pusher.send(sub, payload)
                    if user.digest_channel in ("email", "both") and user.email and user.email_confirmed:
                        unsub = f"{base_url}/api/unsubscribe/{user_id}/{digest.unsubscribe_token(user_id)}"
                        mail = digest.email_html(d, rows, since, base_url, unsub)
                        if mail:
                            mailer.send(user.email, *mail, unsubscribe_url=unsub)
            _forget_gone(store, pusher)
        except Exception as exc:
            log.exception("digest loop failed: %s", exc)
        await asyncio.sleep(60)
