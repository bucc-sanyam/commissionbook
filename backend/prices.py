import json
import logging
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("commission.prices")

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# Common Indian company names and abbreviations mapped to NSE/BSE ticker symbols
KNOWN_TICKERS: dict[str, str] = {
    "TECHNOCRAFT": "TIIL",
    "TECHNOCRAFT VENTURES": "TIIL",
    "TECHNOCRAFT INDUSTRIES": "TIIL",
    "TECHNOCRAFT INDUSTRIES INDIA": "TIIL",
    "TECHNOCRAFT INDUSTRIES (INDIA)": "TIIL",
    "TECHNOCRAFT IND": "TIIL",
    "TIIL": "TIIL",
    "SBC": "SBC",
    "SBC EXPORTS": "SBC",
    "SBC EXPORTS LIMITED": "SBC",
    "SBC EXPORTS LTD": "SBC",
    "RELIANCE": "RELIANCE",
    "RELIANCE INDUSTRIES": "RELIANCE",
    "TCS": "TCS",
    "TATA CONSULTANCY": "TCS",
    "TATA CONSULTANCY SERVICES": "TCS",
    "INFOSYS": "INFY",
    "INFY": "INFY",
    "WIPRO": "WIPRO",
    "HDFC": "HDFCBANK",
    "HDFC BANK": "HDFCBANK",
    "HDFCBANK": "HDFCBANK",
    "ICICI": "ICICIBANK",
    "ICICI BANK": "ICICIBANK",
    "ICICIBANK": "ICICIBANK",
    "SBI": "SBIN",
    "STATE BANK OF INDIA": "SBIN",
    "SBIN": "SBIN",
    "BHARTI AIRTEL": "BHARTIARTL",
    "AIRTEL": "BHARTIARTL",
    "ITC": "ITC",
    "LT": "LT",
    "LARSEN": "LT",
    "LARSEN & TOUBRO": "LT",
    "LARSEN AND TOUBRO": "LT",
    "KOTAK": "KOTAKBANK",
    "KOTAK MAHINDRA BANK": "KOTAKBANK",
    "AXIS": "AXISBANK",
    "AXIS BANK": "AXISBANK",
    "MARUTI": "MARUTI",
    "MARUTI SUZUKI": "MARUTI",
    "TATA MOTORS": "TATAMOTORS",
    "TATAMOTORS": "TATAMOTORS",
    "TATA STEEL": "TATASTEEL",
    "ASIAN PAINTS": "ASIANPAINT",
    "ASIAN PAINT": "ASIANPAINT",
    "HCL TECH": "HCLTECH",
    "HCL TECHNOLOGIES": "HCLTECH",
    "SUN PHARMA": "SUNPHARMA",
    "BAJAJ FINANCE": "BAJFINANCE",
    "BAJAJ FINSERV": "BAJAJFINSV",
    "TITAN": "TITAN",
    "TITAN COMPANY": "TITAN",
    "ADANI ENTERPRISES": "ADANIENT",
    "ADANI PORTS": "ADANIPORTS",
    "ADANI POWER": "ADANIPOWER",
    "ZOMATO": "ZOMATO",
    "PAYTM": "PAYTM",
    "ONE97": "PAYTM",
    "JIO FINANCIAL": "JIOFIN",
    "JIO FINANCIAL SERVICES": "JIOFIN",
    "IREDA": "IREDA",
    "TRENT": "TRENT",
    "BEL": "BEL",
    "BHARAT ELECTRONICS": "BEL",
    "HAL": "HAL",
    "HINDUSTAN AERONAUTICS": "HAL",
    "COAL INDIA": "COALINDIA",
    "NTPC": "NTPC",
    "ONGC": "ONGC",
    "POWER GRID": "POWERGRID",
}


def sanitize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.strip().upper())


def resolve_ticker_candidates(raw_name: str, alias_map: dict | None = None) -> list[str]:
    """Resolve a company or position name into plausible NSE/BSE ticker symbols."""
    raw = raw_name.strip().upper()
    candidates = []

    # 1. Check in-database alias mapping (if configured by admin)
    if alias_map and raw in alias_map:
        canonical = alias_map[raw]
        candidates.append(canonical)
        if canonical in KNOWN_TICKERS:
            candidates.append(KNOWN_TICKERS[canonical])

    # 2. Direct match in known tickers
    if raw in KNOWN_TICKERS:
        candidates.append(KNOWN_TICKERS[raw])

    # 3. Strip corporate suffixes (LIMITED, LTD, VENTURES, INDUSTRIES, etc.)
    stripped = re.sub(
        r"\b(LIMITED|LTD|PVT|PRIVATE|VENTURES|VENTURE|INDUSTRIES|INDUSTRY|ENTERPRISES|ENTERPRISE|HOLDINGS|HOLDING|FINANCE|CORP|CORPORATION|INDIA)\b",
        "",
        raw,
    ).strip()
    if stripped:
        if stripped in KNOWN_TICKERS:
            candidates.append(KNOWN_TICKERS[stripped])
        clean_stripped = sanitize_symbol(stripped)
        if clean_stripped:
            candidates.append(clean_stripped)

    # 4. First word if alphanumeric (e.g. 'SBC' from 'SBC EXPORTS LIMITED')
    words = raw.split()
    if words and 2 <= len(words[0]) <= 12 and words[0].isalnum():
        candidates.append(words[0])

    # 5. Raw sanitized symbol
    clean_raw = sanitize_symbol(raw)
    if clean_raw:
        candidates.append(clean_raw)

    # Deduplicate while preserving priority
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def fetch_stock_quote(raw_symbol: str, alias_map: dict | None = None) -> dict | None:
    """Fetch current market price and previous close from Yahoo Finance for NSE/BSE symbols."""
    candidates = resolve_ticker_candidates(raw_symbol, alias_map)
    headers = {"User-Agent": USER_AGENTS[0], "Accept": "application/json"}

    for candidate in candidates:
        # Build exchange ticker variants: NSE (.NS) first, then BSE (.BO)
        variants = []
        if not candidate.endswith(".NS") and not candidate.endswith(".BO"):
            variants = [f"{candidate}.NS", f"{candidate}.BO"]
        else:
            variants = [candidate]

        for ticker in variants:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=1d"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status != 200:
                        continue
                    data = json.loads(resp.read().decode("utf-8"))

                result = data.get("chart", {}).get("result")
                if not result or len(result) == 0:
                    continue

                meta = result[0].get("meta", {})
                current_price = meta.get("regularMarketPrice")
                if current_price is None:
                    indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
                    closes = [c for c in indicators.get("close", []) if c is not None]
                    if closes:
                        current_price = closes[-1]

                if current_price is None:
                    continue

                prev_close = meta.get("chartPreviousClose") or current_price
                curr_dec = Decimal(str(current_price)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                prev_dec = Decimal(str(prev_close)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                change = curr_dec - prev_dec
                change_pct = (
                    (change / prev_dec * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                    if prev_dec
                    else Decimal("0")
                )

                return {
                    "stock": raw_symbol,
                    "ticker": candidate.replace(".NS", "").replace(".BO", ""),
                    "current_price": curr_dec,
                    "previous_close": prev_dec,
                    "change": change,
                    "change_pct": change_pct,
                }
            except Exception as exc:
                logger.debug("Failed quote for candidate %s (%s): %s", candidate, ticker, exc)
                continue

    return None


def get_all_held_stocks(db) -> list[str]:
    """Retrieve all unique stock symbols currently held in client_shares or open trades."""
    query = """
        SELECT DISTINCT stock FROM (
            SELECT stock FROM client_shares WHERE stock IS NOT NULL AND stock != ''
            UNION
            SELECT stock FROM trades WHERE (sell_price IS NULL OR sell_date IS NULL) AND stock IS NOT NULL AND stock != ''
        )
    """
    rows = db.execute(query).fetchall()
    return [row["stock"].strip().upper() for row in rows if row.get("stock")]


_cached_prices_data: dict[str, dict] | None = None
_cached_prices_time: float = 0.0
_cached_prices_ttl: float = 60.0
_prices_cache_lock = threading.Lock()


def invalidate_prices_cache():
    global _cached_prices_data, _cached_prices_time
    with _prices_cache_lock:
        _cached_prices_data = None
        _cached_prices_time = 0.0


def update_all_stock_prices(db) -> dict:
    """Fetch and update prices for all held stocks in parallel, saving aliases and tickers."""
    stocks = get_all_held_stocks(db)
    if not stocks:
        return {"total": 0, "updated": [], "failed": []}

    # Load any stock aliases defined in the database
    alias_map: dict[str, str] = {}
    try:
        rows = db.execute("SELECT canonical_name, alias FROM stock_aliases").fetchall()
        for r in rows:
            c = (r["canonical_name"] or "").strip().upper()
            a = (r["alias"] or "").strip().upper()
            if a and c:
                alias_map[a] = c
    except Exception:
        pass

    # Parallel fetch using thread pool for speed (< 1s for all stocks)
    updated = []
    failed = []

    def fetch_worker(stk):
        return stk, fetch_stock_quote(stk, alias_map)

    with ThreadPoolExecutor(max_workers=min(8, len(stocks))) as executor:
        futures = [executor.submit(fetch_worker, s) for s in stocks]
        for f in as_completed(futures):
            try:
                stk, quote = f.result()
                if quote:
                    # Save under the raw stock name, the resolved ticker, and canonical name
                    symbols_to_save = {stk, quote["ticker"]}
                    if stk in alias_map:
                        symbols_to_save.add(alias_map[stk])

                    for sym in symbols_to_save:
                        if not sym:
                            continue
                        db.execute(
                            """
                            INSERT INTO stock_prices (stock, current_price, previous_close, change, change_pct, updated_at)
                            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                            ON CONFLICT(stock) DO UPDATE SET
                                current_price=excluded.current_price,
                                previous_close=excluded.previous_close,
                                change=excluded.change,
                                change_pct=excluded.change_pct,
                                updated_at=CURRENT_TIMESTAMP
                            """,
                            (
                                sym,
                                quote["current_price"],
                                quote["previous_close"],
                                quote["change"],
                                quote["change_pct"],
                            ),
                        )
                    updated.append(quote)
                else:
                    failed.append(stk)
            except Exception as e:
                logger.exception("Error processing stock quote: %s", e)
                failed.append(stk)

    db.commit()
    invalidate_prices_cache()
    return {
        "total": len(stocks),
        "updated": updated,
        "failed": failed,
    }


def get_cached_prices(db) -> dict[str, dict]:
    """Return dictionary of stock prices keyed by uppercase stock symbol (cached in memory)."""
    global _cached_prices_data, _cached_prices_time
    now = time.time()
    if _cached_prices_data is not None and (now - _cached_prices_time < _cached_prices_ttl):
        return _cached_prices_data

    with _prices_cache_lock:
        if _cached_prices_data is not None and (now - _cached_prices_time < _cached_prices_ttl):
            return _cached_prices_data
        try:
            rows = db.execute("SELECT * FROM stock_prices").fetchall()
            result = {}
            for row in rows:
                sym = row["stock"].upper()
                result[sym] = {
                    "current_price": Decimal(str(row["current_price"])),
                    "previous_close": Decimal(str(row["previous_close"])) if row.get("previous_close") is not None else None,
                    "change": Decimal(str(row["change"])) if row.get("change") is not None else Decimal("0"),
                    "change_pct": Decimal(str(row["change_pct"])) if row.get("change_pct") is not None else Decimal("0"),
                    "updated_at": row.get("updated_at"),
                }
            _cached_prices_data = result
            _cached_prices_time = now
            return result
        except Exception as exc:
            logger.warning("Could not read stock_prices: %s", exc)
            return {}


class DailyPriceScheduler:
    """Background scheduler that updates stock prices at 3:45 PM IST on market days (Mon-Fri)."""

    def __init__(self, app):
        self.app = app
        self._thread = None
        self._stop_event = threading.Event()
        self.last_run_date = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PriceScheduler")
        self._thread.start()
        logger.info("Daily stock price scheduler started (target: 15:45 IST, Mon-Fri).")

    def stop(self):
        self._stop_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                now_ist = datetime.now(IST)
                weekday = now_ist.weekday()  # 0=Monday, 4=Friday
                today_str = now_ist.strftime("%Y-%m-%d")

                # Check if Monday-Friday and time is between 15:45 and 15:55 IST
                if 0 <= weekday <= 4 and (now_ist.hour == 15 and 45 <= now_ist.minute <= 55):
                    if self.last_run_date != today_str:
                        logger.info("Scheduled 15:45 IST stock price update triggered.")
                        with self.app.app_context():
                            from .database import get_db
                            res = update_all_stock_prices(get_db())
                            logger.info(
                                "Scheduled price update completed: %d updated, %d failed.",
                                len(res["updated"]),
                                len(res["failed"]),
                            )
                        self.last_run_date = today_str
            except Exception as exc:
                logger.error("Error in price scheduler loop: %s", exc)

            # Sleep for 60 seconds before checking again
            self._stop_event.wait(60)
