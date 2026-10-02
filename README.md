# Insider Radar

Finds **meaningful open-market stock purchases by C-suite executives** (CEO, CFO, COO, President, CTO and other "Chief … Officers") in SEC Form 4 filings, and scores each one from 0 to 100.

* **iPhone app:** a home-screen web app, refreshed twice every weekday by GitHub (free). See **[SETUP.md](SETUP.md)**.
* **E-mail alerts:** sent when a new buy scores 70 or higher.
* **Backtest:** checks whether high scores actually beat the S&P 500 in the past.
* **Desktop mode:** double-click `Start (Mac).command` or `Start (Windows).bat` to run scans on your own computer. Needs Python 3.9+.

## Congress trades (separate tab, not scored)

The app also has a **Congress** tab. It lists stock and stock-option trades that members of Congress disclosed in the last 90 days under the STOCK Act (Periodic Transaction Reports).

* **Sources:** the House Clerk's disclosure index plus each report's PDF (read with `pypdf`), and the Senate's electronic disclosure site (efdsearch.senate.gov). Members, parties, leadership and committees come from the public-domain congress-legislators data set.
* **What's kept:** stock (`[ST]`) and stock-option (`[OP]`) trades that have a ticker. Funds, bonds, crypto and paper (scanned) filings are skipped.
* **Tags, never a score:**
  * Leadership role, and committee chair or ranking member.
  * **Oversight overlap:** one of their committees oversees the company's industry. This is a rough match between committee names and SEC industry codes.
  * Spouse, joint or child ownership.
  * Late disclosure: more than 45 days after the trade.
  * **C-suite buying too:** the stock also appears in the insider scan.
* **Default filter:** reported amount of $15,001 or more. You can change it, along with chamber, party, buy/sell and the tags.
* **If one chamber's site can't be reached** on a run, that chamber's last good data is kept and the app says so.

## What counts

* Only **transaction code P**, an open-market buy with the insider's own money. Grants, option exercises, gifts and sales are excluded.
* **Amended filings (4/A)** replace the original filing they correct. Several lots in one filing count as one buy.

## The score

Each factor earns points. The score is points earned ÷ points available × 100. Factors with no data are left out rather than counted as zero.

| Factor | Weight | Full points when… |
|---|---|---|
| Dollars invested | 20 | $5M+ (log scale from $25K) |
| Stake increase | 15 | Total stake (direct + trusts/family) grows 100%+ |
| Size vs. market cap | 15 | The trade is ≥ 0.1% of the company |
| Vs. their past buys | 10 | First buy in 3 years (especially after selling), or bigger than any earlier buy |
| One-off, not routine | 10 | The insider has no fixed yearly trading pattern (Cohen, Malloy & Pomorski 2012) |
| Vs. CEO's annual pay | 10 | The buy is ≥ 1× the CEO's total pay. Pay comes from the proxy's pay-versus-performance table; CEOs only |
| Other insiders buying | 8 | 2+ other insiders bought in the same window |
| Net insider buying (90 days) | 7 | Others bought and nobody sold. Heavy selling by others lowers it |
| Seniority | 5 | CEO 5, CFO 4.5, COO/President 4, CTO 3.5 … |
| Bought after a drop | 5 | 50%+ below the 52-week high |
| Size vs. trading volume | 5 | ≥ 2 days of normal dollar volume |
| Late in the quarter | 5 | 60+ days after the last results. Early in the quarter scores 40% |
| Their past buys' record | 5 | Their earlier buys of this stock beat the S&P 500 by 20%+ over 6 months |

**Multipliers:** pre-scheduled 10b5-1 plan buys ×0.75; executives in the job under 6 months ×0.85.
**Labels:** 70+ Very strong · 50+ Strong · 30+ Notable · below 30 Minor.
**Flags shown but not scored:** late filing, amended filing, and red flags found in the latest 10-Q/10-K (going-concern doubt, material weakness, restructuring, impairment, covenant problems, exchange listing deficiency).
**Company snapshot:** revenue and year-over-year growth, net income over the last 4 quarters, cash, debt, and market cap at the trade and today.

Change the weights in `config/weights.json`. The Backtest tab suggests weights based on what predicted returns.

## Data sources (all free)

* SEC EDGAR: daily Form 4 indexes, the live filings feed, company submissions, XBRL company facts, 10-Q/10-K and proxy (DEF 14A) documents.
* SEC quarterly **Insider Transactions Data Sets**, used for 4 years of insider history (and more for the backtest).
* Yahoo Finance daily prices (best effort; the app works without them).

The app follows the SEC's fair-access rules: a contact e-mail in every request and at most 8 requests per second.

## Files

| Path | What it is |
|---|---|
| `docs/` | The app (web page, iPhone icon, offline support). Published by GitHub Pages |
| `scan.py` | Daily scan and e-mail alerts (run by GitHub) |
| `run_backtest.py` | Monthly backtest |
| `insider_radar.py` | Desktop mode (local server with a "Scan now" button) |
| `radar/` | The engine: SEC client, Form 4 parser, history database, signals, scoring, alerts, Congress feed (`congress.py`) |
| `config/weights.json` | Scoring weights |
| `.github/workflows/insider-radar.yml` | The schedule (a copy is in `setup/`) |

*For research and education. This is not investment advice.*
