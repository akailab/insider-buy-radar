"""Pure helper functions that turn raw data into signals (no network access here)."""
from __future__ import annotations

import html as htmllib
import re
from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from statistics import mean


# ---------------------------------------------------------------- history
def routine_class(prior: list[dict], trade_date: str) -> str | None:
    """Cohen, Malloy & Pomorski (2012): an insider who traded in the same calendar month in each
    of the previous three years is 'routine'; one who traded in each of those years but not in that
    pattern is 'opportunistic'. Returns None when there isn't three years of history."""
    try:
        td = date.fromisoformat(trade_date)
    except (TypeError, ValueError):
        return None
    years = [td.year - 1, td.year - 2, td.year - 3]
    by_year: dict[int, set[int]] = {y: set() for y in years}
    for t in prior:
        try:
            d = date.fromisoformat(t["date"])
        except (TypeError, ValueError, KeyError):
            continue
        if d.year in by_year:
            by_year[d.year].add(d.month)
    if not all(by_year[y] for y in years):
        return None
    return "routine" if all(td.month in by_year[y] for y in years) else "opportunistic"


def history_summary(prior: list[dict], window_years: int, trade_date: str) -> dict:
    try:
        cutoff = (date.fromisoformat(trade_date) - timedelta(days=365 * window_years)).isoformat()
    except (TypeError, ValueError):
        cutoff = "0000"
    recent = [t for t in prior if (t.get("date") or "") >= cutoff]
    buys = [{"date": t["date"], "value": t["value"]} for t in recent if t["code"] == "P"]
    sells = [t for t in recent if t["code"] == "S"]
    return {
        "prior_buys": buys,
        "prior_sales_n": len(sells),
        "prior_sales_value": sum(t["value"] or 0 for t in sells),
        "trades_total": len(prior),
    }


# ----------------------------------------------------------------- prices
class Series:
    """Daily price/volume series with date lookups."""

    def __init__(self, data: dict | None):
        self.d = data["d"] if data else []
        self.c = data["c"] if data else []
        self.v = data["v"] if data else []
        self.a = (data.get("a") or data["c"]) if data else []  # dividend-adjusted closes for returns
        if len(self.a) != len(self.c) or any(not x or x <= 0 for x in self.a):
            self.a = self.c  # bad/zero adjusted prices (seen on some penny stocks): fall back to split-adjusted closes
        self.last = data.get("last") if data else None

    def __bool__(self) -> bool:
        return len(self.d) > 20

    def idx_on_or_before(self, day: str) -> int | None:
        i = bisect_right(self.d, day) - 1
        return i if i >= 0 else None

    def idx_after(self, day: str) -> int | None:
        i = bisect_right(self.d, day)
        return i if i < len(self.d) else None

    def idx_on_or_after(self, day: str) -> int | None:
        i = bisect_left(self.d, day)
        return i if i < len(self.d) else None


def price_stats(s: Series, trade_date: str, trade_price: float) -> dict | None:
    """Price context around the trade. Uses the series' own close on the trade date for relative
    measures, so stock splits after the trade don't distort anything."""
    if not s:
        return None
    i = s.idx_on_or_before(trade_date)
    if i is None or i < 20:
        return None
    close = s.c[i]
    y_ago = (date.fromisoformat(trade_date) - timedelta(days=365)).isoformat()
    j = s.idx_on_or_after(y_ago) or 0
    window = s.c[j:i + 1]
    hi, lo = max(window), min(window)
    q_ago = s.idx_on_or_before((date.fromisoformat(trade_date) - timedelta(days=91)).isoformat())
    vols = [v for v in s.v[max(0, i - 20):i] if v]
    adv = mean(vols) if vols else None
    last = s.last or s.c[-1]
    return {
        "close_on_trade": close,
        "high_52w": hi,
        "low_52w": lo,
        "below_high": max(0.0, min(1.0, 1 - close / hi)) if hi else None,
        "return_3m_before": (close / s.c[q_ago] - 1) if q_ago is not None and s.c[q_ago] else None,
        "since_trade": (s.a[-1] / s.a[i] - 1) if s.a[i] else None,
        "last_price": last,
        "adv_shares": adv,
        "adv_dollars": adv * close if adv else None,
    }


def forward_excess(s: Series, spy: Series, filed: str, days: int) -> tuple[float | None, float | None]:
    """Return and return-vs-SPY from the first close AFTER the filing date to `days` trading days later."""
    i = s.idx_after(filed)
    if i is None or i + days >= len(s.c):
        return None, None
    p0, p1 = s.a[i], s.a[i + days]
    if not p0 or not p1 or p0 <= 0 or p1 <= 0 or not 0.002 < p1 / p0 < 50:
        return None, None  # missing or obviously broken price data - leave this one out
    r = p1 / p0 - 1
    j = spy.idx_on_or_after(s.d[i])
    k = spy.idx_on_or_after(s.d[i + days])
    if j is None or k is None or not spy.a[j] or not spy.a[k]:
        return r, None
    return r, r - (spy.a[k] / spy.a[j] - 1)


def track_record(prior_buys: list[dict], s: Series, spy: Series, days: int = 126) -> dict | None:
    """Average 6-month return vs. the S&P 500 after this insider's earlier buys of this stock."""
    if not s or not spy:
        return None
    ex = []
    for b in prior_buys:
        _, e = forward_excess(s, spy, b["date"], days)
        if e is not None:
            ex.append(e)
    if not ex:
        return None
    return {"n": len(ex), "avg_excess_6m": mean(ex), "hit_rate": sum(1 for e in ex if e > 0) / len(ex)}


# ------------------------------------------------------------- financials
REVENUE_TAGS = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense"]
_QFRAME = re.compile(r"^CY(\d{4})Q([1-4])$")
_IFRAME = re.compile(r"^CY(\d{4})Q([1-4])I$")


def _quarterly(facts: dict, tag: str) -> dict[str, float]:
    units = facts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])
    return {u["frame"]: u["val"] for u in units if _QFRAME.match(u.get("frame", ""))}


def _instant(facts: dict, tags: list[str]) -> tuple[float | None, str | None]:
    best = (None, None)
    for tag in tags:
        units = facts.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {}).get("USD", [])
        pts = [(u["end"], u["val"]) for u in units if _IFRAME.match(u.get("frame", "")) and "end" in u]
        if pts:
            end, val = max(pts)
            if best[1] is None or end > best[1]:
                best = (val, end)
    return best


def _prev_year(frame: str) -> str:
    m = _QFRAME.match(frame)
    return f"CY{int(m.group(1)) - 1}Q{m.group(2)}" if m else ""


def financial_summary(facts: dict | None) -> dict:
    if not facts:
        return {}
    out: dict = {}
    dei = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if dei:
        latest_filed = max(u.get("filed", "") for u in dei)
        latest = [u for u in dei if u.get("filed") == latest_filed]
        latest_end = max(u.get("end", "") for u in latest)
        out["shares_outstanding"] = sum(float(u["val"]) for u in latest if u.get("end") == latest_end) or None
        out["shares_as_of"] = latest_end
    # Revenue: pick the tag with the most recent quarter
    rev, rev_tag = {}, None
    for tag in REVENUE_TAGS:
        q = _quarterly(facts, tag)
        if q and (not rev or max(q) > max(rev)):
            rev, rev_tag = q, tag
    if rev:
        last = max(rev, key=lambda f: (int(f[2:6]), int(f[7])))
        out["revenue_q"] = rev[last]
        out["revenue_quarter"] = f"{last[2:6]} Q{last[7]}"
        prev = rev.get(_prev_year(last))
        if prev:
            out["revenue_yoy"] = rev[last] / prev - 1 if prev > 0 else None
    ni = _quarterly(facts, "NetIncomeLoss")
    if ni:
        frames = sorted(ni, key=lambda f: (int(f[2:6]), int(f[7])))[-4:]
        if len(frames) == 4:
            out["net_income_ttm"] = sum(ni[f] for f in frames)
        out["net_income_q"] = ni[frames[-1]]
    cash, cash_end = _instant(facts, ["CashAndCashEquivalentsAtCarryingValue",
                                      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"])
    if cash is not None:
        out["cash"], out["cash_as_of"] = cash, cash_end
    debt_nc, _ = _instant(facts, ["LongTermDebtNoncurrent", "LongTermDebt"])
    debt_c, _ = _instant(facts, ["LongTermDebtCurrent"])
    if debt_nc is not None or debt_c is not None:
        out["debt"] = (debt_nc or 0) + (debt_c or 0)
    return out


# --------------------------------------------------------- filings / text
def ceo_pay_from_ixbrl(doc: str) -> float | None:
    """Total CEO ('PEO') pay from the pay-versus-performance table tagged in a proxy (DEF 14A).
    Tables list the most recent year first, so the first tagged value is used."""
    for m in re.finditer(r"<ix:nonFraction\b([^>]*)>(.*?)</ix:nonFraction>", doc, re.S | re.I):
        attrs, inner = m.group(1), m.group(2)
        if not re.search(r'name="ecd:PeoTotalCompAmt"', attrs, re.I):
            continue
        text = re.sub(r"<[^>]+>", "", inner).replace(",", "").replace("$", "").strip()
        try:
            val = float(text)
        except ValueError:
            continue
        sm = re.search(r'scale="(-?\d+)"', attrs)
        if sm:
            val *= 10 ** int(sm.group(1))
        if re.search(r'sign="-"', attrs):
            val = -val
        if val > 0:
            return val
    return None


FLAG_PATTERNS = [
    ("Going-concern doubt", re.compile(r"substantial doubt[^.]{0,250}going concern|going concern[^.]{0,250}substantial doubt", re.I)),
    ("Material weakness in controls", re.compile(r"material weakness", re.I)),
    ("Restructuring", re.compile(r"\brestructuring (plan|charges?|costs?)", re.I)),
    ("Impairment charge", re.compile(r"\bimpairment (charge|loss)", re.I)),
    ("Debt covenant issue", re.compile(r"(waiver|breach|violation|not in compliance)[^.]{0,120}covenant|covenant[^.]{0,120}(waiver|breach|violation)", re.I)),
    ("Exchange listing deficiency", re.compile(r"(deficiency|delisting) (letter|notice|notification)|minimum bid price requirement", re.I)),
]


def filing_text(doc: str) -> str:
    doc = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?s)<[^>]+>", " ", doc)
    return re.sub(r"\s+", " ", htmllib.unescape(doc))


def keyword_flags(doc: str | None) -> list[str]:
    if not doc:
        return []
    text = filing_text(doc)
    return [label for label, pat in FLAG_PATTERNS if pat.search(text)]


def issuer_filings(subs: dict | None) -> list[dict]:
    if not subs:
        return []
    r = subs.get("filings", {}).get("recent", {})
    keys = ["accessionNumber", "filingDate", "reportDate", "form", "items", "primaryDocument"]
    cols = [r.get(k, []) for k in keys]
    n = min((len(c) for c in cols if c), default=0)
    return [
        {"acc": cols[0][i], "filed": cols[1][i], "report": cols[2][i] if cols[2] else "",
         "form": cols[3][i], "items": (cols[4][i] if cols[4] else "") or "", "doc": cols[5][i] if cols[5] else ""}
        for i in range(n)
    ]


def earnings_timing(filings: list[dict], trade_date: str) -> dict | None:
    """Days since the last results announcement (8-K item 2.02, 10-Q or 10-K) before the trade."""
    events = [f for f in filings if f["filed"] <= trade_date and (
        f["form"] in ("10-Q", "10-K", "10-Q/A", "10-K/A", "20-F", "40-F") or (f["form"] == "8-K" and "2.02" in f["items"]))]
    if not events:
        return None
    last = max(events, key=lambda f: f["filed"])
    periodic = [f for f in filings if f["form"] in ("10-Q", "10-K") and f["filed"] <= trade_date]
    after = sorted((f for f in filings if f["filed"] > trade_date and (
        f["form"] in ("10-Q", "10-K") or (f["form"] == "8-K" and "2.02" in f["items"]))), key=lambda f: f["filed"])
    td = date.fromisoformat(trade_date)
    out = {
        "last_event": last["form"] + (" (earnings)" if last["form"] == "8-K" else ""),
        "last_event_date": last["filed"],
        "days_since": (td - date.fromisoformat(last["filed"])).days,
    }
    if periodic:
        lp = max(periodic, key=lambda f: f["filed"])
        nxt = date.fromisoformat(lp["filed"]) + timedelta(days=91)
        out["next_expected"] = nxt.isoformat()
        out["days_to_next"] = (nxt - td).days
    if after:
        out["next_actual"] = after[0]["filed"]
        out["next_actual_form"] = after[0]["form"]
    return out
