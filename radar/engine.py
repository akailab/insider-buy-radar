"""Scan recent Form 4 filings, find C-suite open-market buys, enrich them, and score them."""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from . import signals as sig
from .form4 import ROLE_ORDER, ROLE_POINTS, classify_role, parse_form4, purchase_summary, sale_value
from .history_db import HistoryDB, last_published_quarter_guess, quarters_between
from .sec_client import SecClient

Progress = Callable[[str, int, int], None]
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG = {
    "weights": {
        "size": 20,          # dollars invested
        "holdings": 15,      # % increase in their total stake (direct + indirect)
        "mcap": 15,          # trade value vs. company market value
        "history": 10,       # vs. their own past buys
        "routine": 10,       # one-off (informative) vs. routine (same month every year)
        "pay": 10,           # buy size vs. CEO's annual pay (CEOs only)
        "cluster": 8,        # other insiders buying in the same window
        "net": 7,            # other insiders net buying vs. selling over 90 days
        "role": 5,           # CEO > CFO > ...
        "price": 5,          # bought after the stock fell
        "volume": 5,         # size vs. normal daily trading volume
        "timing": 5,         # late in the quarter (closer to the next earnings report)
        "track": 5,          # how their past buys of this stock worked out
    },
    "plan_multiplier": 0.75,      # pre-scheduled 10b5-1 purchases
    "new_exec_multiplier": 0.85,  # insider for less than 6 months
}
LABELS = {
    "size": "Dollars invested", "holdings": "Stake increase", "mcap": "Size vs. market cap",
    "history": "Vs. their past buys", "routine": "One-off, not routine", "pay": "Vs. CEO's annual pay",
    "cluster": "Other insiders buying", "net": "Net insider buying (90d)", "role": "Seniority",
    "price": "Bought after a drop", "volume": "Size vs. trading volume", "timing": "Late in the quarter",
    "track": "Their past buys' record",
}
HISTORY_YEARS = 3
GAP_MAX_FILINGS = 25
NET_MAX_FILINGS = 40


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        user = json.loads((ROOT / "config" / "weights.json").read_text())
        cfg["weights"].update({k: float(v) for k, v in user.get("weights", {}).items() if k in cfg["weights"]})
        for k in ("plan_multiplier", "new_exec_multiplier"):
            if k in user:
                cfg[k] = float(user[k])
    except (OSError, ValueError):
        pass
    return cfg


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _log_scale(x: float, lo: float, hi: float) -> float:
    if not x or x <= 0:
        return 0.0
    return _clip((math.log10(x) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)))


def money(v: float | None) -> str:
    if v is None:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= div:
            return f"${v / div:.1f}{unit}"
    return f"${v:,.0f}"


# --------------------------------------------------------------- fetching
def _key(path: str) -> str:
    """Cache key = accession number, so the same filing reached via different folders is fetched once."""
    return path.rsplit("/", 1)[-1].replace(".txt", "")


def get_filing(client: SecClient, path: str) -> dict | None:
    found, value = client.cache.get_parsed(_key(path))
    if found:
        return value
    txt = client.filing_text(path)
    parsed = parse_form4(txt, path) if txt else None
    if txt is not None:
        client.cache.put_parsed(_key(path), parsed)
    return parsed


def fetch_many(client: SecClient, paths: list[str], progress: Progress, label: str) -> list[dict]:
    out, todo = [], []
    for p in paths:
        found, value = client.cache.get_parsed(_key(p))
        if found:
            if value:
                out.append(value)
        else:
            todo.append(p)
    base = len(paths) - len(todo)
    progress(label, base, len(paths))
    errors = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(get_filing, client, p) for p in todo]
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                f = fut.result()
                if f:
                    out.append(f)
            except Exception:
                errors += 1
                if errors > 50:
                    raise
            if i % 10 == 0 or i == len(todo):
                progress(label, base + i, len(paths))
    return out


def collect_filings(client: SecClient, days: int, include_live: bool, progress: Progress) -> tuple[list[dict], dict]:
    today = date.today()
    start = today - timedelta(days=days - 1)
    paths: set[str] = set()
    covered, missing = [], []
    d = start
    while d <= today:
        progress("Reading EDGAR daily indexes", (d - start).days, days)
        if d.weekday() < 5:
            day_paths = client.daily_form4_paths(d)
            if day_paths is None:
                missing.append(d)
            else:
                paths.update(day_paths)
                covered.append(d.isoformat())
        d += timedelta(days=1)
    recent_missing = [m for m in missing if m >= today - timedelta(days=4)]
    if include_live and recent_missing:
        progress("Reading today's live filings feed", 0, 1)
        paths.update(client.current_form4_paths(datetime.combine(min(recent_missing), dtime.min).astimezone()))
    by_acc: dict[str, str] = {}
    for p in sorted(paths):
        by_acc.setdefault(p.rsplit("/", 1)[-1], p)
    filings = fetch_many(client, sorted(by_acc.values()), progress, "Downloading Form 4 filings")
    return filings, {
        "from": start.isoformat(), "to": today.isoformat(), "days_from_index": covered,
        "days_from_live_feed": [m.isoformat() for m in recent_missing] if include_live else [],
        "filings_checked": len(filings),
    }


# ---------------------------------------------------------------- rows
def _primary_owner(f: dict) -> dict:
    owners = f["owners"] or [{"cik": "", "name": "?", "title": ""}]
    return sorted(owners, key=lambda o: ROLE_ORDER.index(classify_role(o)) if classify_role(o) else 99)[0]


def build_rows(filings: list[dict]) -> tuple[list[dict], dict, dict]:
    """C-suite purchase rows (amendments applied) + per-issuer activity in the scan window."""
    buys, activity = [], {}
    for f in filings:
        o = _primary_owner(f)
        ps = purchase_summary(f)
        act = activity.setdefault(f["issuer_cik"], [])
        if ps:
            act.append({"owner": o["cik"] or o["name"], "code": "P", "value": ps["value"], "filed": f["filed"], "acc": f["accession"]})
            buys.append({**ps, "filing": f, "owner": o, "role": classify_role(o)})
        sv = sale_value(f)
        if sv:
            act.append({"owner": o["cik"] or o["name"], "code": "S", "value": sv, "filed": f["filed"], "acc": f["accession"]})

    # Amendments (4/A) replace the original filing they correct
    originals = [b for b in buys if b["filing"]["doc_type"] == "4"]
    amendments = [b for b in buys if b["filing"]["doc_type"] == "4/A"]
    replaced = set()
    kept_amendments = []
    for a in amendments:
        match = [b for b in originals if b["filing"]["issuer_cik"] == a["filing"]["issuer_cik"]
                 and b["owner"]["cik"] == a["owner"]["cik"] and set(b["trade_dates"]) & set(a["trade_dates"])]
        for m in match:
            replaced.add(m["filing"]["accession"])
        a["amends"] = [m["filing"]["accession"] for m in match]
        kept_amendments.append(a)
    final = [b for b in originals if b["filing"]["accession"] not in replaced] + kept_amendments
    # activity: drop replaced originals
    for k in activity:
        activity[k] = [x for x in activity[k] if x["acc"] not in replaced]

    rows = []
    for b in final:
        if not b["role"]:
            continue
        f, o = b["filing"], b["owner"]
        acc = f["accession"]
        cik_dir = f["path"].split("/")[2]
        window_buyers = {x["owner"] for x in activity.get(f["issuer_cik"], []) if x["code"] == "P"}
        rows.append({
            "id": acc, "accession": acc, "filed": f["filed"], "trade_date": b["first_date"], "trade_date_last": b["last_date"],
            "ticker": f["ticker"], "company": f["issuer_name"], "issuer_cik": f["issuer_cik"],
            "insider": o["name"], "owner_cik": o["cik"], "title": o["title"], "role": b["role"],
            "is_director": o.get("is_director", False),
            "shares": b["shares"], "avg_price": b["avg_price"], "value": b["value"], "n_lots": b["n_lots"],
            "holdings_before": b["holdings_before"], "holdings_after": b["holdings_after"],
            "direct_after": b["direct_after"], "indirect_after": b["indirect_after"], "options_held": b["options_held"],
            "pct_increase": b["pct_increase"], "pct_increase_incl_options": b["pct_increase_incl_options"],
            "new_position": b["new_position"], "plan_10b5_1": bool(f["plan_10b5_1"]),
            "late": b["late"], "filing_lag_bdays": b["filing_lag_bdays"],
            "amended": f["doc_type"] == "4/A", "amends": b.get("amends", []),
            "cluster_others": len(window_buyers - {o["cik"] or o["name"]}),
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik_dir}/{acc.replace('-', '')}/{acc}-index.htm",
        })
    stats = {"insider_buy_filings": len(final), "csuite_buy_filings": len(rows)}
    return rows, activity, stats


# ------------------------------------------------------------ enrichment
class Enricher:
    def __init__(self, client: SecClient, hdb: HistoryDB | None, window_activity: dict, use_prices: bool):
        self.c, self.hdb, self.activity, self.use_prices = client, hdb, window_activity, use_prices
        self.spy = sig.Series(client.yahoo_prices("SPY")) if use_prices else sig.Series(None)
        self._prices: dict[str, sig.Series] = {}
        self._issuer: dict[str, dict] = {}

    def prices(self, ticker: str) -> sig.Series:
        if not self.use_prices or not ticker:
            return sig.Series(None)
        if ticker not in self._prices:
            self._prices[ticker] = sig.Series(self.c.yahoo_prices(ticker))
        return self._prices[ticker]

    def facts(self, cik: str) -> dict:
        found, val = self.c.cache.get_kv(f"facts:{cik}", ttl=3 * 86400)
        if not found:
            val = sig.financial_summary(self.c.companyfacts(cik))
            self.c.cache.put_kv(f"facts:{cik}", val)
        return val or {}

    def issuer(self, cik: str, deep: bool) -> dict:
        """Company-level data, fetched once per company."""
        key = (cik, deep)
        if key in self._issuer:
            return self._issuer[key]
        info = {"facts": self.facts(cik)}
        if deep:
            filings = sig.issuer_filings(self.c.submissions(cik))
            info["filings"] = filings
            periodic = [f for f in filings if f["form"] in ("10-Q", "10-K") and f["doc"]]
            if periodic:
                p = max(periodic, key=lambda f: f["filed"])
                found, flags = self.c.cache.get_kv(f"flags:{p['acc']}", ttl=None)
                if not found:
                    flags = sig.keyword_flags(self.c.document(cik, p["acc"], p["doc"]))
                    self.c.cache.put_kv(f"flags:{p['acc']}", flags)
                info["report"] = {"form": p["form"], "filed": p["filed"], "period": p["report"], "flags": flags,
                                  "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{p['acc'].replace('-', '')}/{p['doc']}"}
            proxies = [f for f in filings if f["form"] == "DEF 14A" and f["doc"]]
            if proxies:
                p = max(proxies, key=lambda f: f["filed"])
                found, pay = self.c.cache.get_kv(f"pay:{p['acc']}", ttl=None)
                if not found:
                    pay = sig.ceo_pay_from_ixbrl(self.c.document(cik, p["acc"], p["doc"]) or "")
                    self.c.cache.put_kv(f"pay:{p['acc']}", pay)
                info["ceo_pay"] = pay
                info["proxy_filed"] = p["filed"]
        self._issuer[key] = info
        return info

    def insider_activity_90d(self, cik: str, filings: list[dict], filed: str, exclude_owner: str) -> dict:
        start = (date.fromisoformat(filed) - timedelta(days=90)).isoformat()
        seen, buys, sells, buyers, sellers = set(), 0.0, 0.0, set(), set()
        entries = list(self.activity.get(cik, []))
        f4 = sorted([f for f in filings if f["form"] in ("4",) and start <= f["filed"] <= filed],
                    key=lambda f: f["filed"], reverse=True)[:NET_MAX_FILINGS]
        for f in f4:
            path = f"edgar/data/{int(cik)}/{f['acc']}.txt"
            p = get_filing(self.c, path)
            if not p:
                continue
            o = _primary_owner(p)
            ps = purchase_summary(p)
            if ps:
                entries.append({"owner": o["cik"] or o["name"], "code": "P", "value": ps["value"], "filed": p["filed"], "acc": p["accession"]})
            sv = sale_value(p)
            if sv:
                entries.append({"owner": o["cik"] or o["name"], "code": "S", "value": sv, "filed": p["filed"], "acc": p["accession"]})
        for e in entries:
            k = (e["acc"], e["code"])
            if k in seen or e["owner"] == exclude_owner or not (start <= e["filed"] <= filed):
                continue
            seen.add(k)
            if e["code"] == "P":
                buys += e["value"]
                buyers.add(e["owner"])
            else:
                sells += e["value"]
                sellers.add(e["owner"])
        return {"buys": buys, "sells": sells, "buyers": len(buyers), "sellers": len(sellers), "checked": len(f4)}

    def insider_trades(self, r: dict, deep: bool) -> tuple[list[dict], str | None]:
        """All earlier open-market trades by this insider in this company (bulk DB + recent filings)."""
        trades, db_end = [], None
        if self.hdb:
            trades = self.hdb.owner_trades(r["owner_cik"], r["issuer_cik"], r["filed"])
            db_end = self.hdb.coverage_end()
        if deep:
            subs = self.c.submissions(r["owner_cik"])
            recent = sig.issuer_filings(subs)
            gap = [f for f in recent if f["form"] == "4" and f["acc"] != r["accession"] and f["filed"] <= r["filed"]
                   and (db_end is None or f["filed"] > db_end)]
            if db_end is None:
                cutoff = (date.fromisoformat(r["filed"]) - timedelta(days=365 * HISTORY_YEARS)).isoformat()
                gap = [f for f in gap if f["filed"] >= cutoff]
            known = {t["acc"] for t in trades}
            for f in sorted(gap, key=lambda f: f["filed"], reverse=True)[:GAP_MAX_FILINGS]:
                if f["acc"] in known:
                    continue
                p = get_filing(self.c, f"edgar/data/{int(r['owner_cik'])}/{f['acc']}.txt")
                if not p or p["issuer_cik"] != r["issuer_cik"] or p["doc_type"] != "4":
                    continue
                ps = purchase_summary(p)
                if ps:
                    trades.append({"acc": p["accession"], "filed": p["filed"], "date": ps["first_date"], "code": "P", "value": ps["value"]})
                sv = sale_value(p)
                if sv:
                    dates = [t["date"] for t in p["txns"] if t["code"] == "S" and t["date"]]
                    trades.append({"acc": p["accession"], "filed": p["filed"], "date": min(dates) if dates else p["filed"], "code": "S", "value": sv})
        return sorted(trades, key=lambda t: t["date"] or ""), db_end

    def enrich(self, r: dict, deep: bool) -> None:
        iss = self.issuer(r["issuer_cik"], deep)
        fin = iss["facts"]
        r["financials"] = {k: v for k, v in fin.items() if k != "shares_outstanding"}
        so = fin.get("shares_outstanding")
        r["shares_outstanding"] = so
        s = self.prices(r["ticker"])
        ps = sig.price_stats(s, r["trade_date"], r["avg_price"])
        r["price_ctx"] = ps
        if so:
            r["mcap"] = so * r["avg_price"]
            if ps and ps.get("since_trade") is not None:
                r["mcap_today"] = r["mcap"] * (1 + ps["since_trade"])
        if ps and ps.get("adv_dollars"):
            r["volume_ratio"] = r["value"] / ps["adv_dollars"]  # in dollars, so splits don't matter

        prior, db_end = self.insider_trades(r, deep)
        r["history"] = sig.history_summary(prior, HISTORY_YEARS, r["trade_date"]) if (prior or db_end or deep) else None
        r["routine"] = sig.routine_class(prior, r["trade_date"]) if db_end else None
        buys_all = [{"date": t["date"], "value": t["value"]} for t in prior if t["code"] == "P"]
        r["track"] = sig.track_record(buys_all, s, self.spy) if buys_all else None

        # Tenure: when did this person first appear as an insider of this company?
        first = self.hdb.first_seen(r["owner_cik"], r["issuer_cik"]) if self.hdb else None
        if first:
            r["insider_since"] = first
            r["tenure_days"] = (date.fromisoformat(r["trade_date"]) - date.fromisoformat(first)).days
        elif (db_end and self.hdb.coverage_start() and self.hdb.coverage_start() < r["trade_date"]
              and self.hdb.issuer_known(r["issuer_cik"], self.hdb.coverage_start())):
            # The company's insiders file regularly, but this person never appears -> joined after the data ends
            r["tenure_days"] = max(0, (date.fromisoformat(r["trade_date"]) - date.fromisoformat(db_end)).days)
            r["insider_since"] = f"after {db_end}"
        r["new_exec"] = r.get("tenure_days") is not None and r["tenure_days"] < 180

        if deep:
            filings = iss.get("filings", [])
            r["timing"] = sig.earnings_timing(filings, r["trade_date"])
            r["report"] = iss.get("report")
            if r["role"] == "CEO" and iss.get("ceo_pay"):
                r["ceo_pay"] = iss["ceo_pay"]
            r["net90"] = self.insider_activity_90d(r["issuer_cik"], filings, r["filed"], r["owner_cik"] or r["insider"])
        r["deep_checked"] = deep


# --------------------------------------------------------------- scoring
def score_row(r: dict, cfg: dict | None = None) -> None:
    cfg = cfg or load_config()
    W = cfg["weights"]
    comps: dict[str, float] = {}
    reasons: list[str] = []
    flags: list[str] = []

    comps["size"] = _log_scale(r["value"], 25_000, 5_000_000)
    reasons.append(f"{r['role']} bought {money(r['value'])} of stock")

    if r.get("new_position"):
        comps["holdings"] = 0.7
        reasons.append("Opened a new position (held no shares before)")
    elif r.get("pct_increase") is not None:
        comps["holdings"] = _clip(r["pct_increase"]) ** 0.6
        pct = r["pct_increase"] * 100
        txt = f"Grew their total stake by {pct:.0f}%" if pct >= 1 else "Grew their total stake by less than 1%"
        if r.get("pct_increase_incl_options") is not None and r.get("options_held"):
            txt += f" ({r['pct_increase_incl_options'] * 100:.0f}% counting options/RSUs)"
        reasons.append(txt)

    if r.get("mcap"):
        bps = r["value"] / r["mcap"] * 1e4
        r["bps_of_mcap"] = bps
        comps["mcap"] = _log_scale(bps, 0.1, 10)
        share = bps / 100
        reasons.append(f"Equal to {share:.{2 if share >= 0.01 else 4}f}% of a {money(r['mcap'])} company")

    h = r.get("history")
    if h is not None:
        buys = h["prior_buys"]
        if h.get("trades_total", 0) == 0 and not buys:
            comps["history"] = 0.8
            reasons.append("No earlier open-market trades by them in this stock on record")
        elif not buys:
            comps["history"] = 1.0 if h["prior_sales_n"] else 0.8
            reasons.append("First open-market buy in 3 yrs" + (" — after previously selling" if h["prior_sales_n"] else ""))
        else:
            biggest = max(b["value"] for b in buys)
            ratio = r["value"] / biggest if biggest else 2
            comps["history"] = _clip(0.75 + 0.35 * math.log2(max(ratio, 1e-6)), 0.1, 1)
            reasons.append(f"Largest of their {len(buys) + 1} buys in 3 yrs ({ratio:.1f}x prior max)" if ratio >= 1
                           else f"Smaller than their past buys ({ratio:.1f}x prior max)")

    if r.get("routine") == "opportunistic":
        comps["routine"] = 1.0
        reasons.append("Opportunistic trader: no fixed yearly pattern (research: these trades are informative)")
    elif r.get("routine") == "routine":
        comps["routine"] = 0.0
        reasons.append("Routine trader: trades in this month every year (research: little signal)")

    if r.get("ceo_pay"):
        ratio = r["value"] / r["ceo_pay"]
        r["pay_ratio"] = ratio
        comps["pay"] = _log_scale(ratio, 0.02, 1.0)
        reasons.append(f"Worth {ratio * 100:.0f}% of the CEO's latest annual pay ({money(r['ceo_pay'])})")

    comps["role"] = ROLE_POINTS.get(r["role"], 0.6)

    n = r.get("cluster_others", 0)
    comps["cluster"] = 0.0 if n == 0 else 0.6 if n == 1 else 1.0
    if n:
        reasons.append(f"{n} other insider{'s' if n > 1 else ''} also bought in this window")

    net = r.get("net90")
    if net is not None:
        if net["buys"] == 0 and net["sells"] == 0:
            comps["net"] = 0.7
        else:
            b = net["buys"] + r["value"]
            comps["net"] = 0.1 + 0.9 * b / (b + net["sells"])
            if net["sells"] > r["value"]:
                reasons.append(f"But other insiders sold {money(net['sells'])} in the last 90 days")
                flags.append("Other insiders selling")
            elif net["buys"] > 0 and net["sells"] == 0:
                reasons.append(f"Other insiders bought {money(net['buys'])} and sold nothing in 90 days")

    pc = r.get("price_ctx")
    if pc and pc.get("below_high") is not None:
        comps["price"] = _clip(pc["below_high"] / 0.5)
        if pc["below_high"] >= 0.15:
            reasons.append(f"Bought {pc['below_high'] * 100:.0f}% below the 52-week high")

    if r.get("volume_ratio") is not None:
        comps["volume"] = _log_scale(r["volume_ratio"], 0.05, 2.0)
        if r["volume_ratio"] >= 0.25:
            reasons.append(f"Size equals {r['volume_ratio']:.1f} days of normal trading volume")

    t = r.get("timing")
    if t and t.get("days_since") is not None:
        d = t["days_since"]
        comps["timing"] = 0.4 if d <= 20 else _clip(0.4 + 0.6 * (d - 20) / 40)
        if d > 45:
            reasons.append(f"Bought {d} days after the last results — late in the quarter")

    tr = r.get("track")
    if tr:
        comps["track"] = _clip((tr["avg_excess_6m"] + 0.10) / 0.30)
        reasons.append(f"Their past buys here beat the S&P 500 by {tr['avg_excess_6m'] * 100:+.0f}% on average over 6 months ({tr['n']} buys)")

    active = {k: v for k, v in comps.items() if W.get(k, 0) > 0}
    total_w = sum(W[k] for k in active)
    score = 100 * sum(W[k] * v for k, v in active.items()) / total_w if total_w else 0
    mults = []
    if r.get("plan_10b5_1"):
        score *= cfg["plan_multiplier"]
        mults.append(f"10b5-1 plan ×{cfg['plan_multiplier']}")
        reasons.append("Made under a pre-set 10b5-1 plan (score reduced)")
        flags.append("10b5-1 plan")
    if r.get("new_exec"):
        score *= cfg["new_exec_multiplier"]
        mults.append(f"New executive ×{cfg['new_exec_multiplier']}")
        reasons.append("Insider for under 6 months — new-hire buys are partly for show (score reduced)")
        flags.append("New executive")
    if r.get("late"):
        flags.append(f"Filed late ({r['filing_lag_bdays']} business days)")
    if r.get("amended"):
        flags.append("Amended filing")
    for f in (r.get("report") or {}).get("flags", []):
        flags.append(f"{f} (latest {r['report']['form']})")

    r["score"] = round(score, 1)
    r["components"] = {k: round(v * W[k], 1) for k, v in active.items()}
    r["components_max"] = {k: W[k] for k in active}
    r["missing"] = [k for k in W if k not in active and W[k] > 0]
    r["multipliers"] = mults
    r["signal"] = "Very strong" if score >= 70 else "Strong" if score >= 50 else "Notable" if score >= 30 else "Minor"
    r["reasons"] = reasons
    r["flags"] = flags


# ------------------------------------------------------------- pipeline
def history_quarters(years: int = 4) -> list[str]:
    last = last_published_quarter_guess(date.today())
    y, q = int(last[:4]), int(last[5])
    start = date(y - years, (q - 1) * 3 + 1, 1)
    return quarters_between(start, date(y, (q - 1) * 3 + 1, 1))


def run_scan(
    client: SecClient,
    days: int = 7,
    include_live: bool = True,
    deep_min: float = 100_000,
    use_prices: bool = True,
    use_history_db: bool = True,
    progress: Progress = lambda *a: None,
) -> dict:
    cfg = load_config()
    hdb, missing_q = None, []
    if use_history_db:
        hdb = HistoryDB(client.cache_dir / "history.sqlite3")
        missing_q = hdb.ensure(client, history_quarters(), progress)
    filings, meta = collect_filings(client, days, include_live, progress)
    rows, activity, stats = build_rows(filings)

    en = Enricher(client, hdb, activity, use_prices)
    label = "Checking company data, prices & insider history"
    # Tiny buys (< $10K) are scored on the filing alone - not worth extra downloads
    ordered = sorted((r for r in rows if r["value"] >= 10_000), key=lambda r: r["value"], reverse=True)
    progress(label, 0, len(ordered))
    errors = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(en.enrich, r, r["value"] >= deep_min and bool(r["owner_cik"])): r for r in ordered}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
            except Exception as e:  # keep going; the row is scored on what we have
                errors.append(f"{futs[fut]['ticker']}: {e}")
            progress(label, i, len(ordered))
    for r in rows:
        for k, v in {"deep_checked": False, "history": None, "mcap": None, "price_ctx": None, "routine": None, "track": None,
                     "timing": None, "net90": None, "new_exec": False, "financials": {}, "report": None}.items():
            r.setdefault(k, v)
        score_row(r, cfg)
    rows.sort(key=lambda r: r["score"], reverse=True)
    progress("Done", 1, 1)
    return {
        "meta": {**meta, **stats, "deep_min": deep_min, "scanned_at": datetime.now().isoformat(timespec="seconds"),
                 "history_db_through": hdb.coverage_end() if hdb else None,
                 "history_db_missing_quarters": missing_q, "enrich_errors": errors[:20],
                 "weights": cfg["weights"], "labels": LABELS},
        "rows": rows,
    }
