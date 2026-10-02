"""E-mail alerts (Gmail or any SMTP server): new high-scoring insider buys, and Congress purchases
whose stock has fallen below the member's estimated purchase price."""
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
    deliver(*build_email(rows, app_url, min_score), settings)


# ------------------------------------------------------------------ Congress price-drop alerts
def honor(t: dict) -> str:
    title = "Sen." if t.get("chamber") == "Senate" else "Rep."
    tag = "-".join(x for x in (t.get("party"), t.get("state")) if x)
    return f"{title} {t.get('member', '')}" + (f" ({tag})" if tag else "")


def _px(v: float | None) -> str:
    return f"${v:,.2f}" if v is not None else "?"


def build_congress_email(trades: list[dict], app_url: str, drop_pct: float) -> tuple[str, str, str]:
    trades = sorted(trades, key=lambda t: t.get("price_change") or 0)  # biggest drop first
    top = trades[0]
    subject = (f"Congress alert: {top['ticker']} is {abs(top['price_change']) * 100:.1f}% below "
               f"{honor(top).split(' (')[0]}'s purchase price")
    if len(trades) > 1:
        subject += f" + {len(trades) - 1} more"
    cards, lines = [], []
    for t in trades[:20]:
        link = f"{app_url}#c={t['id']}" if app_url else t["url"]
        who = honor(t) + ("" if t.get("owner", "Self") == "Self" else f" · {t['owner'].lower()}'s account")
        extra = []
        if t.get("oversight"):
            extra.append("Sits on a committee that oversees this industry")
        if t.get("key_person"):
            extra.append("Party leader or committee chair")
        cards.append(f"""
<tr><td style="padding:14px 0;border-bottom:1px solid #e5e7eb">
  <div style="font-size:18px;font-weight:700">{html.escape(t['ticker'])} <span style="color:#6b7280;font-weight:400;font-size:14px">{html.escape(t.get('company') or '')}</span></div>
  <div style="margin:4px 0;font-size:14px">{html.escape(who)} bought <b>{html.escape(t.get('amount') or '')}</b> on {t['trade_date']}</div>
  <div style="margin:4px 0;font-size:14px">Est. buy price <b>{_px(t['buy_price'])}</b> → now <b>{_px(t['price_now'])}</b>
    <b style="color:#b42318">({t['price_change'] * 100:+.1f}%)</b></div>
  {''.join(f'<div style="font-size:12px;color:#8a5a00">• {html.escape(x)}</div>' for x in extra)}
  <a href="{html.escape(link)}" style="font-size:13px">Open in Insider Radar</a> · <a href="{html.escape(t['url'])}" style="font-size:13px">Disclosure</a>
</td></tr>""")
        lines.append(f"{t['ticker']}: {who} bought {t.get('amount', '')} on {t['trade_date']}; est. buy {_px(t['buy_price'])}, "
                     f"now {_px(t['price_now'])} ({t['price_change'] * 100:+.1f}%) - {link}")
    more = len(trades) - 20
    rule = "below" if not drop_pct else f"at least {drop_pct:g}% below"
    body_html = f"""<html><body style="font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;color:#111827;max-width:620px">
<p style="font-size:15px">{len(trades)} stock{'s' if len(trades) > 1 else ''} bought by members of Congress {'are' if len(trades) > 1 else 'is'} now trading {rule} the estimated purchase price.</p>
<table style="width:100%;border-collapse:collapse">{''.join(cards)}</table>
{f'<p style="font-size:13px">…and {more} more in the app (Congress tab, "Below buy price" filter).</p>' if more > 0 else ''}
<p style="font-size:12px;color:#6b7280;margin-top:18px">Members of Congress disclose only a dollar range, not the price they paid, so the
purchase price is estimated as the stock's closing price on the trade date. You get one e-mail per trade.
Insider Radar · not investment advice.{f'<br><a href="{html.escape(app_url)}">Open the app</a>' if app_url else ''}</p></body></html>"""
    body_text = "\n".join(lines) + ("\n...and %d more in the app." % more if more > 0 else "") + \
        "\n\nPurchase price = closing price on the trade date (disclosures give only a range). Not investment advice."
    return subject, body_html, body_text


def send_congress(trades: list[dict], app_url: str, drop_pct: float, settings: dict) -> None:
    deliver(*build_congress_email(trades, app_url, drop_pct), settings)


def deliver(subject: str, body_html: str, body_text: str, settings: dict) -> None:
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
