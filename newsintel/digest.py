"""Scheduled briefings ("digests").

Each user has up to three named slots (morning, afternoon, evening) plus up
to two extra times of their own. At each due time they get ONE notification
listing the top stories for them since their previous briefing. If nothing
new cleared the bar, nothing is sent, so a briefing always carries news.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Story

NAMED = ("morning", "afternoon", "evening")
MAX_EXTRA = 2
DEFAULT_DIGESTS = [
    {"slot": "morning", "time": "08:00", "on": True},
    {"slot": "afternoon", "time": "13:30", "on": False},
    {"slot": "evening", "time": "19:00", "on": True},
]
LABEL = {"morning": "Morning brief", "afternoon": "Afternoon update", "evening": "Evening wrap"}
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(digests: list[dict]) -> list[str]:
    """Problems with a user's briefing schedule; empty list means it is fine."""
    errs = []
    extra = [d for d in digests if d.get("slot") == "extra"]
    if len(extra) > MAX_EXTRA:
        errs.append(f"At most {MAX_EXTRA} extra times")
    for d in digests:
        if d.get("slot") not in NAMED + ("extra",):
            errs.append(f"Unknown slot {d.get('slot')!r}")
        if not _HHMM.match(str(d.get("time", ""))):
            errs.append(f"Time {d.get('time')!r} must be HH:MM (24-hour)")
    times = [d["time"] for d in digests if d.get("on", True) and _HHMM.match(str(d.get("time", "")))]
    if len(times) != len(set(times)):
        errs.append("Two briefings are set for the same time")
    return errs


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def due(digests: list[dict], tz: str, now_utc: datetime, sent: set[str], window_min: int = 5) -> list[dict]:
    """Briefings whose time fell within the last `window_min` minutes (user's local time) and
    have not been sent today. `sent` holds keys like '2026-11-19|19:00'."""
    local = now_utc.astimezone(ZoneInfo(tz))
    now_m = local.hour * 60 + local.minute
    out = []
    for d in digests:
        if not d.get("on", True):
            continue
        key = f"{local.date()}|{d['time']}"
        if key in sent:
            continue
        if 0 <= now_m - _minutes(d["time"]) < window_min:
            out.append(dict(d, key=key))
    return out


def label(d: dict) -> str:
    return LABEL.get(d["slot"]) or (d.get("name") or "Your briefing")


def build(d: dict, ranked: list[dict], since: datetime, max_items: int = 3) -> dict | None:
    """ranked: rows from feed.rank() for this user. Returns a push payload, or None if nothing new."""
    fresh: list[Story] = [r["story"] for r in ranked if r["story"].last_updated >= since]
    if not fresh:
        return None
    top = fresh[:max_items]
    more = len(fresh) - len(top)
    lines = [f"• {s.summary.headline}" for s in top]
    if more > 0:
        lines.append(f"+ {more} more in Sift")
    n = len(top)
    return {"title": f"{label(d)} · {n} thing{'s' if n > 1 else ''} to know", "body": "\n".join(lines),
            "url": "/", "tag": f"digest-{d['time']}"}


def previous_time(digests: list[dict], tz: str, now_utc: datetime) -> datetime:
    """Start of the window a briefing covers: the previous enabled briefing time, else 24 h ago."""
    local = now_utc.astimezone(ZoneInfo(tz))
    times = sorted(_minutes(d["time"]) for d in digests if d.get("on", True))
    now_m = local.hour * 60 + local.minute
    earlier = [t for t in times if t < now_m - 4]
    if earlier:
        t = earlier[-1]
        return (local.replace(hour=t // 60, minute=t % 60, second=0, microsecond=0)).astimezone(ZoneInfo("UTC"))
    if times:  # previous day's last briefing
        t = times[-1]
        prev = (local - timedelta(days=1)).replace(hour=t // 60, minute=t % 60, second=0, microsecond=0)
        return prev.astimezone(ZoneInfo("UTC"))
    return now_utc - timedelta(hours=24)


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------
def email_html(d: dict, ranked: list[dict], since: datetime, base_url: str, unsubscribe_url: str,
               max_items: int = 5) -> tuple[str, str, str] | None:
    """Subject, HTML and plain-text bodies for an emailed briefing, or None if nothing new."""
    from html import escape

    fresh = [r["story"] for r in ranked if r["story"].last_updated >= since][:max_items]
    if not fresh:
        return None
    subject = f"{label(d)}: {fresh[0].summary.headline}" + (f" + {len(fresh) - 1} more" if len(fresh) > 1 else "")
    items_html, items_txt = [], []
    for s in fresh:
        link = f"{base_url}/s/{s.id.replace('_', '-', 1)}"
        lead = s.factual_articles[0]
        items_html.append(
            f'<tr><td style="padding:16px 0;border-bottom:1px solid #e3e6eb">'
            f'<div style="font:600 11px/1.4 monospace;letter-spacing:.06em;text-transform:uppercase;color:#5a6372">{escape(s.importance_label)} · {escape(s.summary.category)}</div>'
            f'<a href="{link}" style="font:600 19px/1.3 Georgia,serif;color:#12151b;text-decoration:none">{escape(s.summary.headline)}</a>'
            f'<p style="font:15px/1.5 Arial,sans-serif;color:#30363f;margin:6px 0">{escape(s.summary.why_it_matters or s.summary.summary)}</p>'
            f'<div style="font:13px Arial,sans-serif;color:#5a6372">Source: <a href="{lead.url}" style="color:#2441c9">{escape(lead.publisher)}</a>'
            f'{f" + {len(s.independent_owners) - 1} more" if len(s.independent_owners) > 1 else ""} · <a href="{link}" style="color:#2441c9">Explainer in Sift</a></div></td></tr>')
        items_txt.append(f"- {s.summary.headline}\n  {s.summary.why_it_matters}\n  {link}")
    html = (f'<div style="max-width:560px;margin:0 auto;padding:20px;background:#fff">'
            f'<div style="font:italic 700 26px Georgia,serif;color:#12151b">Sift<span style="color:#2441c9">.</span></div>'
            f'<div style="font:13px Arial,sans-serif;color:#5a6372;margin:4px 0 8px">{escape(label(d))} · {len(fresh)} things to know</div>'
            f'<table width="100%" cellpadding="0" cellspacing="0">{"".join(items_html)}</table>'
            f'<p style="font:12px Arial,sans-serif;color:#8a929f;margin-top:18px">You chose to get this at {d["time"]}. '
            f'<a href="{base_url}/#briefings" style="color:#8a929f">Change times</a> · '
            f'<a href="{unsubscribe_url}" style="color:#8a929f">Unsubscribe from emails</a></p></div>')
    text = f"{label(d)}\n\n" + "\n\n".join(items_txt) + f"\n\nUnsubscribe: {unsubscribe_url}"
    return subject, html, text


class EmailSender:
    """SMTP delivery (works with Gmail/Workspace app passwords, Amazon SES, Resend, SendGrid, Zoho...).

    Env: SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, MAIL_FROM. Without them it only logs.
    Adds List-Unsubscribe headers, which Gmail and Yahoo require from bulk senders.
    """

    def __init__(self):
        import os
        self.host, self.port = os.environ.get("SMTP_HOST"), int(os.environ.get("SMTP_PORT", "587"))
        self.user, self.password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        self.sender = os.environ.get("MAIL_FROM", "Sift <briefing@example.com>")

    @property
    def enabled(self) -> bool:
        return bool(self.host)

    def send(self, to: str, subject: str, html: str, text: str, unsubscribe_url: str) -> bool:
        import logging
        import smtplib
        from email.message import EmailMessage

        if not self.enabled:
            logging.getLogger("digest").info("email (dry run) to %s: %s", to, subject)
            return False
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=20) as s:
                s.starttls()
                if self.user:
                    s.login(self.user, self.password)
                s.send_message(msg)
            return True
        except Exception as exc:
            logging.getLogger("digest").warning("email failed to %s: %s", to, exc)
            return False


def _sign(message: str) -> str:
    import hashlib
    import hmac
    from . import serverkeys
    return hmac.new(serverkeys.app_secret().encode(), message.encode(), hashlib.sha256).hexdigest()[:24]


def unsubscribe_token(user_id: str) -> str:
    return _sign(user_id)


def confirm_token(user_id: str, email: str) -> str:
    """Signs (user, address) so a confirmation link only ever confirms the address it was sent to."""
    return _sign(f"confirm|{user_id}|{email.strip().lower()}")


def confirmation_email(confirm_url: str) -> tuple[str, str, str]:
    subject = "Confirm your Sift briefings"
    text = ("Someone (hopefully you) asked for Sift news briefings at this address.\n\n"
            f"Confirm: {confirm_url}\n\nIf this wasn't you, ignore this email and you won't hear from us again.")
    html = ('<div style="font:16px/1.5 -apple-system,Segoe UI,sans-serif;max-width:520px;padding:24px;color:#12151B">'
            '<p style="font:italic 700 26px Georgia,serif;margin:0 0 12px">Sift<span style="color:#2441C9">.</span></p>'
            '<p>Someone (hopefully you) asked for Sift news briefings at this address.</p>'
            f'<p><a href="{confirm_url}" style="display:inline-block;background:#12151B;color:#fff;padding:10px 18px;'
            'border-radius:999px;text-decoration:none">Confirm briefings</a></p>'
            '<p style="color:#5A6372;font-size:13px">If this wasn\'t you, ignore this email and you won\'t hear from us again.</p></div>')
    return subject, html, text
