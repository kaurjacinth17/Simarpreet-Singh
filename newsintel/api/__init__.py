"""HTTP API. Run with:  uvicorn newsintel.api:app --reload

API keys (ANTHROPIC_API_KEY) stay on the server: the browser only ever
talks to these endpoints, never to a model or a publisher API directly.
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from pathlib import Path

import asyncio
import json
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from ..db import Store
from ..feed import breaking, catch_me_up, rank, whats_important, your_feed
from ..ingestion import FixtureAdapter, RSSAdapter
from ..notify import AlertLog, alerts_for
from ..pipeline import Result, result_json, run, story_json
from ..realtime import Hub, PushSender, digest_loop, run_loop
from .. import digest
from ..scoring.relevance import story_topics
from ..summarization import CacheSummarizer, ClaudeSummarizer
from .. import serverkeys

WEB = Path(__file__).resolve().parents[2] / "web"
app = FastAPI(title="NewsIntel", version="1.0")
store = Store(os.environ.get("NEWSINTEL_DB", "newsintel.db"))
serverkeys.init(store)          # push keys + link secret: from env vars, else generated once and kept in the DB
_state: dict = {"result": None, "refreshes": 0, "last_refresh": None, "feeds": [], "started": datetime.now(timezone.utc)}
_lock = threading.Lock()
alert_log = AlertLog()
hub = Hub()
pusher = PushSender()
mailer = digest.EmailSender()
USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def base_url(request: Request | None = None) -> str:
    """Public address of the app, for links in emails and share previews."""
    env = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if env or request is None:
        return env
    return str(request.base_url).rstrip("/")


@app.middleware("http")
async def check_user_id(request: Request, call_next):
    """Each device has its own random id; reject anything that is not shaped like one."""
    parts = request.url.path.split("/")
    if len(parts) > 3 and parts[1:3] == ["api", "users"] and not USER_ID.match(parts[3]):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "bad user id"}, status_code=400)
    return await call_next(request)


@app.on_event("startup")
async def start_live_loop():
    """With NEWSINTEL_LIVE=1 the server refreshes on its own schedule (see realtime.py)."""
    if os.environ.get("NEWSINTEL_LIVE") == "1":
        asyncio.create_task(run_loop(refresh, store, hub, alert_log, pusher))
        asyncio.create_task(digest_loop(current, store, pusher, mailer, base_url=base_url()))


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events: new_story / development / breaking, plus a keep-alive every 25 s."""
    q = hub.subscribe()

    async def events():
        try:
            yield "retry: 10000\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                    yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            hub.unsubscribe(q)

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/api/unsubscribe/{user_id}/{token}")
@app.post("/api/unsubscribe/{user_id}/{token}")
def unsubscribe(user_id: str, token: str):
    """One-click unsubscribe from emailed briefings (linked in every email and in the List-Unsubscribe header)."""
    import hmac
    from fastapi.responses import HTMLResponse
    if not hmac.compare_digest(token, digest.unsubscribe_token(user_id)):
        raise HTTPException(403, "Invalid unsubscribe link")
    u = store.load_user(user_id)
    u.digest_channel = "push" if u.digest_channel == "both" else ("push" if u.digest_channel == "email" else u.digest_channel)
    u.email, u.email_confirmed = None, False
    store.save_user(u)
    return HTMLResponse("<p style='font:16px sans-serif;padding:30px'>You will not get Sift briefings by email any more. "
                        "<a href='/'>Back to Sift</a></p>")


@app.get("/sw.js")
def service_worker():
    return FileResponse(WEB / "sw.js", media_type="application/javascript")


@app.get("/api/push/key")
def push_key():
    return {"key": pusher.public, "enabled": pusher.enabled}


class PushSub(BaseModel):
    endpoint: str
    keys: dict


@app.post("/api/users/{user_id}/push")
def push_subscribe(user_id: str, sub: PushSub):
    store.add_push_subscription(user_id, sub.model_dump())
    return {"ok": True}


_refresh_lock = threading.Lock()   # one refresh at a time (the loop, the briefing timer and first visitors share it)


def refresh() -> Result:
    """Run the pipeline. Uses a fixture when NEWSINTEL_FIXTURE is set (offline demo), else live feeds."""
    with _refresh_lock:
        return _refresh()


def _refresh() -> Result:
    fixture = os.environ.get("NEWSINTEL_FIXTURE")
    reviewed = os.environ.get("NEWSINTEL_REVIEWED")
    if fixture:
        fx = FixtureAdapter(fixture)
        articles, now = list(fx.fetch()), fx.snapshot_time
        feeds = []
    else:
        rss = RSSAdapter()
        articles, now = list(rss.fetch()), datetime.now(timezone.utc)
        feeds = rss.report
    result = run(articles, now, [CacheSummarizer(store, reviewed), ClaudeSummarizer(store=store)])
    store.save_articles(result.articles)
    store.save_stories(result.stories)
    with _lock:
        _state.update(result=result, last_refresh=datetime.now(timezone.utc), feeds=feeds,
                      refreshes=_state["refreshes"] + 1)
    return result


def current() -> Result:
    if _state["result"]:
        return _state["result"]
    with _refresh_lock:   # if a refresh is already running, wait for it instead of starting another
        return _state["result"] or _refresh()


def _rows(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        j = story_json(r["story"])
        j.update({k: r[k] for k in ("score", "relevance", "why", "parts", "slot") if k in r})
        out.append(j)
    return out


def _page(request: Request, title: str = "Sift — less news, more signal",
          description: str = "Approved sources, duplicates merged, noise removed: the day's news as short cards that link to every original report.",
          path: str = "/") -> str:
    """index.html with absolute preview tags (WhatsApp, LinkedIn and X need full URLs)."""
    from html import escape
    root = base_url(request)
    tags = (f'<meta property="og:type" content="article"><meta property="og:site_name" content="Sift">'
            f'<meta property="og:title" content="{escape(title)}">'
            f'<meta property="og:description" content="{escape(description)}">'
            f'<meta property="og:url" content="{escape(root + path)}">'
            f'<meta property="og:image" content="{root}/icons/og-card.png">'
            f'<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta property="og:image:alt" content="Sift: less news, more signal">'
            f'<meta name="twitter:card" content="summary_large_image">'
            f'<meta name="description" content="{escape(description)}">')
    return (WEB / "index.html").read_text().replace("</head>", tags + "</head>", 1)


@app.get("/")
def index(request: Request):
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_page(request))


@app.get("/demo")
def demo():
    """A saved sample (real reports from 19 Nov 2026) with full AI summaries, for showing what the AI layer adds."""
    return FileResponse(WEB / "demo.html")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(WEB / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icons/{name}")
def icon(name: str):
    path = WEB / "icons" / name
    if not re.match(r"^[a-z0-9-]+\.png$", name) or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/health")
def health():
    """Proof of life: when the app last refreshed on its own, and how each feed did. No secrets."""
    r = _state["result"]
    return {"ok": True, "live_mode": os.environ.get("NEWSINTEL_LIVE") == "1",
            "started": _state["started"].isoformat(),
            "refreshes_since_start": _state["refreshes"],
            "last_refresh": _state["last_refresh"].isoformat() if _state["last_refresh"] else None,
            "stats": r.stats if r else None,
            "feeds": _state["feeds"],
            "push_enabled": pusher.enabled, "email_enabled": mailer.enabled,
            "claude_summaries": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "claude": _claude_status()}


def _claude_status() -> dict:
    """AI summaries: on or off, which model, today's calls and an estimated cost. Never shows the key."""
    from ..summarization import ClaudeSummarizer, estimated_cost, usage_today
    c = ClaudeSummarizer(store=store)
    today = usage_today(store)
    return {"on": bool(c.key), "model": c.model, "daily_limit": c.daily_limit, "per_refresh_limit": c.refresh_limit,
            "today_utc": {**today, "estimated_cost_usd": estimated_cost(today, c.model)}}


@app.get("/s/{slug}")
def shared_story(slug: str, request: Request):
    """A shared story link. Adds preview tags so WhatsApp, LinkedIn and X show the headline and
    summary, then the app opens that card first. The original publisher is credited in the preview."""
    from fastapi.responses import HTMLResponse

    story = next((s for s in current().stories if s.id == slug.replace("-", "_", 1) and not s.rejected), None)
    if story is None:
        return HTMLResponse(_page(request, path=f"/s/{slug}"))
    sm, lead = story.summary, story.factual_articles[0]
    return HTMLResponse(_page(request, title=sm.headline, description=f"{sm.summary} Source: {lead.publisher}.",
                              path=f"/s/{slug}"))


@app.get("/api/bundle")
def api_bundle():
    """Everything the web client needs; ranking for the signed-in user also runs client-side
    with the same formula, so preference changes re-rank instantly."""
    return result_json(current())


def require_admin(request: Request) -> None:
    """Owner-only actions need header X-Admin-Token matching the ADMIN_TOKEN set on the host."""
    import hmac
    token = os.environ.get("ADMIN_TOKEN")
    if not token or not hmac.compare_digest(request.headers.get("x-admin-token", ""), token):
        raise HTTPException(403, "This needs the admin token")


@app.post("/api/refresh")
def api_refresh(request: Request):
    """Manual refresh, for the owner only."""
    require_admin(request)
    return refresh().stats


class TestBriefing(BaseModel):
    to: str
    slot: str = "morning"


@app.post("/api/admin/test-briefing")
def admin_test_briefing(body: TestBriefing, request: Request):
    """Owner only: email a real briefing built from the current stories (covers the last 24 h)."""
    from datetime import timedelta
    from ..users import UserProfile
    require_admin(request)
    if not mailer.enabled:
        raise HTTPException(409, "Email is not configured (set SMTP_HOST, SMTP_USER, SMTP_PASSWORD, MAIL_FROM)")
    r = current()
    d = {"slot": body.slot, "time": "08:00" if body.slot == "morning" else "19:00", "on": True}
    rows = rank(r.stories, UserProfile(user_id="owner-test"))
    unsub = f"{base_url(request)}/"
    mail = digest.email_html(d, rows, r.now - timedelta(hours=24), base_url(request), unsub)
    if not mail:
        return {"sent": False, "reason": "no stories cleared the bar in the last 24 hours"}
    return {"sent": mailer.send(body.to.strip(), *mail, unsubscribe_url=unsub), "subject": mail[0]}


@app.post("/api/admin/test-push")
def admin_test_push(request: Request):
    """Owner only: send a test notification to devices that turned alerts on in the last 30 minutes."""
    require_admin(request)
    if not pusher.enabled:
        raise HTTPException(409, "Push is not available on this server")
    subs = store.recent_push_subscriptions(30)
    ok = sum(pusher.send(sub, {"title": "Sift test alert", "body": "Alerts work on this device.", "url": "/"})
             for _, sub in subs)
    from ..realtime import _forget_gone
    _forget_gone(store, pusher)
    return {"devices": len(subs), "delivered_to_push_service": ok}


@app.get("/api/feed/{user_id}")
def api_feed(user_id: str):
    r, user = current(), store.load_user(user_id)
    return {"generated_at": r.now.isoformat(), "stats": r.stats,
            "your_feed": _rows(your_feed(r.stories, user)),
            "important": _rows(whats_important(r.stories, user)),
            "breaking": [story_json(s) for s in breaking(r.stories)]}


@app.get("/api/catch-up/{user_id}")
def api_catch_up(user_id: str):
    r, user = current(), store.load_user(user_id)
    cm = catch_me_up(r.stories, user, r.now)
    user.last_seen = r.now
    store.save_user(user)
    return {"since": cm["since"].isoformat(), "skipped": cm["skipped"],
            **{k: [story_json(s) for s in cm[k]] for k in ("top", "worth_knowing", "watch")}}


@app.get("/api/story/{story_id}")
def api_story(story_id: str):
    for s in current().stories:
        if s.id == story_id and not s.rejected:
            return story_json(s)
    raise HTTPException(404, "Story not found")


class Prefs(BaseModel):
    interests: list[str] = []
    priority_interests: list[str] = []
    excluded_topics: list[str] = []
    regions: list[str] = []
    breaking_alerts: str = "major"
    personal_share: float | None = None
    digests: list[dict] = []
    timezone: str = "Asia/Kolkata"
    digest_channel: str = "push"
    email: str | None = None
    email_confirmed: bool = False   # read-only: ignored on PUT


@app.get("/api/users/{user_id}/preferences")
def get_prefs(user_id: str):
    u = store.load_user(user_id)
    return Prefs(interests=u.interests, priority_interests=u.priority_interests, excluded_topics=u.excluded_topics,
                 regions=u.regions, breaking_alerts=u.breaking_alerts, personal_share=u.personal_share,
                 digests=u.digests, timezone=u.timezone, digest_channel=u.digest_channel, email=u.email,
                 email_confirmed=u.email_confirmed)


@app.put("/api/users/{user_id}/preferences")
def put_prefs(user_id: str, prefs: Prefs, request: Request):
    if prefs.personal_share is not None and not 0.5 <= prefs.personal_share <= 0.9:
        raise HTTPException(422, "personal_share must be between 0.5 and 0.9")
    problems = digest.validate(prefs.digests)
    if prefs.digest_channel not in ("push", "email", "both"):
        problems.append("digest_channel must be push, email or both")
    if prefs.digest_channel in ("email", "both") and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", prefs.email or ""):
        problems.append("A valid email address is needed for email briefings")
    if problems:
        raise HTTPException(422, "; ".join(problems))
    u = store.load_user(user_id)
    old_email = (u.email or "").strip().lower()
    for k, v in prefs.model_dump().items():
        if k == "email_confirmed" or (k == "digests" and not v):
            continue  # confirmation only comes from the emailed link; keep schedule if none sent
        setattr(u, k, v)
    u.email = (u.email or "").strip().lower() or None
    if u.email != old_email:
        u.email_confirmed = False
    email_status = None
    if u.digest_channel in ("email", "both") and u.email:
        if u.email_confirmed:
            email_status = "confirmed"
        elif not mailer.enabled:
            email_status = "email_not_configured"
        else:
            # Double opt-in: nothing but this one message goes to an address until its owner clicks the link
            link = f"{base_url(request)}/api/confirm-email/{user_id}/{digest.confirm_token(user_id, u.email)}"
            if u.email != old_email or request.query_params.get("resend") == "1":
                mailer.send(u.email, *digest.confirmation_email(link),
                            unsubscribe_url=f"{base_url(request)}/api/unsubscribe/{user_id}/{digest.unsubscribe_token(user_id)}")
            email_status = "confirmation_sent"
    store.save_user(u)
    return {"ok": True, "email_status": email_status}


@app.get("/api/confirm-email/{user_id}/{token}")
def confirm_email(user_id: str, token: str):
    import hmac
    from fastapi.responses import HTMLResponse
    u = store.load_user(user_id)
    if not u.email or not hmac.compare_digest(token, digest.confirm_token(user_id, u.email)):
        raise HTTPException(403, "This confirmation link is invalid or out of date")
    u.email_confirmed = True
    store.save_user(u)
    return HTMLResponse("<p style='font:16px sans-serif;padding:30px'>Confirmed. Your Sift briefings will arrive at "
                        "the times you chose. <a href='/'>Back to Sift</a></p>")


class Feedback(BaseModel):
    story_id: str
    kind: str  # relevant | not_relevant | too_repetitive | too_trivial | more_like_this | hide_topic | hide_source | follow_topic | hide_similar


@app.post("/api/users/{user_id}/feedback")
def post_feedback(user_id: str, fb: Feedback):
    story = next((s for s in current().stories if s.id == fb.story_id), None)
    if story is None:
        raise HTTPException(404, "Story not found")
    topics = sorted(story_topics(story))
    src = story.factual_articles[0].source_id
    u = store.load_user(user_id)
    u.record(fb.story_id, fb.kind, topics, src)
    store.save_user(u)
    store.add_feedback(user_id, fb.story_id, fb.kind, topics, src)
    return {"ok": True, "undo": f"/api/users/{user_id}/feedback/{fb.story_id}"}


@app.delete("/api/users/{user_id}/feedback/{story_id}")
def undo_feedback(user_id: str, story_id: str):
    store.undo_feedback(user_id, story_id)
    return {"ok": True}


@app.put("/api/users/{user_id}/saved/{story_id}")
def save_story(user_id: str, story_id: str, saved: bool = True):
    store.set_saved(user_id, story_id, saved)
    return {"ok": True}


class Report(BaseModel):
    reason: str


@app.post("/api/story/{story_id}/report")
def report(story_id: str, body: Report, user_id: str | None = None):
    store.report(user_id, story_id, body.reason)
    return {"ok": True}


@app.get("/api/users/{user_id}/alerts")
def api_alerts(user_id: str):
    r, user = current(), store.load_user(user_id)
    return alerts_for(user, r.stories, alert_log, r.now)


@app.get("/api/ranking/{user_id}")
def api_ranking(user_id: str):
    """Debug view: every live story with its score breakdown."""
    r, user = current(), store.load_user(user_id)
    return [{"id": x["story"].id, "headline": x["story"].summary.headline, "score": x["score"], "parts": x["parts"],
             "why": x["why"]} for x in rank(r.stories, user)]
