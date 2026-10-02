"""Stock trades by members of Congress (STOCK Act Periodic Transaction Reports).

Sources (all official and free):
  House  - Clerk's yearly index of disclosures (FD.zip -> XML) + each PTR's PDF.
  Senate - Electronic Financial Disclosures (efdsearch.senate.gov) search + each PTR's page.
  Members, parties, leadership and committees - the public-domain congress-legislators data set.
  Company names and industries - SEC ticker list + company submissions.

This is a data feed, not a ranking: every stock or stock-option trade with a ticker is kept, tagged
with context (leadership, committee oversight overlap, C-suite buying in the same stock).
"""
from __future__ import annotations

import io
from bisect import bisect_right
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Callable

from .sec_client import Cache, RateLimiter, SecClient, _ssl_context

Progress = Callable[[str, int, int], None]

HOUSE_ZIP = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
HOUSE_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
SENATE = "https://efdsearch.senate.gov"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators/"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
LATE_DAYS = 45  # STOCK Act: report no later than 45 days after the trade


class CongressError(RuntimeError):
    pass


# ------------------------------------------------------------------ http
class Http:
    """Small polite HTTP helper with an optional cookie jar (the Senate site needs one)."""

    def __init__(self, contact_email: str, per_second: float = 2.0, cookies: bool = False):
        self.ua = f"Insider Radar Research {contact_email}"
        self.limiter = RateLimiter(per_second)
        handlers: list = [urllib.request.HTTPSHandler(context=_ssl_context())]
        self.jar = CookieJar() if cookies else None
        if self.jar is not None:
            handlers.append(urllib.request.HTTPCookieProcessor(self.jar))
        self.opener = urllib.request.build_opener(*handlers)

    def request(self, url: str, data: dict | None = None, headers: dict | None = None, attempts: int = 3) -> bytes | None:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last = None
        for i in range(attempts):
            self.limiter.wait()
            req = urllib.request.Request(url, data=body, headers={"User-Agent": self.ua, **(headers or {})})
            try:
                with self.opener.open(req, timeout=60) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                last = e
                if e.code in (401, 403) and i >= 1:
                    break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
            time.sleep(2 * 2**i)
        raise CongressError(f"{urllib.parse.urlsplit(url).netloc}: {last}")

    def cookie(self, name: str) -> str | None:
        for c in self.jar or []:
            if c.name == name:
                return c.value
        return None


def _iso(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(d[:10], fmt).date().isoformat()
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------- amounts
_AMT = re.compile(r"\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)|over\s*\$\s*([\d,]+)|\$\s*([\d,]+)\s*\+", re.I)


def parse_amount(text: str) -> tuple[int | None, int | None, str]:
    m = _AMT.search(text or "")
    if not m:
        return None, None, (text or "").strip()
    n = lambda s: int(s.replace(",", ""))  # noqa: E731
    if m.group(1):
        lo, hi = n(m.group(1)), n(m.group(2))
        return lo, hi, f"${lo:,} – ${hi:,}"
    v = n(m.group(3) or m.group(4))
    return v + 1, None, f"Over ${v:,}"


# ------------------------------------------------------------------ House
OWNERS = {"SP": "Spouse", "DC": "Dependent child", "JT": "Joint", "": "Self"}
TYPES = {"P": "Buy", "S": "Sell", "S (partial)": "Sell (partial)", "E": "Exchange"}
ASSET_CODES = {"ST": "Stock", "OP": "Stock option"}  # the codes kept (others: funds, bonds, crypto...)

_TX = re.compile(
    r"(?:(?<![A-Za-z])|(?<=New)|(?<=Amended))(S\s*\(partial\)|P|S|E)\s*(\d{1,2}/\d{1,2}/\d{4})\s*(\d{1,2}/\d{1,2}/\d{4})\s*"
    r"(\$\s*[\d,]+\s*-\s*\$\s*[\d,]+|Over\s*\$\s*[\d,]+|\$\s*[\d,]+\s*\+)", re.I)
_MARKER = re.compile(r"\[([A-Z]{2})\]")
_TICKER_END = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*$")
_NOISE = re.compile(
    r"F\s*S\s*:|S\s*O\s*:|^\s*D\s*:|Filing\s+Status|Subholding\s+Of|Description|Comments?\s*:|Cap\.?\s*Gains|"
    r"Notification\s+Date|Transaction\s+Type|^\s*ID\b|Asset\s+Class|Location\s*:|\$200\?", re.I)


def parse_house_ptr_text(text: str) -> list[dict]:
    """Parse the text of an electronically filed House PTR into transactions."""
    text = (text or "").replace("\x00", "")
    out, prev_end = [], 0
    for m in _TX.finditer(text):
        seg = text[prev_end:m.start()]
        prev_end = m.end()
        markers = list(_MARKER.finditer(seg))
        if not markers:
            continue
        mk = markers[-1]
        code = mk.group(1)
        before = seg[:mk.start()]
        tm = _TICKER_END.search(before)
        ticker = tm.group(1) if tm else ""
        name_part = before[:tm.start()] if tm else before
        # asset name: walk back over at most 3 lines, stopping at noise/labels
        lines = [ln.strip() for ln in name_part.split("\n")]
        picked: list[str] = []
        for ln in reversed(lines):
            if not ln:
                if picked:
                    break
                continue
            if _NOISE.search(ln) or _MARKER.search(ln) or len(picked) >= 3:
                cut = _NOISE.split(ln)[-1].strip() if _NOISE.search(ln) and not _MARKER.search(ln) else ""
                if cut and not picked:
                    picked.append(cut)
                break
            picked.append(ln)
        asset = " ".join(reversed(picked)).strip(" -")
        owner = ""
        om = re.match(r"^(SP|DC|JT)(?!DR)(?=\s|[A-Z])\s*", asset)  # owner code, but not "SPDR ..." funds
        if om:
            owner, asset = om.group(1), asset[om.end():]
        asset = re.sub(r"^\d{1,3}\s+", "", asset)  # a leading row number, if printed
        tx_type = re.sub(r"\s+", " ", m.group(1)).strip()
        tx_type = "S (partial)" if tx_type.upper().startswith("S (") else tx_type.upper()
        lo, hi, amt = parse_amount(m.group(4))
        out.append({
            "owner": OWNERS.get(owner, "Self"), "asset": asset[:140], "ticker": ticker, "asset_code": code,
            "type": TYPES.get(tx_type, tx_type), "trade_date": _iso(m.group(2)), "notified": _iso(m.group(3)),
            "amount_low": lo, "amount_high": hi, "amount": amt,
        })
    return out


def pdf_text(blob: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:
        raise CongressError("House filings are PDFs; install the free 'pypdf' package to read them "
                            "(the start scripts and the GitHub workflow do this for you).") from e
    reader = PdfReader(io.BytesIO(blob))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_house_index(blob: bytes) -> list[dict]:
    zf = zipfile.ZipFile(io.BytesIO(blob))
    xml_name = next((n for n in zf.namelist() if n.lower().endswith(".xml")), None)
    if not xml_name:
        return []
    root = ET.fromstring(zf.read(xml_name))
    out = []
    for m in root.iter("Member"):
        g = lambda k: (m.findtext(k) or "").strip()  # noqa: E731
        if g("FilingType").upper() != "P":
            continue
        out.append({"doc": g("DocID"), "year": g("Year"), "filed": _iso(g("FilingDate")), "last": g("Last"),
                    "first": g("First"), "prefix": g("Prefix"), "suffix": g("Suffix"), "state_dst": g("StateDst")})
    return out


# ----------------------------------------------------------------- Senate
class _Table(HTMLParser):
    """Collects rows of cell text from every <table> on a page."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


SENATE_COLS = {"transaction date": "date", "owner": "owner", "ticker": "ticker", "asset name": "asset",
               "asset type": "asset_type", "type": "type", "amount": "amount", "comment": "comment"}


def parse_senate_ptr_html(html: str) -> list[dict]:
    p = _Table()
    p.feed(html)
    header, out = None, []
    for row in p.rows:
        low = [c.lower() for c in row]
        if "ticker" in low and any("amount" in c for c in low):
            header = {i: SENATE_COLS[c] for i, c in enumerate(low) if c in SENATE_COLS}
            continue
        if header is None or len(row) < 8:
            continue
        rec = {v: row[i] for i, v in header.items() if i < len(row)}
        ticker = (rec.get("ticker") or "").strip().upper()
        ticker = "" if ticker in ("--", "N/A", "") else re.sub(r"[^A-Z0-9.\-]", "", ticker)
        typ = (rec.get("type") or "").lower()
        lo, hi, amt = parse_amount(rec.get("amount", ""))
        out.append({
            "owner": (rec.get("owner") or "Self").title().replace("Child", "Dependent child"),
            "asset": rec.get("asset", "")[:140], "ticker": ticker, "asset_type_raw": rec.get("asset_type", ""),
            "type": "Buy" if typ.startswith("purchase") else "Sell (partial)" if "partial" in typ
            else "Sell" if typ.startswith("sale") else "Exchange" if "exchange" in typ else rec.get("type", ""),
            "trade_date": _iso(rec.get("date")), "amount_low": lo, "amount_high": hi, "amount": amt,
            "comment": "" if (rec.get("comment") or "--") == "--" else rec.get("comment", ""),
        })
    return out


# --------------------------------------------------------------- members
COMMITTEE_GROUPS = [  # committee name keywords -> industries it oversees (a deliberately narrow, rough map)
    (r"Energy and Commerce", ["health", "energy", "tech"]),
    (r"Armed Services", ["defense"]),
    (r"Intelligence", ["defense", "tech"]),
    (r"Homeland Security", ["defense", "tech"]),
    (r"Financial Services|Banking", ["finance"]),
    (r"Energy and Natural Resources|Natural Resources|Environment and Public Works", ["energy"]),
    (r"Health, Education, Labor", ["health"]),
    (r"Ways and Means|\bFinance\b", ["health"]),
    (r"Commerce, Science, and Transportation", ["tech", "transport", "aerospace"]),
    (r"Transportation and Infrastructure", ["transport", "construction"]),
    (r"Science, Space, and Technology", ["aerospace", "tech"]),
    (r"Agriculture", ["agriculture"]),
]
SIC_GROUPS = {  # SIC code ranges per industry group
    "defense": [(3480, 3489), (3720, 3729), (3760, 3769), (3795, 3795), (3812, 3812)],
    "finance": [(6000, 6799)],
    "health": [(2830, 2836), (3841, 3851), (5122, 5122), (5912, 5912), (6324, 6324), (8000, 8099), (8731, 8731)],
    "energy": [(1000, 1499), (2900, 2999), (4900, 4991)],
    "tech": [(3570, 3579), (3600, 3699), (4800, 4899), (7370, 7379)],
    "transport": [(3710, 3716), (4000, 4799)],
    "aerospace": [(3720, 3729), (3760, 3769)],
    "construction": [(1500, 1799)],
    "agriculture": [(100, 999), (2000, 2099), (5140, 5159)],
}
GROUP_LABEL = {"defense": "defense", "finance": "financial", "health": "health care", "energy": "energy",
               "tech": "technology/telecom", "transport": "transportation", "aerospace": "aerospace",
               "construction": "construction", "agriculture": "agriculture/food"}


def sic_groups(sic: int | None) -> set[str]:
    if not sic:
        return set()
    return {g for g, ranges in SIC_GROUPS.items() if any(a <= sic <= b for a, b in ranges)}


def build_members(legislators: list, membership: dict, committees: list, today: date) -> list[dict]:
    names = {c.get("thomas_id"): c.get("name", "") for c in committees}
    by_bio: dict[str, list] = {}
    for cid, people in (membership or {}).items():
        if cid not in names:  # full committees only (subcommittee ids have extra digits)
            continue
        for p in people:
            by_bio.setdefault(p.get("bioguide", ""), []).append({"name": names[cid], "title": p.get("title", "")})
    out = []
    for leg in legislators:
        term = (leg.get("terms") or [{}])[-1]
        nm = leg.get("name", {})
        roles = [r.get("title", "") for r in leg.get("leadership_roles", []) or []
                 if not r.get("end") or r.get("end") >= today.isoformat()]
        comms = by_bio.get(leg.get("id", {}).get("bioguide", ""), [])
        party = (term.get("party") or "")[:1].upper()
        out.append({
            "bioguide": leg.get("id", {}).get("bioguide", ""), "name": nm.get("official_full") or f"{nm.get('first', '')} {nm.get('last', '')}",
            "first": nm.get("first", ""), "nick": nm.get("nickname", ""), "last": nm.get("last", ""),
            "chamber": "Senate" if term.get("type") == "sen" else "House", "state": term.get("state", ""),
            "district": term.get("district"), "party": party if party in ("D", "R") else "I",
            "leadership": roles, "committees": comms,
            "chairs": [c["name"] for c in comms if re.fullmatch(r"(chair|chairman|chairwoman|ranking member)", (c["title"] or "").strip(), re.I)],
        })
    return out


def _norm(s: str) -> str:
    s = re.sub(r"\b(jr|sr|ii|iii|iv|hon|dr|mr|mrs|ms)\b\.?", " ", (s or "").lower())
    return re.sub(r"[^a-z]", "", s)


def match_member(members: list[dict], chamber: str, last: str, first: str, state_dst: str = "") -> dict | None:
    pool = [m for m in members if m["chamber"] == chamber]
    ln = _norm(last.split(",")[0])
    if chamber == "House" and len(state_dst) >= 2:
        st, dist = state_dst[:2].upper(), state_dst[2:]
        try:
            d = int(dist) if dist else 0
        except ValueError:
            d = None
        c = [m for m in pool if m["state"] == st and (m["district"] or 0) == d]
        if len(c) == 1 and (not ln or _norm(c[0]["last"]) in ln or ln in _norm(c[0]["last"])):
            return c[0]
        pool = [m for m in pool if m["state"] == st] or pool
    c = [m for m in pool if _norm(m["last"]) == ln or (ln and ln.endswith(_norm(m["last"])))]
    if len(c) > 1:
        fn = _norm(first)[:3]
        c = [m for m in c if _norm(m["first"]).startswith(fn) or _norm(m["nick"]).startswith(fn)] or c
    return c[0] if len(c) == 1 else None


# ------------------------------------------------------------------ main
class CongressScanner:
    def __init__(self, sec: SecClient, cache: Cache | None = None):
        self.sec = sec
        self.cache = cache or sec.cache
        email = sec.user_agent.split(";")[-1].strip(" )")
        self.http = Http(email, per_second=2.0)
        self.senate_http = Http(email, per_second=1.5, cookies=True)

    # --- reference data
    def _json(self, url: str, key: str, ttl: float):
        found, val = self.cache.get_kv(key, ttl)
        if found:
            return val
        raw = self.http.request(url)
        val = json.loads(raw) if raw else None
        if val is not None:
            self.cache.put_kv(key, val)
        return val

    def members(self) -> list[dict]:
        legs = self._json(LEGISLATORS + "legislators-current.json", "cl:legislators", 24 * 3600) or []
        memb = self._json(LEGISLATORS + "committee-membership-current.json", "cl:membership", 24 * 3600) or {}
        comm = self._json(LEGISLATORS + "committees-current.json", "cl:committees", 7 * 86400) or []
        return build_members(legs, memb, comm, date.today())

    def ticker_map(self) -> dict[str, dict]:
        found, val = self.cache.get_kv("sec:tickers", 7 * 86400)
        if not found:
            try:
                raw = self.sec.get(SEC_TICKERS, ttl=7 * 86400)
            except Exception:  # SEC unreachable: fall back to the last saved list, or names as reported
                found, val = self.cache.get_kv("sec:tickers", None)
                return val or {}
            data = json.loads(raw) if raw else {}
            val = {v["ticker"].upper(): {"cik": str(v["cik_str"]), "name": v["title"]} for v in data.values()}
            self.cache.put_kv("sec:tickers", val)
        return val or {}

    def industry(self, cik: str) -> tuple[str, int | None]:
        found, val = self.cache.get_kv(f"sic:{cik}", 30 * 86400)
        if not found:
            try:
                subs = self.sec.submissions(cik, ttl=30 * 86400) or {}
            except Exception:
                return "", None
            try:
                sic = int(subs.get("sic") or 0) or None
            except ValueError:
                sic = None
            val = [subs.get("sicDescription") or "", sic]
            self.cache.put_kv(f"sic:{cik}", val)
        return val[0], val[1]

    # --- House
    def house(self, since: date, progress: Progress) -> tuple[list[dict], dict]:
        filings = []
        for y in sorted({since.year, date.today().year}):
            key = f"house-index:{y}"
            found, idx = self.cache.get_kv(key, 6 * 3600)
            if not found:
                blob = self.http.request(HOUSE_ZIP.format(year=y))
                idx = parse_house_index(blob) if blob else []
                self.cache.put_kv(key, idx)
            filings += [f for f in idx if f["filed"] and f["filed"] >= since.isoformat()]
        trades, paper = [], 0
        for i, f in enumerate(sorted(filings, key=lambda f: f["filed"], reverse=True)):
            progress("Congress: reading House trade reports", i, len(filings))
            found, parsed = self.cache.get_kv(f"house-ptr:{f['doc']}", None)
            if not found:
                blob = self.http.request(HOUSE_PDF.format(year=f["year"] or f["filed"][:4], doc=f["doc"]))
                txns = parse_house_ptr_text(pdf_text(blob)) if blob else []
                parsed = {"txns": txns, "paper": bool(blob) and not txns}
                if blob is not None:
                    self.cache.put_kv(f"house-ptr:{f['doc']}", parsed)
            paper += 1 if parsed.get("paper") else 0
            for k, t in enumerate(parsed["txns"]):
                trades.append({**t, "id": f"H{f['doc']}-{k}", "chamber": "House", "last": f["last"], "first": f["first"],
                               "state_dst": f["state_dst"], "filed": f["filed"],
                               "url": HOUSE_PDF.format(year=f["year"] or f["filed"][:4], doc=f["doc"])})
        progress("Congress: reading House trade reports", len(filings), len(filings))
        return trades, {"filings": len(filings), "unreadable_paper_filings": paper}

    # --- Senate
    def senate(self, since: date, progress: Progress) -> tuple[list[dict], dict]:
        h = self.senate_http
        home = h.request(f"{SENATE}/search/home/")
        m = re.search(rb'name="csrfmiddlewaretoken"\s+value="([^"]+)"', home or b"")
        if not m:
            raise CongressError("Senate site did not return its agreement page (it may be blocking this network).")
        h.request(f"{SENATE}/search/home/", data={"csrfmiddlewaretoken": m.group(1).decode(), "prohibition_agreement": "1"},
                  headers={"Referer": f"{SENATE}/search/home/"})
        token = h.cookie("csrftoken") or m.group(1).decode()
        rows, start = [], 0
        while True:
            raw = h.request(f"{SENATE}/search/report/data/", data={
                "start": str(start), "length": "100", "report_types": "[11]", "filer_types": "[]",
                "submitted_start_date": since.strftime("%m/%d/%Y 00:00:00"), "submitted_end_date": "",
                "candidate_state": "", "senator_state": "", "office_id": "", "first_name": "", "last_name": "",
                "csrfmiddlewaretoken": token},
                headers={"Referer": f"{SENATE}/search/", "X-CSRFToken": token, "X-Requested-With": "XMLHttpRequest"})
            try:
                data = json.loads(raw or b"{}")
            except ValueError as e:
                raise CongressError("Senate search returned a page instead of data (agreement not accepted).") from e
            batch = data.get("data") or []
            rows += batch
            start += len(batch)
            if not batch or start >= int(data.get("recordsTotal") or 0) or start > 3000:
                break
        filings = []
        for r in rows:
            link = re.search(r'href="([^"]+)"', r[3] if len(r) > 3 else "")
            if not link:
                continue
            href = link.group(1)
            filings.append({"first": r[0], "last": r[1], "href": href if href.startswith("http") else SENATE + href,
                            "paper": "/paper/" in href, "filed": _iso(r[4] if len(r) > 4 else "")})
        trades, paper = [], 0
        for i, f in enumerate(filings):
            progress("Congress: reading Senate trade reports", i, len(filings))
            if f["paper"]:
                paper += 1
                continue
            key = "senate-ptr:" + f["href"].rstrip("/").rsplit("/", 1)[-1]
            found, txns = self.cache.get_kv(key, None)
            if not found:
                html = h.request(f["href"], headers={"Referer": f"{SENATE}/search/"})
                txns = parse_senate_ptr_html(html.decode("utf-8", "replace")) if html else []
                if html is not None:
                    self.cache.put_kv(key, txns)
            for k, t in enumerate(txns):
                trades.append({**t, "id": f"S{key.split(':')[1][:12]}-{k}", "chamber": "Senate", "last": f["last"],
                               "first": f["first"], "state_dst": "", "filed": f["filed"], "url": f["href"]})
        progress("Congress: reading Senate trade reports", len(filings), len(filings))
        return trades, {"filings": len(filings), "unreadable_paper_filings": paper}

    # --- assemble
    def enrich(self, raw: list[dict], members: list[dict], insider_rows: list[dict]) -> list[dict]:
        tick = self.ticker_map()
        insider = {}
        for r in insider_rows or []:
            if r.get("ticker") and (r["ticker"] not in insider or r["score"] > insider[r["ticker"]]["score"]):
                insider[r["ticker"]] = {"id": r["id"], "score": r["score"], "role": r["role"], "value": r["value"]}
        out = []
        for t in raw:
            if t["chamber"] == "House":
                if t.get("asset_code") not in ASSET_CODES:
                    continue
                t["asset_type"] = ASSET_CODES[t["asset_code"]]
            else:
                at = (t.get("asset_type_raw") or "").lower()
                if not (at.startswith("stock") or "option" in at):
                    continue
                t["asset_type"] = "Stock option" if "option" in at else "Stock"
            if not t.get("ticker") or t["type"] not in ("Buy", "Sell", "Sell (partial)", "Exchange"):
                continue
            mem = match_member(members, t["chamber"], t["last"], t["first"], t.get("state_dst", ""))
            info = tick.get(t["ticker"].replace(".", "-")) or tick.get(t["ticker"]) or {}
            industry, sic = self.industry(info["cik"]) if info else ("", None)
            groups = sic_groups(sic)
            overlap = []
            if mem and groups:
                for c in mem["committees"]:
                    for pat, gs in COMMITTEE_GROUPS:
                        if re.search(pat, c["name"]) and groups & set(gs):
                            overlap.append(c["name"])
                            break
            delay = None
            if t.get("trade_date") and t.get("filed"):
                delay = (date.fromisoformat(t["filed"]) - date.fromisoformat(t["trade_date"])).days
            out.append({
                "id": t["id"], "chamber": t["chamber"],
                "member": mem["name"] if mem else f"{t['first']} {t['last']}".strip(),
                "matched": bool(mem), "party": mem["party"] if mem else "", "state": mem["state"] if mem else t.get("state_dst", "")[:2],
                "district": mem["district"] if mem and t["chamber"] == "House" else None,
                "leadership": mem["leadership"] if mem else [], "chairs": mem["chairs"] if mem else [],
                "committees": [c["name"] + (f" ({c['title']})" if c.get("title") else "") for c in (mem["committees"] if mem else [])],
                "key_person": bool(mem and (mem["leadership"] or mem["chairs"])),
                "owner": t.get("owner", "Self"), "ticker": t["ticker"], "asset": t.get("asset", ""), "asset_type": t["asset_type"],
                "type": t["type"], "trade_date": t.get("trade_date"), "filed": t.get("filed"), "delay_days": delay,
                "late": delay is not None and delay > LATE_DAYS, "amount": t.get("amount", ""),
                "amount_low": t.get("amount_low"), "amount_high": t.get("amount_high"),
                "company": info.get("name") or t.get("asset", ""), "industry": industry,
                "oversight": sorted(set(overlap)),
                "oversight_industry": ", ".join(sorted(GROUP_LABEL[g] for g in groups)) if overlap else "",
                "insider_buying": insider.get(t["ticker"]), "comment": t.get("comment", ""), "url": t["url"],
            })
        out.sort(key=lambda r: (r["filed"] or "", r["trade_date"] or ""), reverse=True)
        return out


def priceable(t: dict) -> bool:
    """Stock purchases we can compare with today's price (options and sells are skipped)."""
    return t.get("type") == "Buy" and t.get("asset_type") == "Stock" and bool(t.get("ticker")) and bool(t.get("trade_date"))


def add_prices(sec: SecClient, trades: list[dict], progress: Progress = lambda *a: None) -> dict:
    """Estimate each Congress purchase price and compare it with the latest price.

    Disclosures give only a dollar range, never the price paid, so the purchase price is
    estimated as the stock's closing price on the trade date (Yahoo Finance, split-adjusted).
    Adds buy_price, price_now, price_asof, price_change (fraction) and below_buy to each buy."""
    for t in trades:  # drop old numbers (e.g. on rows kept from the last run) so nothing goes stale
        for k in ("buy_price", "price_now", "price_asof", "price_change", "below_buy"):
            t.pop(k, None)
    buys = [t for t in trades if priceable(t)]
    by_ticker: dict[str, list[dict]] = {}
    for t in buys:
        by_ticker.setdefault(t["ticker"].upper(), []).append(t)
    priced = failed = 0
    tickers = sorted(by_ticker)
    for i, tk in enumerate(tickers):
        progress("Congress: checking prices of purchased stocks", i, len(tickers))
        rows = by_ticker[tk]
        first = min(date.fromisoformat(r["trade_date"]) for r in rows) - timedelta(days=10)
        data = sec.yahoo_prices(tk, start=first.replace(day=1))  # 1st of month keeps the cache key stable
        if not data or not data.get("d"):
            failed += 1
            continue
        d, c, last = data["d"], data["c"], data.get("last") or data["c"][-1]
        for r in rows:
            j = bisect_right(d, r["trade_date"]) - 1
            if j < 0 or not c[j] or not last:
                continue
            r["buy_price"] = round(c[j], 2)
            r["price_now"] = round(float(last), 2)
            r["price_asof"] = d[-1]
            r["price_change"] = round(float(last) / c[j] - 1, 4)
            r["below_buy"] = float(last) < c[j]
            priced += 1
    progress("Congress: checking prices of purchased stocks", len(tickers), len(tickers))
    return {"buys": len(buys), "priced": priced, "tickers_without_prices": failed}


def run_congress_scan(sec: SecClient, days: int = 90, insider_rows: list[dict] | None = None,
                      previous: dict | None = None, progress: Progress = lambda *a: None,
                      use_prices: bool = True) -> dict:
    """Collect the last `days` of disclosed trades. A chamber that fails keeps its previous data."""
    sc = CongressScanner(sec)
    since = date.today() - timedelta(days=days)
    status, raw = {}, []
    prev_rows = (previous or {}).get("trades", [])
    for chamber, fn in (("House", sc.house), ("Senate", sc.senate)):
        try:
            rows, info = fn(since, progress)
            raw += rows
            status[chamber] = {"ok": True, **info}
        except Exception as e:  # keep going with the other chamber
            status[chamber] = {"ok": False, "error": str(e)[:300]}
    try:
        members = sc.members()
    except Exception as e:
        members = []
        status["members"] = {"ok": False, "error": str(e)[:300]}
    trades = sc.enrich(raw, members, insider_rows or [])
    for chamber in ("House", "Senate"):
        if not status[chamber]["ok"]:  # reuse the last good data for that chamber
            kept = [r for r in prev_rows if r["chamber"] == chamber and (r.get("filed") or "") >= since.isoformat()]
            trades += kept
            status[chamber]["kept_previous"] = len(kept)
    trades.sort(key=lambda r: (r["filed"] or "", r["trade_date"] or ""), reverse=True)
    prices = None
    if use_prices:
        try:
            prices = add_prices(sec, trades, progress)
        except Exception as e:  # prices are a bonus; never lose the feed over them
            prices = {"error": str(e)[:300]}
    return {"meta": {"scanned_at": datetime.now().isoformat(timespec="seconds"), "days": days,
                     "from": since.isoformat(), "sources": status, "members_loaded": len(members),
                     "prices": prices},
            "trades": trades}
