"""Backtest: do higher-scored C-suite buys actually beat the market afterwards?

Uses the SEC's quarterly bulk data sets (every Form 4 since 2006) plus Yahoo Finance prices.
Each historical buy is scored with the same formula as the live app (minus the parts that need
extra per-company documents: CEO pay, earnings timing, 10-Q flags), then we measure the stock's
return vs. the S&P 500 (SPY) starting the trading day AFTER the filing became public.
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable

from . import signals as sig
from .engine import LABELS, load_config, score_row
from .history_db import HistoryDB, last_published_quarter_guess, quarters_between
from .sec_client import SecClient

Progress = Callable[[str, int, int], None]
HORIZONS = {"3m": 63, "6m": 126, "12m": 252}


def _shares_outstanding_history(client: SecClient, cik: str) -> list[tuple[str, float]]:
    """[(date, shares outstanding)] from the cover pages of the company's filings."""
    data = client.company_concept(cik, "dei", "EntityCommonStockSharesOutstanding")
    per_filing: dict[tuple[str, str], float] = defaultdict(float)
    for u in (data or {}).get("units", {}).get("shares", []):
        if u.get("end") and u.get("val"):
            per_filing[(u["end"], u.get("accn", ""))] += float(u["val"])  # sum share classes within a filing
    best: dict[str, float] = {}
    for (end, _), v in per_filing.items():
        best[end] = max(best.get(end, 0), v)
    return sorted(best.items())


def _as_of(points: list[tuple[str, float]], day: str) -> float | None:
    val = None
    for d, v in points:
        if d <= day:
            val = v
        else:
            break
    return val


def _summ(xs: list[float]) -> dict:
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    return {"n": len(xs), "avg": st.mean(xs), "median": st.median(xs), "hit": sum(1 for x in xs if x > 0) / len(xs)}


def run_backtest(client: SecClient, start_year: int = 2019, min_value: float = 25_000, max_tickers: int | None = None,
                 progress: Progress = lambda *a: None) -> dict:
    cfg = load_config()
    today = date.today()
    hdb = HistoryDB(client.cache_dir / "history.sqlite3")
    last_q = last_published_quarter_guess(today)
    quarters = quarters_between(date(start_year - 3, 1, 1), date(int(last_q[:4]), (int(last_q[5]) - 1) * 3 + 1, 1))
    missing = hdb.ensure(client, quarters, progress)

    buys = hdb.csuite_buys(f"{start_year}-01-01", today.isoformat(), min_value)
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for b in buys:
        if b["ticker"] and b["ticker"] not in ("NONE", "N/A", "NA"):
            by_ticker[b["ticker"]].append(b)
    tickers = sorted(by_ticker, key=lambda t: -len(by_ticker[t]))
    if max_tickers:
        tickers = tickers[:max_tickers]

    spy = sig.Series(client.yahoo_prices("SPY", start=date(start_year - 2, 1, 1)))
    if not spy:
        raise RuntimeError("Couldn't download S&P 500 (SPY) prices from Yahoo Finance.")

    results, no_price = [], 0
    so_cache: dict[str, list] = {}
    for ti, t in enumerate(tickers):
        progress("Backtest: scoring historical buys & measuring returns", ti, len(tickers))
        s = sig.Series(client.yahoo_prices(t, start=date(start_year - 2, 1, 1)))
        if not s:
            no_price += len(by_ticker[t])
            continue
        for b in by_ticker[t]:
            r = _historical_row(b, hdb, s, spy, client, so_cache)
            if r is None:
                continue
            score_row(r, cfg)
            for h, n in HORIZONS.items():
                ret, ex = sig.forward_excess(s, spy, b["filed"], n)
                r[f"ret_{h}"], r[f"ex_{h}"] = ret, ex
            results.append(r)
    progress("Backtest: scoring historical buys & measuring returns", len(tickers), len(tickers))
    return summarise(results, cfg, {
        "start_year": start_year, "min_value": min_value, "buys_found": len(buys), "tickers_tested": len(tickers),
        "dropped_no_prices": no_price, "missing_quarters": missing, "run_at": datetime.now().isoformat(timespec="seconds"),
        "data_through": hdb.coverage_end(),
    })


def _historical_row(b: dict, hdb: HistoryDB, s: sig.Series, spy: sig.Series, client: SecClient, so_cache: dict) -> dict | None:
    if not b["shares"] or not b["value"]:
        return None
    avg = b["value"] / b["shares"]
    prior = hdb.owner_trades(b["owner_cik"], b["issuer_cik"], b["filed"])
    before = (b["after"] - b["shares"]) if b["after"] else None
    r = {
        "role": b["role"], "value": b["value"], "shares": b["shares"], "avg_price": avg, "trade_date": b["first_date"],
        "filed": b["filed"], "ticker": b["ticker"], "plan_10b5_1": bool(b["plan"]),
        "new_position": before == 0 if before is not None else False,
        "pct_increase": (b["shares"] / before) if before and before > 0 else None,
    }
    r["history"] = sig.history_summary(prior, 3, b["first_date"])
    r["routine"] = sig.routine_class(prior, b["first_date"])
    buys_before = [{"date": t["date"], "value": t["value"]} for t in prior if t["code"] == "P"]
    # Track record only from buys whose 6-month result was already known at the time
    cutoff = (date.fromisoformat(b["filed"]) - timedelta(days=190)).isoformat()
    tr = sig.track_record([x for x in buys_before if x["date"] <= cutoff], s, spy)
    r["track"] = tr
    start90 = (date.fromisoformat(b["filed"]) - timedelta(days=90)).isoformat()
    start14 = (date.fromisoformat(b["filed"]) - timedelta(days=14)).isoformat()
    act = [a for a in hdb.issuer_trades(b["issuer_cik"], start90, b["filed"]) if a["owner_cik"] != b["owner_cik"]]
    r["cluster_others"] = len({a["owner_cik"] for a in act if a["code"] == "P" and a["filed"] >= start14})
    r["net90"] = {"buys": sum(a["value"] for a in act if a["code"] == "P"), "sells": sum(a["value"] for a in act if a["code"] == "S")}
    first = hdb.first_seen(b["owner_cik"], b["issuer_cik"])
    r["new_exec"] = bool(first and (date.fromisoformat(b["first_date"]) - date.fromisoformat(first)).days < 180
                         and first > (hdb.coverage_start() or "9999"))
    ps = sig.price_stats(s, b["first_date"], avg)
    r["price_ctx"] = ps
    if ps and ps.get("adv_dollars"):
        r["volume_ratio"] = b["value"] / ps["adv_dollars"]
    if b["issuer_cik"] not in so_cache:
        so_cache[b["issuer_cik"]] = _shares_outstanding_history(client, b["issuer_cik"])
    so = _as_of(so_cache[b["issuer_cik"]], b["first_date"])
    if so:
        r["mcap"] = so * avg
    return r


def summarise(rows: list[dict], cfg: dict, meta: dict) -> dict:
    out = {"meta": {**meta, "trades_scored": len(rows), "weights": cfg["weights"], "labels": LABELS,
                    "not_tested": ["pay", "timing"], "horizons": list(HORIZONS)}}
    out["overall"] = {h: _summ([r[f"ex_{h}"] for r in rows]) for h in HORIZONS}
    buckets = {}
    for label in ("Very strong", "Strong", "Notable", "Minor"):
        rs = [r for r in rows if r["signal"] == label]
        buckets[label] = {h: _summ([r[f"ex_{h}"] for r in rs]) for h in HORIZONS}
    out["by_signal"] = buckets
    # Score quintiles (6-month)
    ranked = sorted([r for r in rows if r["ex_6m"] is not None], key=lambda r: r["score"])
    quint = []
    for i in range(5):
        chunk = ranked[i * len(ranked) // 5:(i + 1) * len(ranked) // 5]
        if chunk:
            quint.append({"q": i + 1, "score_from": chunk[0]["score"], "score_to": chunk[-1]["score"], **_summ([r["ex_6m"] for r in chunk])})
    out["by_quintile_6m"] = quint
    # Which factors mattered: top-third vs bottom-third 6-month excess return
    factors, spreads = {}, {}
    for k, w in cfg["weights"].items():
        pts = [(r["components"][k] / r["components_max"][k], r["ex_6m"]) for r in rows
               if k in r["components"] and r["ex_6m"] is not None and r["components_max"].get(k)]
        if len(pts) < 30:
            factors[k] = {"n": len(pts), "spread": None}
            continue
        pts.sort(key=lambda p: p[0])
        third = len(pts) // 3
        lo, hi = [p[1] for p in pts[:third]], [p[1] for p in pts[-third:]]
        spread = st.mean(hi) - st.mean(lo)
        factors[k] = {"n": len(pts), "top_third": st.mean(hi), "bottom_third": st.mean(lo), "spread": spread}
        spreads[k] = spread
    out["factors"] = factors
    # Suggested weights: proportional to each factor's positive spread; untested factors keep their weight
    pos = {k: max(0.0, v) for k, v in spreads.items()}
    tested_total = sum(cfg["weights"][k] for k in spreads)
    if sum(pos.values()) > 0:
        sugg = {k: round(tested_total * v / sum(pos.values()), 1) for k, v in pos.items()}
        for k, w in cfg["weights"].items():
            sugg.setdefault(k, w)
        out["suggested_weights"] = sugg
    out["sample"] = [
        {k: r.get(k) for k in ("filed", "ticker", "role", "value", "score", "signal", "ex_3m", "ex_6m", "ex_12m")}
        for r in sorted(rows, key=lambda r: r["filed"], reverse=True)[:300]
    ]
    return out
