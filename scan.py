#!/usr/bin/env python3
"""Daily scan (used by the GitHub workflow; also runs on your own computer).

    SEC_CONTACT_EMAIL=you@example.com python3 scan.py --days 30 --alert

Writes docs/data/latest.json (read by the iPhone/desktop app) and, with --alert, e-mails any
new trade scoring at or above --min-score, plus any Congress purchase whose stock has fallen
below the member's estimated purchase price (see congress_alerts). Alert settings come from environment variables:
ALERT_EMAIL_TO, SMTP_USER, SMTP_PASSWORD (a Gmail app password), optional SMTP_HOST/SMTP_PORT.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from radar import alerts  # noqa: E402
from radar.congress import run_congress_scan  # noqa: E402
from radar.engine import run_scan  # noqa: E402
from radar.sec_client import SecClient, SecError  # noqa: E402

DATA = HERE / "docs" / "data"


def app_url() -> str:
    if os.environ.get("APP_URL"):
        return os.environ["APP_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="look-back window in calendar days (default 30)")
    ap.add_argument("--deep-min", type=float, default=50_000, help="check insider history for buys above this $ (default 50000)")
    ap.add_argument("--alert", action="store_true", help="e-mail new trades at or above --min-score, and Congress buys now below their purchase price")
    ap.add_argument("--min-score", type=float, default=float(os.environ.get("ALERT_MIN_SCORE") or 70))
    ap.add_argument("--no-prices", action="store_true", help="skip Yahoo Finance price data")
    ap.add_argument("--out", default=str(DATA / "latest.json"))
    ap.add_argument("--no-congress", action="store_true", help="skip the Congress trades feed")
    ap.add_argument("--congress-days", type=int, default=90, help="Congress trades disclosed in the last N days (default 90)")
    args = ap.parse_args()

    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not email:
        print("Set SEC_CONTACT_EMAIL (the SEC requires a contact e-mail).", file=sys.stderr)
        return 2
    client = SecClient(email)
    client.cache.prune()
    t0 = time.time()
    last = [0.0]

    def progress(stage: str, done: int, total: int) -> None:
        if time.time() - last[0] > 15 or done == total:
            last[0] = time.time()
            print(f"[{time.time() - t0:6.0f}s] {stage}: {done}/{total}", flush=True)

    try:
        result = run_scan(client, days=args.days, include_live=True, deep_min=args.deep_min,
                          use_prices=not args.no_prices, progress=progress)
    except SecError as e:
        # Keep the app working: publish the previous scan with a notice, and still build the Congress feed.
        print(f"::error title=Insider scan could not reach the SEC::{e}", flush=True)
        try:
            result = json.loads((client.cache_dir / "latest.json").read_text())
        except (OSError, ValueError):
            result = {"meta": {"scanned_at": datetime.now().isoformat(timespec="seconds"), "from": "", "to": "",
                               "filings_checked": 0, "days_from_index": [], "weights": {}, "labels": {}}, "rows": []}
        result["meta"]["refresh_error"] = str(e)
        result["meta"]["refresh_failed_at"] = datetime.now().isoformat(timespec="seconds")
    else:
        result["meta"].pop("refresh_error", None)
    result["meta"]["app_url"] = app_url()
    write_json(Path(args.out), result)
    write_json(client.cache_dir / "latest.json", result)  # kept with the cache so every deploy can publish it
    rows = result["rows"]
    print(f"Wrote {len(rows)} C-suite buys to {args.out} in {time.time() - t0:.0f}s")
    # Small heartbeat file (committed by the workflow so the schedule stays active)
    write_json(DATA / "status.json", {"last_scan": result["meta"]["scanned_at"], "buys": len(rows),
                                      "very_strong": sum(1 for r in rows if r["score"] >= 70),
                                      "filings_checked": result["meta"]["filings_checked"],
                                      "refresh_error": result["meta"].get("refresh_error")})

    cres = None
    if not args.no_congress:
        try:
            prev = json.loads((client.cache_dir / "congress.json").read_text())
        except (OSError, ValueError):
            prev = None
        try:
            cres = run_congress_scan(client, days=args.congress_days, insider_rows=rows, previous=prev, progress=progress,
                                     use_prices=not args.no_prices)
            write_json(DATA / "congress.json", cres)
            write_json(client.cache_dir / "congress.json", cres)
            src = ", ".join(f"{k}: {'ok' if v.get('ok') else 'FAILED - ' + v.get('error', '')}" for k, v in cres["meta"]["sources"].items())
            print(f"Congress: {len(cres['trades'])} stock trades ({src})")
            px = cres["meta"].get("prices") or {}
            if px:
                print(f"Congress prices: {px}")
        except Exception as e:  # never let this break the insider scan
            cres = None
            print(f"Congress feed skipped: {e}")

    if args.alert:
        state_path = DATA / "alerted.json"
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {"ids": []}
        already = set(state.get("ids", []))
        new = [r for r in rows if r["score"] >= args.min_score and r["id"] not in already]
        settings = alerts.smtp_settings()
        if not new:
            print("No new trades for an alert.")
        elif not settings:
            print(f"{len(new)} new strong trades, but e-mail isn't configured (ALERT_EMAIL_TO / SMTP_USER / SMTP_PASSWORD).")
        else:
            alerts.send(new, app_url(), args.min_score, settings)
            print(f"E-mailed {len(new)} new trade(s) to {settings['to']}")
            state["ids"] = (state.get("ids", []) + [r["id"] for r in new])[-3000:]
            state["updated"] = datetime.now().isoformat(timespec="seconds")
            write_json(state_path, state)
        if cres and os.environ.get("CONGRESS_ALERTS", "on").strip().lower() not in ("off", "0", "no", "false"):
            congress_alerts(cres["trades"], settings)
    return 0


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def congress_alerts(trades: list[dict], settings: dict | None) -> None:
    """E-mail Congress purchases whose stock now trades below the member's estimated purchase price.
    Each trade is e-mailed once. Settings (optional GitHub variables):
    CONGRESS_DROP_PCT - how far below, in % (default 0 = any amount below)
    CONGRESS_ALERT_MIN_AMOUNT - smallest disclosed amount to watch (default 15001 = skip $1K-$15K trades)."""
    drop = max(0.0, _num("CONGRESS_DROP_PCT", 0))
    min_amt = _num("CONGRESS_ALERT_MIN_AMOUNT", 15001)
    state_path = DATA / "congress_alerted.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {"ids": []}
    already = set(state.get("ids", []))
    hits = [t for t in trades if t.get("below_buy") and t.get("price_change") is not None
            and t["price_change"] <= -drop / 100 and (t.get("amount_low") or 0) >= min_amt and t["id"] not in already]
    if not hits:
        print("Congress: no new purchases below the buy price.")
        return
    if not settings:
        print(f"Congress: {len(hits)} purchase(s) below the buy price, but e-mail isn't configured.")
        return
    alerts.send_congress(hits, app_url(), drop, settings)
    print(f"Congress: e-mailed {len(hits)} purchase(s) now below the buy price to {settings['to']}")
    state["ids"] = (state.get("ids", []) + [t["id"] for t in hits])[-5000:]
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    write_json(state_path, state)


if __name__ == "__main__":
    sys.exit(main())
