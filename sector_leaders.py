#!/usr/bin/env python3
"""
NSE Mid/Small Sector Leaders -> Telegram

Large caps are excluded entirely. The universe is NIFTY MIDCAP 150 +
NIFTY SMALLCAP 250.

Data sources:
  * Universe + sector labels: NSE's published index constituent CSVs.
    (The old /api/equity-stockIndices JSON endpoint now returns 404 for
    every index, including NIFTY 50, so it is no longer used.)
  * Prices, volume and 3-month returns: Yahoo Finance, split/bonus adjusted.

Sector strength is computed FROM this universe -- stocks are grouped by
their NSE industry label and each group's median move ranks the sectors.
So "Healthcare is leading" means mid/small healthcare, not Sun Pharma.

Env vars required:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Optional env vars:
    TOP_SECTORS      (default 4)      leading sectors to expand
    TOP_STOCKS       (default 5)      stocks shown per sector
    MIN_GROUP        (default 4)      min stocks for a sector to rank
    MIN_VOLUME       (default 100000) min shares traded on the latest bar
    MIN_3M_GAIN      (default 25)     min 3-month % gain for the momentum list
    TOP_MOMENTUM     (default 10)     how many momentum names to list
    SHOW_LAGGARDS    (default 1)      also list the weakest sectors
    SKIP_IF_CLOSED   (default 1)      exit quietly when the market is closed
"""

import os
import sys
import csv
import time
import html
import statistics
import datetime as dt
from collections import defaultdict

import requests

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

TOP_SECTORS = int(os.environ.get("TOP_SECTORS", "4"))
TOP_STOCKS = int(os.environ.get("TOP_STOCKS", "5"))
MIN_GROUP = int(os.environ.get("MIN_GROUP", "4"))
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "100000"))
MIN_3M_GAIN = float(os.environ.get("MIN_3M_GAIN", "25"))
TOP_MOMENTUM = int(os.environ.get("TOP_MOMENTUM", "10"))
SHOW_LAGGARDS = os.environ.get("SHOW_LAGGARDS", "1") == "1"
SKIP_IF_CLOSED = os.environ.get("SKIP_IF_CLOSED", "1") == "1"

# Index constituent lists. Mid + small only -- no large caps.
CSV_FILES = [
    "ind_niftymidcap150list.csv",
    "ind_niftysmallcap250list.csv",
]
CSV_HOSTS = [
    "https://nsearchives.nseindia.com/content/indices/",
    "https://archives.nseindia.com/content/indices/",
]

MARKET_STATUS = "https://www.nseindia.com/api/marketStatus"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


def log(msg):
    print(f"[{dt.datetime.now(IST):%H:%M:%S}] {msg}", flush=True)


def market_is_open():
    """Best-effort holiday guard. Never blocks the run if it can't tell."""
    try:
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1)
        r = s.get(MARKET_STATUS, timeout=20)
        if r.status_code != 200:
            log(f"market status check returned HTTP {r.status_code} -- proceeding")
            return True
        for row in r.json().get("marketState", []):
            if row.get("market") == "Capital Market":
                status = (row.get("marketStatus") or "").lower()
                log(f"capital market status: {status}")
                return "close" not in status
    except Exception as e:
        log(f"market status check failed ({e}) -- proceeding")
    return True


def fetch_csv(filename):
    """Download one index constituent CSV, trying both archive hosts."""
    for host in CSV_HOSTS:
        url = host + filename
        for attempt in (1, 2):
            try:
                r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
                if r.status_code == 200 and "Symbol" in r.text[:400]:
                    return r.text
                log(f"{filename} via {host.split('/')[2]}: HTTP {r.status_code}")
            except requests.RequestException as e:
                log(f"{filename} via {host.split('/')[2]}: {e}")
            time.sleep(2)
    return None


def parse_constituents(text):
    """CSV columns: Company Name, Industry, Symbol, Series, ISIN Code."""
    rows = {}
    for row in csv.DictReader(text.splitlines()):
        sym = (row.get("Symbol") or "").strip()
        industry = (row.get("Industry") or "").strip()
        if not sym or not industry:
            continue
        rows[sym] = {
            "symbol": sym,
            "industry": industry,
            "name": (row.get("Company Name") or "").strip(),
        }
    return rows


def build_universe():
    universe = {}
    for filename in CSV_FILES:
        text = fetch_csv(filename)
        if not text:
            log(f"could not download {filename}")
            continue
        rows = parse_constituents(text)
        log(f"{filename}: {len(rows)} constituents")
        universe.update(rows)
    log(f"mid/small universe: {len(universe)} stocks")
    return universe


def fetch_prices(symbols):
    """Latest close, day move, volume and 3M return per symbol, from Yahoo."""
    try:
        import yfinance as yf
    except ImportError:
        log("ERROR: yfinance not installed")
        return {}

    out = {}
    tickers = [f"{s}.NS" for s in symbols]
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            df = yf.download(
                chunk, period="3mo", interval="1d",
                auto_adjust=True, progress=False,
                threads=True, group_by="column",
            )
        except Exception as e:
            log(f"yfinance chunk {i // 100 + 1} failed: {e}")
            continue
        if df is None or df.empty:
            continue

        try:
            close, volume = df["Close"], df["Volume"]
        except (KeyError, TypeError):
            continue
        if not hasattr(close, "columns"):
            close = close.to_frame(chunk[0])
            volume = volume.to_frame(chunk[0])

        for col in close.columns:
            c = close[col].dropna()
            if len(c) < 40:  # need a real 3 months of history
                continue
            first, last, prev = float(c.iloc[0]), float(c.iloc[-1]), float(c.iloc[-2])
            if first <= 0 or prev <= 0:
                continue
            try:
                vol = int(volume[col].dropna().iloc[-1])
            except Exception:
                vol = 0
            out[str(col).replace(".NS", "")] = {
                "price": last,
                "pct": (last / prev - 1) * 100,
                "gain3m": (last / first - 1) * 100,
                "volume": vol,
            }
        log(f"prices resolved: {len(out)}")
        time.sleep(1)
    return out


def rank_sectors(stocks):
    """Group by industry label and rank each group by its median move."""
    groups = defaultdict(list)
    for s in stocks:
        groups[s["industry"]].append(s)

    ranked = []
    for industry, rows in groups.items():
        if len(rows) < MIN_GROUP:
            continue
        moves = [r["pct"] for r in rows]
        ranked.append({
            "industry": industry,
            "median": statistics.median(moves),
            "count": len(rows),
            "up": sum(1 for m in moves if m > 0),
            "stocks": sorted(rows, key=lambda x: x["pct"], reverse=True),
        })
    ranked.sort(key=lambda x: x["median"], reverse=True)
    log(f"{len(ranked)} sectors with >= {MIN_GROUP} liquid names")
    return ranked


def arrow(pct):
    return "\U0001F7E2" if pct > 0 else ("\U0001F534" if pct < 0 else "\u26AA")


def table(headers, rows, aligns):
    """Fixed-width monospace table for Telegram <pre> blocks."""
    cols = list(zip(*([headers] + rows))) if rows else [[h] for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    out = []
    for r in [headers] + rows:
        cells = []
        for val, w, a in zip(r, widths, aligns):
            cells.append(str(val).rjust(w) if a == "r" else str(val).ljust(w))
        out.append(" ".join(cells).rstrip())
    return "\n".join(out)


def build_sections(ranked, momentum, stocks):
    """Returns a list of self-contained HTML blocks."""
    now = dt.datetime.now(IST)
    green = sum(1 for s in stocks if s["pct"] > 0)
    total = len(stocks)
    pct_green = (green / total * 100) if total else 0
    breadth = "broad" if pct_green >= 60 else ("narrow" if pct_green <= 35 else "mixed")

    sections = [
        f"<b>Mid/Small Sector Leaders</b>  {now:%d %b, %H:%M} IST\n"
        f"<i>{green}/{total} green ({pct_green:.0f}%) - {breadth} tape</i>"
    ]

    # Sector summary table
    rows = [[s["industry"][:18], f"{s['median']:+.2f}", f"{s['up']}/{s['count']}"]
            for s in ranked[:TOP_SECTORS]]
    sections.append(
        "<b>Leading sectors</b>\n<pre>"
        + html.escape(table(["SECTOR", "MED%", "UP"], rows, ["l", "r", "r"]))
        + "</pre>"
    )

    # One table of top stocks per leading sector
    for sec in ranked[:TOP_SECTORS]:
        rows = []
        for st in sec["stocks"][:TOP_STOCKS]:
            flag = "*" if st["gain3m"] >= MIN_3M_GAIN else ""
            rows.append([f"{flag}{st['symbol']}", f"{st['pct']:+.2f}",
                         f"{st['gain3m']:+.0f}", f"{st['price']:,.0f}"])
        sections.append(
            f"<b>{html.escape(sec['industry'])}</b>  {sec['median']:+.2f}%\n<pre>"
            + html.escape(table(["STOCK", "DAY%", "3M%", "LTP"], rows,
                                ["l", "r", "r", "r"]))
            + "</pre>"
        )

    tail_pool = ranked[TOP_SECTORS:]
    if SHOW_LAGGARDS and len(tail_pool) >= 2:
        rows = [[s["industry"][:18], f"{s['median']:+.2f}", f"{s['up']}/{s['count']}"]
                for s in tail_pool[-3:][::-1]]
        sections.append(
            "<b>Weakest sectors</b>\n<pre>"
            + html.escape(table(["SECTOR", "MED%", "UP"], rows, ["l", "r", "r"]))
            + "</pre>"
        )

    if momentum:
        rows = [[f"{'*' if m['hot'] else ''}{m['symbol']}",
                 f"{m['gain3m']:+.0f}", f"{m['pct']:+.2f}"] for m in momentum]
        sections.append(
            f"<b>Strongest 3M gainers (&gt;{MIN_3M_GAIN:.0f}%)</b>\n<pre>"
            + html.escape(table(["STOCK", "3M%", "DAY%"], rows, ["l", "r", "r"]))
            + "</pre>\n<i>* = in a leading sector today</i>"
        )
    else:
        sections.append(f"<i>No names cleared the {MIN_3M_GAIN:.0f}% 3M filter.</i>")

    return sections


def send_telegram(sections):
    """Sends HTML blocks, packing them into as few messages as fit."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log("ERROR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False

    if isinstance(sections, str):
        sections = [sections]

    # Never split inside a <pre> block -- pack whole sections only
    chunks, current = [], ""
    for block in sections:
        if current and len(current) + len(block) + 2 > 3800:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)

    ok = True
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, "text": chunk,
                "parse_mode": "HTML", "disable_web_page_preview": True,
            },
            timeout=25,
        )
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:300]}")
            ok = False
        time.sleep(0.5)
    if ok:
        log(f"sent to Telegram ({len(chunks)} message(s))")
    return ok


def main():
    if SKIP_IF_CLOSED and not market_is_open():
        log("market closed -- exiting without sending")
        return 0

    universe = build_universe()
    if not universe:
        send_telegram("<b>Mid/Small Sector Leaders</b>\nCould not download the "
                      "NSE constituent lists on this run. Check the Actions log.")
        return 1

    prices = fetch_prices(list(universe.keys()))
    if not prices:
        send_telegram("<b>Mid/Small Sector Leaders</b>\nCould not fetch price "
                      "data on this run. Check the Actions log.")
        return 1

    stocks = []
    for sym, meta in universe.items():
        p = prices.get(sym)
        if not p or p["volume"] < MIN_VOLUME:
            continue
        stocks.append({**meta, **p})
    log(f"liquid stocks with prices: {len(stocks)}")

    if not stocks:
        send_telegram("<b>Mid/Small Sector Leaders</b>\nNo stocks passed the "
                      "volume filter. Try lowering MIN_VOLUME.")
        return 1

    ranked = rank_sectors(stocks)
    if not ranked:
        send_telegram("<b>Mid/Small Sector Leaders</b>\nNo sector had enough "
                      "liquid names to rank. Try lowering MIN_GROUP.")
        return 1

    leading = {s["industry"] for s in ranked[:TOP_SECTORS]}
    momentum = [
        {**s, "hot": s["industry"] in leading}
        for s in stocks if s["gain3m"] >= MIN_3M_GAIN
    ]
    momentum.sort(key=lambda x: (x["hot"], x["gain3m"]), reverse=True)
    momentum = momentum[:TOP_MOMENTUM]
    log(f"momentum picks: {len(momentum)}")

    send_telegram(build_sections(ranked, momentum, stocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())
