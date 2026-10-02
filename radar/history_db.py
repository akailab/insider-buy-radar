"""Local database of every insider open-market trade, built from the SEC's quarterly
Form 3/4/5 bulk data sets (free, published a few weeks after each quarter ends).

Used for: insider history, routine-vs-opportunistic classification, track record,
tenure (new executive), and the backtest.
"""
from __future__ import annotations

import csv
import io
import sqlite3
import threading
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

from .form4 import classify_role
from .sec_client import SecClient, SecError

_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

csv.field_size_limit(10_000_000)


def parse_date(s: str | None) -> str | None:
    """'05-JAN-2024', '2024-01-05', '01/05/2024' -> '2024-01-05'."""
    if not s:
        return None
    s = s.strip()
    try:
        if len(s) >= 11 and s[2] == "-" and s[6] == "-":
            return date(int(s[7:11]), _MONTHS[s[3:6].upper()], int(s[:2])).isoformat()
        if len(s) >= 10 and s[4] == "-":
            return s[:10]
        if "/" in s:
            m, d, y = s.split()[0].split("/")
            return date(int(y), int(m), int(d)).isoformat()
    except (KeyError, ValueError, IndexError):
        return None
    return None


def _f(s: str | None) -> float | None:
    try:
        return float(s) if s not in (None, "") else None
    except ValueError:
        return None


def quarter_of(d: date) -> str:
    return f"{d.year}q{(d.month - 1) // 3 + 1}"


def quarters_between(start: date, end: date) -> list[str]:
    out, y, q = [], start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append(f"{y}q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return out


def quarter_end(q: str) -> date:
    y, n = int(q[:4]), int(q[5])
    return date(y, 12, 31) if n == 4 else date(y, n * 3 + 1, 1) - timedelta(days=1)


class HistoryDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS quarters (q TEXT PRIMARY KEY, loaded REAL, n INTEGER);
                CREATE TABLE IF NOT EXISTS trades (
                    acc TEXT, owner_cik TEXT, owner_name TEXT, issuer_cik TEXT, ticker TEXT, filed TEXT, tdate TEXT,
                    code TEXT, shares REAL, price REAL, value REAL, after REAL, di TEXT,
                    title TEXT, role TEXT, is_officer INTEGER, is_director INTEGER, plan INTEGER);
                CREATE INDEX IF NOT EXISTS t_owner ON trades(owner_cik, issuer_cik);
                CREATE INDEX IF NOT EXISTS t_issuer ON trades(issuer_cik, filed);
                CREATE INDEX IF NOT EXISTS t_filed ON trades(filed);
                CREATE TABLE IF NOT EXISTS seen (owner_cik TEXT, issuer_cik TEXT, first_filed TEXT,
                    PRIMARY KEY (owner_cik, issuer_cik));
                """
            )
            self.db.commit()

    # ----------------------------------------------------------- loading
    def loaded_quarters(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.db.execute("SELECT q FROM quarters")}

    def coverage_end(self) -> str | None:
        qs = sorted(self.loaded_quarters())
        return quarter_end(qs[-1]).isoformat() if qs else None

    def coverage_start(self) -> str | None:
        qs = sorted(self.loaded_quarters())
        return quarters_start(qs[0]) if qs else None

    def ensure(self, client: SecClient, quarters: list[str], progress: Callable[[str, int, int], None]) -> list[str]:
        """Download and load any missing quarters. Returns quarters that couldn't be loaded."""
        have = self.loaded_quarters()
        todo = [q for q in quarters if q not in have]
        if not todo:
            return []
        listing = client.dataset_quarters()
        failed = []
        for i, q in enumerate(todo):
            progress("Building insider history database (one-time, SEC bulk data)", i, len(todo))
            try:
                blob = client.dataset_zip(q, listing.get(q))
            except SecError:
                blob = None
            if not blob:
                failed.append(q)
                continue
            self.load_zip(q, blob)
        progress("Building insider history database (one-time, SEC bulk data)", len(todo), len(todo))
        return failed

    def load_zip(self, q: str, blob: bytes) -> int:
        zf = zipfile.ZipFile(io.BytesIO(blob))
        names = {n.split("/")[-1].upper(): n for n in zf.namelist()}

        def rows(name: str):
            member = names.get(name)
            if not member:
                return
            with zf.open(member) as fh:
                reader = csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"), delimiter="\t",
                                        quoting=csv.QUOTE_NONE)
                for r in reader:
                    yield {(k or "").strip().upper(): (v or "").strip() for k, v in r.items()}

        subs = {}
        for r in rows("SUBMISSION.TSV"):
            doc = r.get("DOCUMENT_TYPE") or r.get("FORM_TYPE") or ""
            subs[r["ACCESSION_NUMBER"]] = (
                parse_date(r.get("FILING_DATE")), doc, (r.get("ISSUERCIK") or "").lstrip("0"),
                (r.get("ISSUERTRADINGSYMBOL") or "").upper(),
                1 if (r.get("AFF10B5ONE") or "").strip().lower() in ("1", "true", "y") else 0,
            )
        owners: dict[str, list] = {}
        for r in rows("REPORTINGOWNER.TSV"):
            rel = (r.get("RPTOWNER_RELATIONSHIP") or "").lower()
            owners.setdefault(r["ACCESSION_NUMBER"], []).append(
                ((r.get("RPTOWNERCIK") or "").lstrip("0"), r.get("RPTOWNERNAME") or "", r.get("RPTOWNER_TITLE") or "",
                 1 if "officer" in rel else 0, 1 if "director" in rel else 0)
            )

        seen: dict[tuple, str] = {}
        for acc, (filed, doc, icik, _t, _p) in subs.items():
            if not filed:
                continue
            for o in owners.get(acc, []):
                k = (o[0], icik)
                if k not in seen or filed < seen[k]:
                    seen[k] = filed

        batch = []
        for r in rows("NONDERIV_TRANS.TSV"):
            code = r.get("TRANS_CODE")
            if code not in ("P", "S"):
                continue
            acc = r["ACCESSION_NUMBER"]
            sub = subs.get(acc)
            if not sub or sub[1] != "4":
                continue
            shares, price = _f(r.get("TRANS_SHARES")), _f(r.get("TRANS_PRICEPERSHARE"))
            if not shares or price is None:
                continue
            filed, _doc, icik, ticker, plan = sub
            tdate = parse_date(r.get("TRANS_DATE")) or filed
            for (ocik, oname, title, is_off, is_dir) in owners.get(acc, []):
                role = classify_role({"title": title})
                batch.append((acc, ocik, oname, icik, ticker, filed, tdate, code, shares, price, shares * price,
                              _f(r.get("SHRS_OWND_FOLWNG_TRANS")), r.get("DIRECT_INDIRECT_OWNERSHIP") or "D",
                              title, role, is_off, is_dir, plan))
        with self.lock:
            self.db.execute("DELETE FROM trades WHERE acc IN (SELECT acc FROM trades WHERE filed BETWEEN ? AND ?)",
                            (quarters_start(q), quarter_end(q).isoformat()))
            self.db.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            for (ocik, icik), filed in seen.items():
                self.db.execute(
                    "INSERT INTO seen VALUES (?,?,?) ON CONFLICT(owner_cik, issuer_cik) DO UPDATE SET "
                    "first_filed = MIN(first_filed, excluded.first_filed)", (ocik, icik, filed))
            self.db.execute("INSERT OR REPLACE INTO quarters VALUES (?,?,?)", (q, datetime.now().timestamp(), len(batch)))
            self.db.commit()
        return len(batch)

    # ----------------------------------------------------------- queries
    def owner_trades(self, owner_cik: str, issuer_cik: str, before: str) -> list[dict]:
        """Open-market buys/sells by this insider in this company filed before `before` (ISO date)."""
        with self.lock:
            cur = self.db.execute(
                "SELECT acc, filed, tdate, code, SUM(shares), SUM(value) FROM trades "
                "WHERE owner_cik=? AND issuer_cik=? AND filed < ? GROUP BY acc, code ORDER BY tdate",
                (owner_cik, issuer_cik, before))
            return [{"acc": a, "filed": f, "date": t, "code": c, "shares": s, "value": v} for a, f, t, c, s, v in cur]

    def first_seen(self, owner_cik: str, issuer_cik: str) -> str | None:
        with self.lock:
            row = self.db.execute("SELECT first_filed FROM seen WHERE owner_cik=? AND issuer_cik=?",
                                  (owner_cik, issuer_cik)).fetchone()
        return row[0] if row else None

    def issuer_known(self, issuer_cik: str, since: str) -> bool:
        """True if the database has insider filings for this company since `since`."""
        with self.lock:
            row = self.db.execute("SELECT 1 FROM seen WHERE issuer_cik=? AND first_filed >= ? LIMIT 1", (issuer_cik, since)).fetchone()
            if not row:
                row = self.db.execute("SELECT 1 FROM trades WHERE issuer_cik=? AND filed >= ? LIMIT 1", (issuer_cik, since)).fetchone()
        return bool(row)

    def issuer_trades(self, issuer_cik: str, start: str, end: str) -> list[dict]:
        with self.lock:
            cur = self.db.execute(
                "SELECT owner_cik, filed, tdate, code, SUM(value) FROM trades WHERE issuer_cik=? AND filed BETWEEN ? AND ? "
                "GROUP BY acc, owner_cik, code", (issuer_cik, start, end))
            return [{"owner_cik": o, "filed": f, "date": t, "code": c, "value": v} for o, f, t, c, v in cur]

    def csuite_buys(self, start: str, end: str, min_value: float) -> list[dict]:
        """One row per (filing, C-suite owner) with open-market purchases - for the backtest."""
        with self.lock:
            cur = self.db.execute(
                "SELECT acc, owner_cik, owner_name, issuer_cik, ticker, filed, MIN(tdate), MAX(tdate), SUM(shares), SUM(value), "
                "MAX(after), title, role, MAX(plan) FROM trades WHERE code='P' AND role IS NOT NULL AND filed BETWEEN ? AND ? "
                "GROUP BY acc, owner_cik HAVING SUM(value) >= ?", (start, end, min_value))
            cols = ["acc", "owner_cik", "insider", "issuer_cik", "ticker", "filed", "first_date", "last_date", "shares",
                    "value", "after", "title", "role", "plan"]
            return [dict(zip(cols, r)) for r in cur]


def quarters_start(q: str) -> str:
    y, n = int(q[:4]), int(q[5])
    return date(y, (n - 1) * 3 + 1, 1).isoformat()


def last_published_quarter_guess(today: date) -> str:
    """The quarter before the current one (the SEC posts it a few weeks after quarter end)."""
    y, n = today.year, (today.month - 1) // 3 + 1
    n -= 1
    if n == 0:
        y, n = y - 1, 4
    return f"{y}q{n}"
