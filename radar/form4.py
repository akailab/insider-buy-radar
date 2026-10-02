"""Parse SEC Form 4 filings and summarise open-market purchases."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date, timedelta


def _bool(text: str | None) -> bool:
    return (text or "").strip().lower() in ("1", "true", "y", "yes")


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip().replace(",", "").replace("$", "")
    try:
        return float(t)
    except ValueError:
        return None


def _val(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path + "/value")
    if node is None:
        node = el.find(path)
    return node.text.strip() if node is not None and node.text and node.text.strip() else None


def _header(txt: str, label: str) -> str | None:
    m = re.search(rf"^\s*{label}:\s*(\S+)", txt, re.M)
    return m.group(1) if m else None


def _iso(d: str | None) -> str | None:
    if not d:
        return None
    d = d.strip()[:10]
    if re.fullmatch(r"\d{8}", d):
        return f"{d[:4]}-{d[4:6]}-{d[6:]}"
    return d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None


EXCLUDED_SECURITY = re.compile(r"preferred|warrant|\bunits?\b|\bnotes?\b|debenture|\brights?\b", re.I)


def parse_form4(txt: str, path: str) -> dict | None:
    """Turn a full EDGAR submission (.txt) into a compact dict. None if not a Form 4 / 4/A."""
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", txt, re.S)
    if not m:
        return None
    xml = m.group(0)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        try:
            root = ET.fromstring(re.sub(r"&(?!\w+;|#\d+;)", "&amp;", xml))
        except ET.ParseError:
            return None

    doc_type = (root.findtext("documentType") or "").strip()
    if doc_type not in ("4", "4/A"):
        return None

    accession = _header(txt, "ACCESSION NUMBER") or path.rsplit("/", 1)[-1].replace(".txt", "")
    filed = _iso(_header(txt, "FILED AS OF DATE")) or _iso(root.findtext("periodOfReport")) or ""

    issuer = root.find("issuer")
    owners = []
    for ro in root.findall("reportingOwner"):
        rid, rel = ro.find("reportingOwnerId"), ro.find("reportingOwnerRelationship")
        owners.append(
            {
                "cik": (rid.findtext("rptOwnerCik") or "").strip().lstrip("0") if rid is not None else "",
                "name": (rid.findtext("rptOwnerName") or "").strip() if rid is not None else "",
                "is_director": _bool(rel.findtext("isDirector")) if rel is not None else False,
                "is_officer": _bool(rel.findtext("isOfficer")) if rel is not None else False,
                "is_ten_pct": _bool(rel.findtext("isTenPercentOwner")) if rel is not None else False,
                "title": (rel.findtext("officerTitle") or "").strip() if rel is not None else "",
            }
        )

    footnotes = " ".join((f.text or "") for f in root.iter("footnote"))
    remarks = root.findtext("remarks") or ""
    aff = root.findtext("aff10b5One")
    plan = _bool(aff) if aff is not None else None
    if not plan and re.search(r"10b5-?1", footnotes + " " + remarks, re.I):
        plan = True  # older filings disclose plans only in footnotes

    def line_of(el: ET.Element) -> tuple[str, str]:
        nature = el.find("ownershipNature")
        di = _val(nature, "directOrIndirectOwnership") or "D"
        how = (_val(nature, "natureOfOwnership") or "").strip().lower()
        return di, how

    txns, holdings = [], []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        coding, amounts = t.find("transactionCoding"), t.find("transactionAmounts")
        di, how = line_of(t)
        txns.append(
            {
                "code": (coding.findtext("transactionCode") or "").strip() if coding is not None else "",
                "date": _iso(_val(t, "transactionDate")),
                "shares": _num(_val(amounts, "transactionShares")),
                "price": _num(_val(amounts, "transactionPricePerShare")),
                "ad": _val(amounts, "transactionAcquiredDisposedCode"),
                "after": _num(_val(t.find("postTransactionAmounts"), "sharesOwnedFollowingTransaction")),
                "direct": di,
                "nature": how,
                "security": _val(t, "securityTitle") or "",
            }
        )
    for h in root.findall("nonDerivativeTable/nonDerivativeHolding"):
        di, how = line_of(h)
        holdings.append(
            {
                "after": _num(_val(h.find("postTransactionAmounts"), "sharesOwnedFollowingTransaction")),
                "direct": di,
                "nature": how,
                "security": _val(h, "securityTitle") or "",
            }
        )

    # Options / RSUs / other derivatives still held after this filing (underlying shares)
    deriv_lines: dict[tuple, float] = {}
    for d in root.findall("derivativeTable/derivativeTransaction") + root.findall("derivativeTable/derivativeHolding"):
        after = _num(_val(d.find("postTransactionAmounts"), "sharesOwnedFollowingTransaction"))
        und = _num(_val(d.find("underlyingSecurity"), "underlyingSecurityShares"))
        key = (_val(d, "securityTitle") or "", _val(d, "conversionOrExercisePrice") or "", _val(d, "expirationDate") or "")
        if after is not None:
            deriv_lines[key] = after
        elif und is not None and key not in deriv_lines:
            deriv_lines[key] = und

    return {
        "path": path,
        "accession": accession,
        "filed": filed,
        "doc_type": doc_type,
        "orig_date": _iso(root.findtext("dateOfOriginalSubmission")),
        "issuer_cik": (issuer.findtext("issuerCik") or "").strip().lstrip("0") if issuer is not None else "",
        "issuer_name": (issuer.findtext("issuerName") or "").strip() if issuer is not None else "",
        "ticker": (issuer.findtext("issuerTradingSymbol") or "").strip().upper() if issuer is not None else "",
        "owners": owners,
        "plan_10b5_1": plan,
        "txns": txns,
        "holdings": holdings,
        "derivative_shares": sum(deriv_lines.values()),
    }


# ---------------------------------------------------------------- roles
ROLE_ORDER = ["CEO", "CFO", "COO", "President", "CTO", "Other C-suite", "Chair"]
ROLE_POINTS = {"CEO": 1.0, "CFO": 0.9, "COO": 0.8, "President": 0.8, "CTO": 0.7, "Other C-suite": 0.65, "Chair": 0.7}

_I = re.I
_PATTERNS = [
    ("CEO", re.compile(r"\bC\.?E\.?O\b|chief\s+executive", _I)),
    ("CFO", re.compile(r"\bC\.?F\.?O\b|chief\s+financial|principal\s+financial", _I)),
    ("COO", re.compile(r"\bC\.?O\.?O\b|chief\s+operating", _I)),
    ("President", re.compile(r"\bpresident\b", _I)),
    ("CTO", re.compile(r"\bC\.?T\.?O\b|chief\s+technology|chief\s+technical", _I)),
    ("Other C-suite", re.compile(r"(?i:\bchief\s+[a-z&,/\s-]*?officer\b)|\bC[A-Z]{1,2}O\b")),
]
_VP = re.compile(r"\b(senior\s+|executive\s+|group\s+|corporate\s+)?vice[\s-]+president\b|\b[SE]?VP\b", _I)


def classify_role(owner: dict) -> str | None:
    """Best C-suite role for a reporting owner (from their officer title), or None."""
    title = _VP.sub(" ", owner.get("title") or "")
    for role, pat in _PATTERNS:
        if pat.search(title):
            return role
    if re.search(r"\bchair", title, _I):
        return "Chair"
    return None


# ------------------------------------------------------------ purchases
def business_days_between(a: str, b: str) -> int | None:
    """Business days after date a up to and including date b (weekends skipped, holidays not)."""
    try:
        d1, d2 = date.fromisoformat(a), date.fromisoformat(b)
    except (TypeError, ValueError):
        return None
    n, d = 0, d1
    while d < d2:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def purchase_summary(filing: dict) -> dict | None:
    """Aggregate all open-market purchases (code P) in one filing. None if there are none."""
    rows = [
        t for t in filing["txns"]
        if t["code"] == "P" and (t["ad"] or "A") == "A" and (t["shares"] or 0) > 0 and (t["price"] or 0) > 0
    ]
    if not rows:
        return None
    shares = sum(t["shares"] for t in rows)
    value = sum(t["shares"] * t["price"] for t in rows)

    # Latest share count on every ownership line (direct, each trust, spouse, ...)
    lines: dict[tuple, float] = {}
    for e in filing["txns"] + filing.get("holdings", []):
        if EXCLUDED_SECURITY.search(e.get("security") or "") or e.get("after") is None:
            continue
        lines[(e["direct"], e.get("nature", ""), (e.get("security") or "").lower())] = e["after"]
    bought_lines = {(t["direct"], t.get("nature", ""), (t.get("security") or "").lower()) for t in rows}
    total_after = sum(lines.values()) if lines else None
    total_before = None
    if total_after is not None and all(k in lines for k in bought_lines):
        total_before = max(0.0, total_after - shares)
    direct_after = sum(v for k, v in lines.items() if k[0] == "D") if lines else None
    indirect_after = sum(v for k, v in lines.items() if k[0] == "I") if lines else None

    pct = None if not total_before else shares / total_before
    deriv = filing.get("derivative_shares") or 0.0
    pct_incl_options = None
    if total_before is not None and total_before + deriv > 0:
        pct_incl_options = shares / (total_before + deriv)

    dates = sorted(t["date"] for t in rows if t["date"])
    last_date = dates[-1] if dates else filing["filed"]
    bdays = business_days_between(last_date, filing["filed"])
    return {
        "shares": shares,
        "value": value,
        "avg_price": value / shares,
        "first_date": dates[0] if dates else filing["filed"],
        "last_date": last_date,
        "trade_dates": sorted(set(dates)),
        "holdings_before": total_before,
        "holdings_after": total_after,
        "direct_after": direct_after,
        "indirect_after": indirect_after,
        "options_held": deriv,
        "pct_increase": pct,
        "pct_increase_incl_options": pct_incl_options,
        "new_position": total_before == 0,
        "n_lots": len(rows),
        "filing_lag_bdays": bdays,
        "late": bdays is not None and bdays > 2,
    }


def sale_value(filing: dict) -> float:
    return sum((t["shares"] or 0) * (t["price"] or 0) for t in filing["txns"] if t["code"] == "S")
