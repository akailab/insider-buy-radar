"""Polite client for SEC EDGAR (and Yahoo Finance prices).

- Standard library only (certifi is used when installed).
- SEC fair-access rules: descriptive User-Agent with a contact e-mail, <= ~8 requests/second.
- A local SQLite cache keeps repeat scans fast.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from datetime import date, datetime, timezone
from pathlib import Path

CACHE_DIR = Path(os.environ.get("INSIDER_RADAR_HOME", Path.home() / ".insider_radar"))

SEC_WWW = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
DATASET_PAGE = f"{SEC_WWW}/data-research/sec-markets-data/insider-transactions-data-sets"
DATASET_URL = f"{SEC_WWW}/files/structureddata/data/insider-transactions-data-sets/{{q}}_form345.zip"


class SecError(RuntimeError):
    pass


class SecBlocked(SecError):
    """SEC refused the request (usually too fast, or a missing User-Agent)."""


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            if self.next_slot > now:
                time.sleep(self.next_slot - now)
                now = self.next_slot
            self.next_slot = now + self.interval


class Cache:
    """Thread-safe SQLite cache: raw HTTP bodies, parsed filings and small computed summaries."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS http (url TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched REAL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS parsed (key TEXT PRIMARY KEY, body TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, body BLOB, fetched REAL)")
            self.db.commit()

    # raw http
    def get_http(self, url: str):
        with self.lock:
            return self.db.execute("SELECT status, body, fetched FROM http WHERE url=?", (url,)).fetchone()

    def put_http(self, url: str, status: int, body: str | None) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO http VALUES (?,?,?,?)", (url, status, body, time.time()))
            self.db.commit()

    # parsed filings (never expire - filings don't change)
    def get_parsed(self, key: str) -> tuple[bool, object]:
        with self.lock:
            row = self.db.execute("SELECT body FROM parsed WHERE key=?", (key,)).fetchone()
        return (False, None) if row is None else (True, json.loads(row[0]))

    def put_parsed(self, key: str, value) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO parsed VALUES (?,?)", (key, json.dumps(value)))
            self.db.commit()

    # small computed values with an age limit (compressed JSON)
    def get_kv(self, key: str, ttl: float | None) -> tuple[bool, object]:
        with self.lock:
            row = self.db.execute("SELECT body, fetched FROM kv WHERE key=?", (key,)).fetchone()
        if row is None or (ttl is not None and time.time() - row[1] > ttl):
            return False, None
        return True, json.loads(zlib.decompress(row[0]))

    def put_kv(self, key: str, value) -> None:
        blob = zlib.compress(json.dumps(value).encode(), 6)
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?,?,?)", (key, blob, time.time()))
            self.db.commit()

    def prune(self, max_age_days: int = 40) -> None:
        """Drop stale raw HTTP entries so the cache doesn't grow forever."""
        cutoff = time.time() - max_age_days * 86400
        with self.lock:
            self.db.execute("DELETE FROM http WHERE fetched < ? AND url NOT LIKE '%daily-index%'", (cutoff,))
            self.db.execute("DELETE FROM kv WHERE fetched < ?", (cutoff,))
            self.db.commit()


class SecClient:
    def __init__(self, contact_email: str, per_second: float = 8.0, cache_dir: Path = CACHE_DIR):
        if not contact_email or "@" not in contact_email:
            raise SecError("SEC requires a contact e-mail in the User-Agent. Add yours in Settings.")
        self.user_agent = f"InsiderBuyRadar/2.0 (personal research; {contact_email})"
        self.limiter = RateLimiter(per_second)
        self.yahoo_limiter = RateLimiter(2.0)
        self.yahoo_failures = 0
        self.cache_dir = Path(cache_dir)
        self.cache = Cache(self.cache_dir / "cache.sqlite3")
        self.ctx = _ssl_context()

    # ------------------------------------------------------------------ http
    def _fetch(self, url: str, headers: dict, limiter: RateLimiter, timeout: int = 60, attempts: int = 5) -> tuple[int, bytes]:
        """Returns (status, body). Retries transient errors. Raises SecBlocked/SecError."""
        last_err: Exception | None = None
        for attempt in range(attempts):
            limiter.wait()
            req = urllib.request.Request(url, headers={**headers, "Accept-Encoding": "gzip, deflate"})
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    elif resp.headers.get("Content-Encoding") == "deflate":
                        raw = zlib.decompress(raw)
                    return 200, raw
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return 404, b""
                last_err = e
                if e.code == 403 and attempt >= 2 and "sec.gov" in url:
                    raise SecBlocked(
                        "SEC refused the request (HTTP 403). This usually means requests were too fast or the "
                        "contact e-mail is missing. Wait a few minutes and try again."
                    ) from e
                if e.code in (400, 401):
                    return e.code, b""
                time.sleep(min(30, 2 * 2**attempt))
            except ssl.SSLError as e:
                raise SecError(
                    "Secure connection failed. On a Mac with Python from python.org, run 'Install Certificates.command' "
                    "in your Python folder, or use the start script (it installs certifi)."
                ) from e
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                time.sleep(min(30, 2 * 2**attempt))
        raise SecError(f"Could not reach {urllib.parse.urlsplit(url).netloc} ({url}): {last_err}")

    def get(self, url: str, ttl: float | None = None, cache: bool = True) -> str | None:
        """GET text from SEC. None on 404. ttl=None caches forever; cache=False skips the cache."""
        if cache:
            hit = self.cache.get_http(url)
            if hit is not None:
                status, body, fetched = hit
                if ttl is None or time.time() - fetched < ttl:
                    return body if status == 200 else None
        status, raw = self._fetch(url, {"User-Agent": self.user_agent}, self.limiter)
        text = raw.decode("utf-8", errors="replace") if status == 200 else None
        if cache and status in (200, 404):
            self.cache.put_http(url, status, text)
        return text

    def get_bytes(self, url: str) -> bytes | None:
        status, raw = self._fetch(url, {"User-Agent": self.user_agent}, self.limiter, timeout=300)
        return raw if status == 200 else None

    # ------------------------------------------------------------ endpoints
    def daily_form4_paths(self, d: date) -> list[str] | None:
        """All Form 4 and 4/A filings in the EDGAR daily index for date d (None if not published)."""
        q = (d.month - 1) // 3 + 1
        url = f"{SEC_WWW}/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"
        text = self.get(url, ttl=None)
        if text is None:
            hit = self.cache.get_http(url)
            if hit and time.time() - hit[2] > 3 * 3600:  # a missing index may be published later
                text = self.get(url, ttl=0)
            if text is None:
                return None
        return parse_daily_index(text)

    def current_form4_paths(self, since: datetime, max_pages: int = 40) -> list[str]:
        paths: list[str] = []
        for page in range(max_pages):
            url = (
                f"{SEC_WWW}/cgi-bin/browse-edgar?action=getcurrent&type=4&company=&dateb="
                f"&owner=include&start={page * 100}&count=100&output=atom"
            )
            text = self.get(url, ttl=600)
            if not text:
                break
            entries, oldest, n_entries = parse_current_feed(text)
            paths.extend(entries)
            if n_entries < 100 or (oldest is not None and oldest < since):
                break
        return paths

    def filing_text(self, path: str) -> str | None:
        return self.get(f"{SEC_WWW}/Archives/{path}", cache=False)

    def document(self, cik: str | int, accession: str, filename: str) -> str | None:
        acc = accession.replace("-", "")
        return self.get(f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc}/{filename}", cache=False)

    def submissions(self, cik: str | int, ttl: float = 12 * 3600) -> dict | None:
        text = self.get(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json", ttl=ttl)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    def companyfacts(self, cik: str | int) -> dict | None:
        """Full XBRL company facts (large - not cached raw; callers cache a summary)."""
        text = self.get(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{int(cik):010d}.json", cache=False)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    def company_concept(self, cik: str | int, taxonomy: str, tag: str) -> dict | None:
        text = self.get(f"{SEC_DATA}/api/xbrl/companyconcept/CIK{int(cik):010d}/{taxonomy}/{tag}.json", ttl=30 * 86400)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return None

    def dataset_quarters(self) -> dict[str, str]:
        """{'2026q2': url, ...} for the SEC's quarterly Form 3/4/5 bulk data sets."""
        found: dict[str, str] = {}
        try:
            page = self.get(DATASET_PAGE, ttl=24 * 3600)
        except SecError:
            page = None
        for href in re.findall(r'href="([^"]*?(\d{4}q[1-4])_form345\.zip)"', page or "", re.I):
            url = href[0] if href[0].startswith("http") else SEC_WWW + href[0]
            found[href[1].lower()] = url
        return found

    def dataset_zip(self, quarter: str, url: str | None = None) -> bytes | None:
        return self.get_bytes(url or DATASET_URL.format(q=quarter))

    # ------------------------------------------------------------- prices
    def yahoo_prices(self, ticker: str, start: date | None = None) -> dict | None:
        """Daily close & volume from Yahoo Finance (best effort; None when unavailable).
        Returns {'d': [iso dates], 'c': [close], 'v': [volume], 'last': float}."""
        t = re.sub(r"[^A-Za-z0-9.\-^]", "", ticker or "").upper().replace(".", "-")
        if not t:
            return None
        rng = f"p{start.isoformat()}" if start else "5y"
        key = f"yahoo:{t}:{rng}"
        found, value = self.cache.get_kv(key, ttl=6 * 3600)
        if found:
            return value
        if start:
            p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?period1={p1}&period2={int(time.time())}&interval=1d"
        else:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?range=5y&interval=1d"
        if self.yahoo_failures >= 8:  # Yahoo is refusing us - skip prices for the rest of this run
            return None
        try:
            status, raw = self._fetch(url, {"User-Agent": "Mozilla/5.0"}, self.yahoo_limiter, timeout=30, attempts=2)
        except SecError:
            self.yahoo_failures += 1
            return None
        self.yahoo_failures = 0 if status == 200 else self.yahoo_failures
        value = None
        if status == 200:
            try:
                res = json.loads(raw)["chart"]["result"][0]
                q = res["indicators"]["quote"][0]
                n = len(q["close"])
                adj = ((res["indicators"].get("adjclose") or [{}])[0].get("adjclose")) or [None] * n
                d, c, v, a = [], [], [], []
                for ts, close, vol, ac in zip(res["timestamp"], q["close"], q.get("volume") or [None] * n, adj):
                    if close:
                        d.append(datetime.fromtimestamp(ts, timezone.utc).date().isoformat())
                        c.append(round(float(close), 4))
                        v.append(int(vol or 0))
                        a.append(round(float(ac), 4) if ac else round(float(close), 4))
                if d:
                    value = {"d": d, "c": c, "v": v, "a": a, "last": res.get("meta", {}).get("regularMarketPrice") or c[-1]}
            except (KeyError, IndexError, TypeError, ValueError):
                value = None
        self.cache.put_kv(key, value)
        return value


# ---------------------------------------------------------------- parsers
def parse_daily_index(text: str) -> list[str]:
    paths = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) >= 4 and tokens[0] in ("4", "4/A") and tokens[-1].startswith("edgar/"):
            paths.append(tokens[-1])
    return sorted(set(paths))


_ATOM = "{http://www.w3.org/2005/Atom}"


def parse_current_feed(text: str) -> tuple[list[str], datetime | None, int]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return [], None, 0
    entries = root.findall(f"{_ATOM}entry")
    paths, oldest = [], None
    for entry in entries:
        updated = entry.findtext(f"{_ATOM}updated")
        if updated:
            try:
                ts = datetime.fromisoformat(updated.strip())
                oldest = ts if oldest is None or ts < oldest else oldest
            except ValueError:
                pass
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        if not (title.startswith("4 ") or title.startswith("4/A ")):
            continue
        link = entry.find(f"{_ATOM}link")
        href = link.get("href", "") if link is not None else ""
        m = re.search(r"/Archives/edgar/data/(\d+)/\d+/([\d-]+)-index\.html?$", href)
        if m:
            paths.append(f"edgar/data/{m.group(1)}/{m.group(2)}.txt")
    return paths, oldest, len(entries)
