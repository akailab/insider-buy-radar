"""E-mail alerts for new high-scoring trades (Gmail or any SMTP server)."""
from __future__ import annotations

import html
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .engine import money


def smtp_settings() -> dict | None:
    s = {
        "to": os.environ.get("ALERT_EMAIL_TO", "").strip(),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip(),
        "host": os.environ.get("SMTP_HOST", "").strip() or "smtp.gmail.com",
        "port": int(os.environ.get("SMTP_PORT", "").strip() or 465),
        "security": (os.environ.get("SMTP_SECURITY", "").strip() or "ssl").lower(),
    }
    if not s["to"] or (s["security"] != "none" and not (s["user"] and s["password"])):
        return None
    return s


def person(name: str) -> str:
    """SEC lists people as 'LAST FIRST MIDDLE' -> 'First Middle Last'."""
    parts = (name or "").split()
    return " ".join(parts[1:] + parts[:1]).title() if len(parts) > 1 else (name or "").title()


def _stake(r: dict) -> str:
    if r.get("new_position"):
        return "new position"
    if r.get("pct_increase") is not None:
        return f"+{r['pct_increase'] * 100:.0f}% stake"
    return ""


def build_email(rows: list[dict], app_url: str, min_score: float) -> tuple[str, str, str]:
    top = rows[0]
    subject = (f"Insider Radar: {top['ticker'] or top['company']} {top['role']} bought {money(top['value'])} "
               f"(score {round(top['score'])})")
    if len(rows) > 1:
        subject += f" + {len(rows) - 1} more"
    cards, lines = [], []
    for r in rows[:15]:
        link = f"{app_url}#t={r['id']}" if app_url else r["url"]
        why = "".join(f"<li>{html.escape(x)}</li>" for x in r["reasons"][1:4])
        flags = ", ".join(r.get("flags") or [])
        cards.append(f"""
<tr><td style="padding:14px 0;border-bottom:1px solid #e5e7eb">
  <div style="font-size:18px;font-weight:700">{html.escape(r['ticker'] or '')} <span style="color:#6b7280;font-weight:400;font-size:14px">{html.escape(r['company'])}</span></div>
  <div style="margin:4px 0;font-size:14px"><b style="color:#0f7b4a">Score {round(r['score'])}</b> · {html.escape(person(r['insider']))} ({html.escape(r['role'])}) bought <b>{money(r['value'])}</b> {html.escape(_stake(r))} on {r['trade_date']}</div>
  <ul style="margin:6px 0 6px 18px;padding:0;color:#374151;font-size:13px">{why}</ul>
  {f'<div style="font-size:12px;color:#8a5a00">⚠ {html.escape(flags)}</div>' if flags else ''}
  <a href="{html.escape(link)}" style="font-size:13px">Open in Insider Radar</a> · <a href="{html.escape(r['url'])}" style="font-size:13px">SEC filing</a>
</td></tr>""")
        lines.append(f"{r['ticker']} {r['role']} {person(r['insider'])} bought {money(r['value'])} — score {round(r['score'])} — {link}")
    body_html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111827;max-width:620px">
<p style="font-size:15px">{len(rows)} new C-suite purchase{'s' if len(rows) > 1 else ''} scored {min_score:.0f}+ since the last check.</p>
<table style="width:100%;border-collapse:collapse">{''.join(cards)}</table>
<p style="font-size:12px;color:#6b7280;margin-top:18px">Insider Buy Radar · data from SEC Form 4 filings · not investment advice.
{f'<br><a href="{html.escape(app_url)}">Open the app</a>' if app_url else ''}</p></body></html>"""
    body_text = "\n".join(lines) + "\n\nNot investment advice."
    return subject, body_html, body_text


def send(rows: list[dict], app_url: str, min_score: float, settings: dict) -> None:
    subject, body_html, body_text = build_email(rows, app_url, min_score)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Insider Radar <{settings['user'] or settings['to']}>"
    msg["To"] = settings["to"]
    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    recipients = [a.strip() for a in settings["to"].split(",") if a.strip()]
    if settings["security"] == "ssl":
        with smtplib.SMTP_SSL(settings["host"], settings["port"], context=ssl.create_default_context(), timeout=30) as s:
            s.login(settings["user"], settings["password"])
            s.sendmail(msg["From"], recipients, msg.as_string())
    else:
        with smtplib.SMTP(settings["host"], settings["port"], timeout=30) as s:
            if settings["security"] == "starttls":
                s.starttls(context=ssl.create_default_context())
            if settings["user"] and settings["password"]:
                s.login(settings["user"], settings["password"])
            s.sendmail(msg["From"], recipients, msg.as_string())
