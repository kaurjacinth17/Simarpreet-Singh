import copy
import os
from datetime import timedelta
from pathlib import Path

import pytest

from newsintel.feed import catch_me_up, your_feed, whats_important
from newsintel.ingestion import FixtureAdapter
from newsintel.pipeline import run
from newsintel.summarization import CacheSummarizer, validate
from newsintel.users import UserProfile

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures/articles_2026-11-19.json"
REV = ROOT / "fixtures/summaries_2026-11-19.json"


@pytest.fixture(scope="module")
def result():
    fx = FixtureAdapter(FIX)
    return run(list(fx.fetch()), fx.snapshot_time, [CacheSummarizer(None, REV)])


def by_article(result, article_id):
    return next(s for s in result.stories if any(a.id == article_id for a in s.articles))


# --- Stage 1: verification ------------------------------------------------------
def test_unapproved_and_marketing_sources_rejected(result):
    reasons = {a.id: a.rejected for a in result.articles if a.rejected}
    assert "Blocked domain" in reasons["a16"]          # Substack newsletter
    assert "Blocked domain" in reasons["a57"]          # unvetted site
    assert "brokerage" in reasons["a41"]               # broker market report with sign-up pitch
    assert "brokerage" in reasons["a42"]


def test_opinion_is_labelled_not_used_as_fact(result):
    apec = by_article(result, "a01")
    types = {a.id: a.content_type for a in apec.articles}
    assert types["a08"] == "opinion" and types["a07"] == "analysis"
    assert all(a.id not in {"a07", "a08"} for a in apec.factual_articles)


# --- Stage 2: clustering -----------------------------------------------------------
def test_same_event_merged_into_one_story(result):
    assert {a.id for a in by_article(result, "a01").articles} == {"a01", "a02", "a03", "a04", "a05", "a06", "a07", "a08"}
    assert {a.id for a in by_article(result, "a10").articles} == {"a10", "a11", "a12", "a13", "a14"}
    assert len(by_article(result, "a20").articles) == 7


def test_related_but_different_events_stay_apart(result):
    assert by_article(result, "a01").id != by_article(result, "a10").id   # APEC vs Iran war
    assert by_article(result, "a10").id != by_article(result, "a36").id   # Iran war vs oil prices
    assert by_article(result, "a15").id != by_article(result, "a10").id   # political gossip vs war reporting


def test_syndicated_copies_not_counted_twice(result):
    nvidia = by_article(result, "a20")
    # NVIDIA newsroom + IR site + SEC filing are one owner's voice, not independent confirmation
    owners = [a.owner for a in nvidia.factual_articles]
    assert owners.count("nvidia") == 2 and len(nvidia.independent_owners) < len(nvidia.factual_articles)


# --- Stages 3, 5, 6 ------------------------------------------------------------
def test_old_and_trivial_stories_rejected(result):
    assert by_article(result, "a43").rejected_stage == "freshness"     # a 2025 market report
    assert by_article(result, "a85").rejected_stage == "freshness"     # week-old CPI release
    assert by_article(result, "a15").rejected_stage == "quality"       # "said to privately vent"
    assert by_article(result, "a87").rejected_stage == "quality"       # routine bond notice


def test_breaking_needs_recent_major_credible(result):
    flagged = {s.id for s in result.live if s.breaking}
    assert by_article(result, "a01").id in flagged
    assert by_article(result, "a20").id not in flagged  # important but ~33h old
    assert by_article(result, "a65").id not in flagged  # recent but limited summary


def test_limited_summary_is_marked(result):
    pib = by_article(result, "a65")
    assert pib.summary.limited and "not available" in pib.summary.limited_reason


# --- Summary validation ------------------------------------------------------------
def test_validator_catches_invented_numbers(result):
    fed = copy.deepcopy(by_article(result, "a30"))
    fed.summary.summary = fed.summary.summary.replace("2% target", "3% target")
    assert any("not found" in p for p in validate(fed.summary, fed))


def test_validator_allows_flagged_rounding(result):
    oil = by_article(result, "a35")
    assert validate(oil.summary, oil) == []          # "about $92" vs reported 92.14
    bad = copy.deepcopy(oil)
    bad.summary.summary = bad.summary.summary.replace("around $92", "$97")
    assert validate(bad.summary, bad)


def test_all_shown_summaries_validate(result):
    for s in result.live:
        assert validate(s.summary, s) == [], s.summary.headline


# --- Feed ---------------------------------------------------------------------
def test_feed_reserves_global_slots_and_skips_weak_stories(result):
    user = UserProfile(interests=["Startups"], regions=["India"])
    feed = your_feed(result.stories, user)
    slots = [r["slot"] for r in feed]
    assert "global" in slots and "personal" in slots
    assert all(r["score"] >= 48 for r in feed)
    assert len(feed) < len(result.live)   # fewer stories rather than padding


def test_excluded_topics_hidden_from_feed_but_not_from_whats_important(result):
    user = UserProfile(interests=["World affairs"], excluded_topics=["conflict"])
    ids = {r["story"].id for r in your_feed(result.stories, user)}
    assert by_article(result, "a10").id not in ids
    assert by_article(result, "a10").id in {r["story"].id for r in whats_important(result.stories, user)}


def test_feedback_is_gradual_and_reversible(result):
    story = by_article(result, "a35")
    user = UserProfile(interests=["Finance"])
    from newsintel.scoring.relevance import relevance
    before, _ = relevance(story, user)
    user.record(story.id, "not_relevant", ["oil"])
    after, _ = relevance(story, user)
    assert 0 < after < before and after > before * 0.8      # one tap nudges, never wipes out
    user.undo(story.id)
    assert relevance(story, user)[0] == before


def test_catch_me_up_shape(result):
    cm = catch_me_up(result.stories, UserProfile(), result.now)
    assert len(cm["top"]) == 3 and len(cm["worth_knowing"]) == 2 and len(cm["watch"]) == 1
    assert cm["watch"][0].summary.status == "developing"


# --- API ------------------------------------------------------------------------
def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSINTEL_FIXTURE", str(FIX))
    monkeypatch.setenv("NEWSINTEL_REVIEWED", str(REV))
    monkeypatch.setenv("NEWSINTEL_DB", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    import importlib
    import newsintel.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    feed = c.get("/api/feed/u1").json()
    assert feed["your_feed"] and feed["breaking"]
    sid = feed["your_feed"][0]["id"]
    assert c.post("/api/users/u1/feedback", json={"story_id": sid, "kind": "relevant"}).json()["ok"]
    assert c.put(f"/api/users/u1/saved/{sid}").json()["ok"]
    assert c.put("/api/users/u1/preferences", json={"interests": ["Finance"], "personal_share": 0.95}).status_code == 422
    cu = c.get("/api/catch-up/u1").json()
    assert len(cu["top"]) == 3


# --- Real-time diff ---------------------------------------------------------------
def test_realtime_diff_reports_new_and_developing_stories(result):
    from newsintel.realtime import diff, fingerprint
    apec = by_article(result, "a01")
    fresh = next(s for s in result.live if s.id != apec.id)                # pretend this one just appeared
    baseline = {s.id: fingerprint(s) for s in result.live if s.id != fresh.id}
    baseline[apec.id] = dict(fingerprint(apec), timeline=1, breaking=False)  # it has since developed
    events = {e["id"]: e["type"] for e in diff(baseline, result.stories)}
    assert events[fresh.id] in {"new_story", "breaking"}
    assert events[apec.id] == "breaking"
    assert len(events) == 2                                                # unchanged stories stay quiet


def test_shared_story_link_has_preview_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSINTEL_FIXTURE", str(FIX))
    monkeypatch.setenv("NEWSINTEL_REVIEWED", str(REV))
    monkeypatch.setenv("NEWSINTEL_DB", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    import importlib
    import newsintel.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    sid = c.get("/api/feed/u1").json()["your_feed"][0]["id"]
    page = c.get("/s/" + sid.replace("_", "-")).text
    assert 'property="og:title"' in page and "Source:" in page


# --- Scheduled briefings --------------------------------------------------------
def test_briefing_schedule_rules():
    from datetime import datetime, timezone
    from newsintel import digest
    sched = [{"slot": "morning", "time": "08:00", "on": True}, {"slot": "evening", "time": "19:00", "on": True},
             {"slot": "extra", "time": "22:15", "on": True}]
    assert digest.validate(sched) == []
    assert digest.validate(sched + [{"slot": "extra", "time": "10:00"}, {"slot": "extra", "time": "11:00"}])  # 3 extras
    assert digest.validate([{"slot": "morning", "time": "8am"}])
    # 19:02 IST = 13:32 UTC -> evening briefing is due once
    now = datetime(2026, 11, 19, 13, 32, tzinfo=timezone.utc)
    due = digest.due(sched, "Asia/Kolkata", now, set())
    assert [d["time"] for d in due] == ["19:00"]
    assert digest.due(sched, "Asia/Kolkata", now, {due[0]["key"]}) == []        # never twice a day
    since = digest.previous_time(sched, "Asia/Kolkata", now)
    assert since.astimezone().hour is not None and since < now                  # covers news since 08:00


def test_briefing_lists_top_stories_or_stays_silent(result):
    from datetime import timedelta
    from newsintel import digest
    from newsintel.feed import rank
    rows = rank(result.stories, UserProfile())
    d = {"slot": "evening", "time": "19:00"}
    payload = digest.build(d, rows, result.now - timedelta(hours=24))
    assert payload["title"].startswith("Evening wrap · 3 things") and payload["body"].count("•") == 3
    assert digest.build(d, rows, result.now + timedelta(hours=1)) is None        # nothing new -> no notification


def test_api_rejects_bad_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSINTEL_FIXTURE", str(FIX))
    monkeypatch.setenv("NEWSINTEL_REVIEWED", str(REV))
    monkeypatch.setenv("NEWSINTEL_DB", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    import importlib
    import newsintel.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    bad = {"interests": ["Finance"], "digests": [{"slot": "morning", "time": "08:00"}, {"slot": "extra", "time": "08:00"}]}
    assert c.put("/api/users/u9/preferences", json=bad).status_code == 422
    good = {"interests": ["Finance"], "digests": [{"slot": "morning", "time": "07:30", "on": True}, {"slot": "extra", "time": "16:45", "on": True}]}
    assert c.put("/api/users/u9/preferences", json=good).status_code == 200
    assert c.get("/api/users/u9/preferences").json()["digests"][1]["time"] == "16:45"


def test_email_briefing_and_unsubscribe(result, tmp_path, monkeypatch):
    from datetime import timedelta
    from newsintel import digest
    from newsintel.feed import rank
    rows = rank(result.stories, UserProfile())
    mail = digest.email_html({"slot": "morning", "time": "08:00"}, rows, result.now - timedelta(hours=24),
                             "https://sift.example", "https://sift.example/u")
    subject, html, text = mail
    assert subject.startswith("Morning brief: ") and "Unsubscribe" in html and "https://sift.example/s/s-" in html
    assert "Source:" in html                                      # publishers stay credited in email too
    monkeypatch.setenv("NEWSINTEL_FIXTURE", str(FIX)); monkeypatch.setenv("NEWSINTEL_REVIEWED", str(REV))
    monkeypatch.setenv("NEWSINTEL_DB", str(tmp_path / "t.db"))
    from fastapi.testclient import TestClient
    import importlib
    import newsintel.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    assert c.put("/api/users/m1/preferences", json={"interests": ["Finance"], "digest_channel": "email", "email": "nope"}).status_code == 422
    assert c.put("/api/users/m1/preferences", json={"interests": ["Finance"], "digest_channel": "both", "email": "a@b.in"}).status_code == 200
    assert c.get("/api/unsubscribe/m1/wrong").status_code == 403
    assert c.get("/api/unsubscribe/m1/" + digest.unsubscribe_token("m1")).status_code == 200
    assert c.get("/api/users/m1/preferences").json()["digest_channel"] == "push"


def test_served_page_and_snapshot_embed_valid_json():
    """web/index.html must embed null (load from the API); built snapshots must embed real JSON."""
    import json
    import re
    pat = re.compile(r'<script id="snapshot" type="application/json">(.*?)</script>', re.S)
    assert json.loads(pat.search((ROOT / "web/index.html").read_text()).group(1)) is None
    snap = json.loads(pat.search((ROOT / "dist/sift-snapshot.html").read_text()).group(1))
    assert snap["stories"]


def test_failed_claude_summary_is_not_resent_every_refresh(result, tmp_path, monkeypatch):
    """A summary that fails validation must not trigger a paid model call on every refresh."""
    import copy as _copy
    from newsintel.db import Store
    from newsintel.summarization import ClaudeSummarizer, summarize

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    calls = []

    class FakeClaude(ClaudeSummarizer):
        def _call(self, story):
            calls.append(story.id)
            return {"headline": "Something happened", "summary": "Too short, with 999 invented."}

    store = Store(str(tmp_path / "t.db"))
    story = _copy.deepcopy(next(s for s in result.stories if not s.rejected))
    for _ in range(5):  # five refreshes, nothing changed
        out = summarize(story, [CacheSummarizer(store), FakeClaude(store=store)])
        assert out.limited  # falls back to the source's own description, clearly marked
    assert len(calls) == 1

    story.articles.append(_copy.deepcopy(story.articles[0]))
    story.articles[-1].url += "?new-report"  # a new article joins: fresh material, try again
    summarize(story, [CacheSummarizer(store), FakeClaude(store=store)])
    assert len(calls) == 2


# --- Deployment: health, PWA, previews, delivery ----------------------------------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NEWSINTEL_FIXTURE", str(FIX))
    monkeypatch.setenv("NEWSINTEL_REVIEWED", str(REV))
    monkeypatch.setenv("NEWSINTEL_DB", str(tmp_path / "data" / "t.db"))   # folder is created on first start
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://sift.example.app")
    for k in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "UNSUBSCRIBE_SECRET", "ADMIN_TOKEN", "SMTP_HOST"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    import importlib
    import newsintel.api as api
    importlib.reload(api)
    c = TestClient(api.app)
    c.api = api
    return c


def test_health_install_files_and_share_previews(client):
    h = client.get("/api/health").json()
    assert h["ok"] and "last_refresh" in h and "feeds" in h and h["push_enabled"]
    assert "ANTHROPIC" not in str(h) and "private" not in str(h).lower()
    m = client.get("/manifest.webmanifest").json()
    assert m["display"] == "standalone" and {i["sizes"] for i in m["icons"]} >= {"192x192", "512x512"}
    for i in m["icons"]:
        assert client.get(i["src"]).status_code == 200
    assert client.get("/icons/..%2Fsw.js").status_code == 404
    home = client.get("/").text
    assert 'rel="manifest"' in home and 'content="https://sift.example.app/icons/og-card.png"' in home
    story = client.get("/api/bundle").json()["stories"][0]
    shared = client.get("/s/" + story["id"].replace("_", "-", 1)).text
    assert f'property="og:title" content="{story["headline"]}"' in shared.replace("&#x27;", "'")
    assert 'og:url" content="https://sift.example.app/s/' in shared


def test_manual_refresh_needs_admin_token(client, monkeypatch):
    assert client.post("/api/refresh").status_code == 403
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    assert client.post("/api/refresh", headers={"X-Admin-Token": "wrong"}).status_code == 403
    assert client.post("/api/refresh", headers={"X-Admin-Token": "s3cret"}).status_code == 200


def test_each_device_has_its_own_profile(client):
    client.put("/api/users/dev-a/preferences", json={"interests": ["Sports"]})
    client.put("/api/users/dev-b/preferences", json={"interests": ["Finance"]})
    assert client.get("/api/users/dev-a/preferences").json()["interests"] == ["Sports"]
    assert client.get("/api/users/bad%20id!/preferences").status_code == 400


def test_email_briefings_need_confirmation(client):
    sent = []
    client.api.mailer.host = "smtp.test"           # pretend email is configured
    client.api.mailer.send = lambda to, subject, html, text, unsubscribe_url: sent.append((to, subject, text)) or True
    body = {"interests": ["Finance"], "digest_channel": "email", "email": "Reader@Example.org"}
    r = client.put("/api/users/dev-c/preferences", json=body).json()
    assert r["email_status"] == "confirmation_sent" and len(sent) == 1 and sent[0][0] == "reader@example.org"
    client.put("/api/users/dev-c/preferences", json=body)          # saving again does not re-send
    assert len(sent) == 1
    body["email_confirmed"] = True                                   # the client cannot confirm itself
    client.put("/api/users/dev-c/preferences", json=body)
    assert client.get("/api/users/dev-c/preferences").json()["email_confirmed"] is False
    link = next(w for w in sent[0][2].split() if "/api/confirm-email/" in w)
    assert link.startswith("https://sift.example.app/")
    assert client.get(link.replace("https://sift.example.app", "") + "x").status_code == 403
    assert client.get(link.replace("https://sift.example.app", "")).status_code == 200
    assert client.get("/api/users/dev-c/preferences").json()["email_confirmed"] is True
    body["email"] = "other@example.org"                              # a new address needs a new confirmation
    client.put("/api/users/dev-c/preferences", json=body)
    assert client.get("/api/users/dev-c/preferences").json()["email_confirmed"] is False and len(sent) == 2


def test_push_keys_are_generated_once_and_kept(tmp_path, monkeypatch):
    from newsintel import serverkeys
    from newsintel.db import Store
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    serverkeys.init(Store(str(tmp_path / "k.db")))
    first = serverkeys.vapid_keys()
    serverkeys.init(Store(str(tmp_path / "k.db")))                   # a restart reads the same keys back
    assert serverkeys.vapid_keys() == first and len(first[0]) == 87 and len(first[1]) == 43
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "envpub")
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "envpriv")
    assert serverkeys.vapid_keys() == ("envpub", "envpriv")         # host settings win


def test_push_is_signed_encrypted_and_forgets_dead_devices(tmp_path, monkeypatch):
    """Sends a real Web Push request to a local stand-in for Google's/Apple's push service."""
    import base64
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from newsintel import serverkeys
    from newsintel.db import Store
    from newsintel.realtime import PushSender

    got, status = [], {"code": 201}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(({k.lower(): v for k, v in self.headers.items()}, self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status["code"]); self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
    device = ec.generate_private_key(ec.SECP256R1())
    sub = {"endpoint": f"http://127.0.0.1:{srv.server_port}/push/abc",
           "keys": {"p256dh": b64(device.public_key().public_bytes(serialization.Encoding.X962,
                                                                     serialization.PublicFormat.UncompressedPoint)),
                    "auth": b64(b"0123456789abcdef")}}
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    serverkeys.init(Store(str(tmp_path / "p.db")))
    pusher = PushSender()
    assert pusher.send(sub, {"title": "Test alert", "body": "Hello", "url": "/"})
    headers, body = got[0]
    assert headers["authorization"].startswith("vapid t=") and pusher.public in headers["authorization"]
    assert headers["content-encoding"] == "aes128gcm" and b"Test alert" not in body   # encrypted end to end
    status["code"] = 410                                              # the device uninstalled the app
    assert not pusher.send(sub, {"title": "x", "body": "y", "url": "/"})
    assert pusher.gone == [sub["endpoint"]]
    srv.shutdown()


def test_live_loop_keeps_refreshing_on_its_own(result, tmp_path, monkeypatch):
    """No visitors needed: the loop refreshes every few minutes, and one failed refresh doesn't stop it."""
    import asyncio
    from newsintel import realtime
    from newsintel.db import Store
    from newsintel.notify import AlertLog

    calls, waits = [], []

    def refresh():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("feeds down")
        return result

    async def fake_sleep(seconds):
        waits.append(seconds)
        if len(waits) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(realtime.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(realtime.run_loop(refresh, Store(str(tmp_path / "l.db")), realtime.Hub(), AlertLog(),
                                      realtime.PushSender(public="p", private=None)))
    assert len(calls) == 4 and all(w in (120, 300) for w in waits)


def test_owner_test_briefing_and_push(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cret")
    H = {"X-Admin-Token": "s3cret"}
    assert client.post("/api/admin/test-briefing", json={"to": "me@example.org"}).status_code == 403
    assert client.post("/api/admin/test-briefing", json={"to": "me@example.org"}, headers=H).status_code == 409
    sent = []
    client.api.mailer.host = "smtp.test"
    client.api.mailer.send = lambda to, subject, html, text, unsubscribe_url: sent.append((to, subject, html)) or True
    r = client.post("/api/admin/test-briefing", json={"to": "me@example.org"}, headers=H).json()
    assert r["sent"] and sent[0][0] == "me@example.org" and r["subject"].startswith("Morning brief:")
    assert "https://sift.example.app/s/" in sent[0][2]                  # links point at the public app
    client.post("/api/users/dev-p/push", json={"endpoint": "https://push.example/1", "keys": {"p256dh": "x", "auth": "y"}})
    pushed = []
    client.api.pusher.send = lambda sub, payload: pushed.append(sub["endpoint"]) or True
    assert client.post("/api/admin/test-push", headers=H).json() == {"devices": 1, "delivered_to_push_service": 1}


def test_health_explains_feeds_whose_items_have_no_readable_date(monkeypatch):
    """An item without a readable date is dropped (never guessed), and the report says what the feed sent."""
    import httpx
    from newsintel.ingestion import RSSAdapter
    from newsintel import ingestion
    xml = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
           b'<item><title>Press release one</title><link>https://www.rbi.org.in/x?prid=1</link>'
           b'<pubDate>not-a-date</pubDate></item>'
           b'<item><title>Press release two</title><link>https://www.rbi.org.in/x?prid=2</link></item>'
           b'</channel></rss>')
    src = ingestion.registry().get("rbi")
    monkeypatch.setattr(ingestion, "registry", lambda: type("R", (), {"feeds": lambda self: [(src, "https://www.rbi.org.in/f.xml")]})())
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, content=xml, request=httpx.Request("GET", a[0])))
    rss = RSSAdapter()
    assert list(rss.fetch()) == []
    rep = rss.report[0]
    assert rep["items"] == 2 and rep["dated_items"] == 0
    assert rep["undated_sample"]["raw_dates"]["published"] == "not-a-date"
    assert {"title", "link"} <= set(rep["undated_sample"]["fields"])
