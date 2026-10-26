"""Slack webhook + email alerts."""
import os
import smtplib
from email.message import EmailMessage

import httpx


def build(domain, posts, pricing, first, total, warn, slack=True):
    link = (lambda u, t: f"<{u}|{t.replace('|', '/').replace('<', '').replace('>', '')}>") if slack \
        else (lambda u, t: f"{t}\n    {u}")
    bold = (lambda s: f"*{s}*") if slack else (lambda s: s)
    L = []
    if posts:
        if first:
            L.append(f"{bold(domain)} - initial scan: {total} posts indexed. Latest {len(posts)}:")
        else:
            L.append(f"{bold(domain)} published {len(posts)} new post{'s' if len(posts) != 1 else ''} "
                     f"since the last check:")
        for p in posts[:15]:
            vol = f"{p['volume']:,}/mo" if p.get("volume") is not None else "n/a"
            L.append(f"• {link(p['url'], p['title'])} ({p.get('published') or 'date n/a'})\n"
                     f"    keyword: {p.get('keyword') or 'n/a'} | search volume: {vol}")
        if len(posts) > 15:
            L.append(f"...and {len(posts) - 15} more")
    if pricing:
        L.append(f"\n{bold('Pricing change detected')}: {pricing['url']}")
        if pricing["added"]:
            L.append("  added: " + ", ".join(pricing["added"]))
        if pricing["removed"]:
            L.append("  removed: " + ", ".join(pricing["removed"]))
    if warn:
        L.append(f"\nNote: {warn}")
    return "\n".join(L)


def _slack(url, text):
    r = httpx.post(url, json={"text": text}, timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"Slack returned {r.status_code}: {r.text[:80]}")


def _email(to, subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP is not configured on the server")
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER"), to, subject
    m.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    with (smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)) as s:
        if port != 465:
            s.starttls()
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
        s.send_message(m)


def dispatch(domain, posts, pricing, first, total, warn, slack_url=None, email=None):
    results = []
    slack_url = slack_url or os.getenv("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            if not slack_url.startswith("https://hooks.slack.com/"):
                raise RuntimeError("Not a Slack incoming-webhook URL")
            _slack(slack_url, build(domain, posts, pricing, first, total, warn, True))
            results.append({"channel": "slack", "ok": True})
        except Exception as e:
            results.append({"channel": "slack", "ok": False, "error": str(e)})
    if email:
        try:
            n = len(posts)
            _email(email, f"{domain}: {n} new post{'s' if n != 1 else ''}" if posts else f"{domain}: pricing changed",
                   build(domain, posts, pricing, first, total, warn, False))
            results.append({"channel": "email", "ok": True})
        except Exception as e:
            results.append({"channel": "email", "ok": False, "error": str(e)})
    if not results:
        results.append({"channel": "none", "ok": False, "error": "No Slack webhook or email configured"})
    return results
