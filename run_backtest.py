#!/usr/bin/env python3
"""Backtest the conviction score on past C-suite buys (used monthly by the GitHub workflow).

    SEC_CONTACT_EMAIL=you@example.com python3 run_backtest.py --start-year 2019

Writes docs/data/backtest.json, which the app shows on its Backtest tab. The first run downloads
several years of SEC bulk data and prices for thousands of stocks, so it can take 1-2 hours.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from radar.backtest import run_backtest  # noqa: E402
from radar.sec_client import SecClient  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start-year", type=int, default=2019)
    ap.add_argument("--min-value", type=float, default=25_000)
    ap.add_argument("--max-tickers", type=int, default=None, help="limit the number of stocks (faster test runs)")
    ap.add_argument("--out", default=str(HERE / "docs" / "data" / "backtest.json"))
    args = ap.parse_args()
    email = os.environ.get("SEC_CONTACT_EMAIL", "").strip()
    if not email:
        print("Set SEC_CONTACT_EMAIL (the SEC requires a contact e-mail).", file=sys.stderr)
        return 2
    t0, last = time.time(), [0.0]

    def progress(stage: str, done: int, total: int) -> None:
        if time.time() - last[0] > 20 or done == total:
            last[0] = time.time()
            print(f"[{time.time() - t0:6.0f}s] {stage}: {done}/{total}", flush=True)

    res = run_backtest(SecClient(email), start_year=args.start_year, min_value=args.min_value,
                       max_tickers=args.max_tickers, progress=progress)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, separators=(",", ":"), default=str), encoding="utf-8")
    m = res["meta"]
    print(f"Scored {m['trades_scored']} historical buys across {m['tickers_tested']} stocks in {time.time() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
