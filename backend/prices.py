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
    "TECHNOCRAFT": "TECHNOCRAF",
    "TECHNOCRAFT VENTURES": "TECHNOCRAF",
    "TECHNOCRAFT VENTURES LIMITED": "TECHNOCRAF",
    "TECHNOCRAFT VENTURES LTD": "TECHNOCRAF",
    "TECHNOCRAF": "TECHNOCRAF",
    "TECHNOCRAFT INDUSTRIES": "TECHNOCRAF",
    "TECHNOCRAFT INDUSTRIES INDIA": "TECHNOCRAF",
    "TECHNOCRAFT INDUSTRIES (INDIA)": "TECHNOCRAF",
    "TECHNOCRAFT IND": "TECHNOCRAF",
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
    "BLS": "BLS",
    "BLS POLYMERS": "BLS",
    "BLS POLYMERS LIMITED": "BLS",
    "BLS POLYMERS LTD": "BLS",
    "BLS INTERNATIONAL": "BLS",
    "BLS INTERNATIONAL SERVICES": "BLS",
}


def sanitize_symbol(symbol: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", symbol.strip().upper())


def resolve_ticker_candidates(raw_name: str, alias_map: dict | None = None) -> list[str]:
    """Resolve a company or position name into plausible NSE/BSE ticker symbols."""
    raw = raw_name.strip().upper()

    # 1. Direct match in known tickers (fast-path: zero ambiguity)
    if raw in KNOWN_TICKERS:
        return [KNOWN_TICKERS[raw]]

    # 2. Check in-database alias mapping (if configured by admin)
    if alias_map and raw in alias_map:
        canonical = alias_map[raw]
        if canonical in KNOWN_TICKERS:
            return [KNOWN_TICKERS[canonical]]
        return [canonical]

    candidates = []

    # 3. Strip corporate suffixes (LIMITED, LTD, VENTURES, INDUSTRIES, etc.)
    stripped = re.sub(
        r"\b(LIMITED|LTD|PVT|PRIVATE|VENTURES|VENTURE|INDUSTRIES|INDUSTRY|ENTERPRISES|ENTERPRISE|HOLDINGS|HOLDING|FINANCE|CORP|CORPORATION|INDIA)\b",
        "",
        raw,
    ).strip()
    if stripped:
        if stripped in KNOWN_TICKERS:
            return [KNOWN_TICKERS[stripped]]
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
                with urllib.request.urlopen(req, timeout=1.8) as resp:
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


def is_market_hours(dt: datetime | None = None) -> bool:
    """Check if current time is within Indian stock market trading hours (Mon-Fri 09:15 to 15:45 IST)."""
    if dt is None:
        dt = datetime.now(IST)
    # Monday is 0, Friday is 4, Saturday is 5, Sunday is 6
    if dt.weekday() > 4:
        return False
    market_open = dt.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = dt.replace(hour=15, minute=45, second=0, microsecond=0)
    return market_open <= dt <= market_close


def format_ist_timestamp(val) -> str:
    """Format any timestamp (string, datetime, or timestamptz) into 'YYYY-MM-DD HH:MM:SS' in IST."""
    if not val:
        return ""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    s = str(val).strip()
    try:
        cleaned = s.replace("Z", "+00:00")
        if "T" not in cleaned and " " in cleaned:
            parts = cleaned.split(" ", 1)
            cleaned = parts[0] + "T" + parts[1]
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return s[:19]


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

    now_ist = datetime.now(IST)
    now_ist_str = now_ist.strftime("%Y-%m-%d %H:%M:%S")

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
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(stock) DO UPDATE SET
                                current_price=excluded.current_price,
                                previous_close=excluded.previous_close,
                                change=excluded.change,
                                change_pct=excluded.change_pct,
                                updated_at=excluded.updated_at
                            """,
                            (
                                sym,
                                quote["current_price"],
                                quote["previous_close"],
                                quote["change"],
                                quote["change_pct"],
                                now_ist_str,
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
                    "updated_at": format_ist_timestamp(row.get("updated_at")),
                }
            _cached_prices_data = result
            _cached_prices_time = now
            return result
        except Exception as exc:
            logger.warning("Could not read stock_prices: %s", exc)
            return {}


def get_all_ledger_stocks_summary(db) -> list[dict]:
    """Retrieve all unique stocks present in trades, client holdings, prices, or aliases with complete summary metrics."""
    alias_map = {}
    try:
        alias_rows = db.execute("SELECT alias, canonical_name FROM stock_aliases").fetchall()
        for ar in alias_rows:
            alias_map[ar["alias"].strip().upper()] = ar["canonical_name"].strip().upper()
    except Exception:
        alias_map = {}

    stocks_set = set()
    # 1. Stocks from trades
    try:
        t_rows = db.execute("SELECT DISTINCT stock FROM trades WHERE stock IS NOT NULL AND TRIM(stock) != ''").fetchall()
        for r in t_rows:
            s = (r["stock"] or "").strip().upper()
            if s:
                stocks_set.add(alias_map.get(s, s))
    except Exception:
        pass

    # 2. Stocks from client_shares
    try:
        cs_rows = db.execute("SELECT DISTINCT stock FROM client_shares WHERE stock IS NOT NULL AND TRIM(stock) != ''").fetchall()
        for r in cs_rows:
            s = (r["stock"] or "").strip().upper()
            if s:
                stocks_set.add(alias_map.get(s, s))
    except Exception:
        pass

    # 3. Stocks from stock_prices
    try:
        sp_rows = db.execute("SELECT stock FROM stock_prices WHERE stock IS NOT NULL AND TRIM(stock) != ''").fetchall()
        for r in sp_rows:
            s = (r["stock"] or "").strip().upper()
            if s:
                stocks_set.add(alias_map.get(s, s))
    except Exception:
        pass

    # Cached prices
    cached_prices = get_cached_prices(db)

    # Aggregations from client_shares
    shares_agg = {}
    try:
        agg_rows = db.execute(
            """SELECT stock, client_id, quantity, buy_price FROM client_shares WHERE quantity > 0"""
        ).fetchall()
        for r in agg_rows:
            sym = alias_map.get((r["stock"] or "").strip().upper(), (r["stock"] or "").strip().upper())
            if not sym:
                continue
            if sym not in shares_agg:
                shares_agg[sym] = {"total_qty": Decimal("0"), "clients": set(), "total_cost": Decimal("0")}
            q = Decimal(str(r["quantity"] or 0))
            bp = Decimal(str(r["buy_price"] or 0))
            shares_agg[sym]["total_qty"] += q
            shares_agg[sym]["total_cost"] += q * bp
            shares_agg[sym]["clients"].add(r["client_id"])
    except Exception:
        pass

    # Trade counts
    trades_count_map = {}
    try:
        t_count_rows = db.execute(
            """SELECT stock, COUNT(*) as c FROM trades WHERE stock IS NOT NULL GROUP BY stock"""
        ).fetchall()
        for r in t_count_rows:
            sym = alias_map.get((r["stock"] or "").strip().upper(), (r["stock"] or "").strip().upper())
            trades_count_map[sym] = trades_count_map.get(sym, 0) + r["c"]
    except Exception:
        pass

    result = []
    for sym in sorted(stocks_set):
        quote = cached_prices.get(sym, {})
        agg = shares_agg.get(sym, {"total_qty": Decimal("0"), "clients": set(), "total_cost": Decimal("0")})
        tickers = resolve_ticker_candidates(sym, alias_map)
        ticker = tickers[0] if tickers else sym

        cmp = quote.get("current_price")
        chg = quote.get("change", Decimal("0"))
        chg_pct = quote.get("change_pct", Decimal("0"))
        tot_qty = int(round(agg["total_qty"]))
        avg_bp = (agg["total_cost"] / agg["total_qty"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if agg["total_qty"] > Decimal("0") else Decimal("0")

        result.append({
            "symbol": sym,
            "ticker": ticker,
            "exchange": "NSE" if not ticker.isdigit() else "BSE",
            "current_price": cmp,
            "change": chg,
            "change_pct": chg_pct,
            "updated_at": quote.get("updated_at"),
            "total_quantity": tot_qty,
            "clients_count": len(agg["clients"]),
            "avg_buy_price": avg_bp,
            "total_cost": agg["total_cost"],
            "trades_count": trades_count_map.get(sym, 0),
        })

    # Sort so stocks with active holdings appear first, then alphabetically
    result.sort(key=lambda s: (-s["total_quantity"], s["symbol"]))
    return result


def fetch_stock_candles(raw_symbol: str, range_str: str = "1mo", interval: str = "1d", db=None) -> dict:
    """Fetch candlestick (OHLCV) chart data for a stock, falling back to deterministic synthetic candles if external provider is unreachable."""
    alias_map = {}
    if db is not None:
        try:
            alias_rows = db.execute("SELECT alias, canonical_name FROM stock_aliases").fetchall()
            for ar in alias_rows:
                alias_map[ar["alias"].strip().upper()] = ar["canonical_name"].strip().upper()
        except Exception:
            alias_map = {}

    candidates = resolve_ticker_candidates(raw_symbol, alias_map)
    headers = {"User-Agent": USER_AGENTS[0], "Accept": "application/json"}

    valid_ranges = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"}
    if range_str not in valid_ranges:
        range_str = "1mo"

    valid_intervals = {"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}
    if interval not in valid_intervals:
        interval = "1d"

    # Attempt live query from Yahoo Finance
    for candidate in candidates:
        variants = []
        if not candidate.endswith(".NS") and not candidate.endswith(".BO"):
            variants = [f"{candidate}.NS", f"{candidate}.BO"]
        else:
            variants = [candidate]

        for ticker in variants:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={range_str}"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=2.5) as resp:
                    if resp.status != 200:
                        continue
                    data = json.loads(resp.read().decode("utf-8"))

                result = data.get("chart", {}).get("result")
                if not result or len(result) == 0:
                    continue

                res = result[0]
                timestamps = res.get("timestamp", [])
                quote_block = res.get("indicators", {}).get("quote", [{}])[0]
                opens = quote_block.get("open", [])
                highs = quote_block.get("high", [])
                lows = quote_block.get("low", [])
                closes = quote_block.get("close", [])
                volumes = quote_block.get("volume", [0] * len(timestamps))

                raw_candles = []
                for i, ts in enumerate(timestamps):
                    if i >= len(opens) or i >= len(closes):
                        break
                    o, h, l, c = opens[i], highs[i], lows[i], closes[i]
                    if o is None or h is None or l is None or c is None:
                        continue
                    v = volumes[i] if i < len(volumes) and volumes[i] is not None else 0
                    raw_candles.append({
                        "time": int(ts),
                        "open": float(round(Decimal(str(o)), 2)),
                        "high": float(round(Decimal(str(h)), 2)),
                        "low": float(round(Decimal(str(l)), 2)),
                        "close": float(round(Decimal(str(c)), 2)),
                        "volume": int(v),
                    })

                # Deduplicate and sort strictly ascending by timestamp for Lightweight Charts
                seen_ts = set()
                candles = []
                for cd in sorted(raw_candles, key=lambda x: x["time"]):
                    if cd["time"] not in seen_ts:
                        seen_ts.add(cd["time"])
                        candles.append(cd)

                if candles:
                    clean_ticker = candidate.replace(".NS", "").replace(".BO", "")
                    return {
                        "status": "ok",
                        "stock": raw_symbol,
                        "ticker": clean_ticker,
                        "exchange": "BSE" if candidate.endswith(".BO") else "NSE",
                        "range": range_str,
                        "interval": interval,
                        "candles": candles,
                        "source": "live",
                    }
            except Exception as exc:
                logger.debug("Candle fetch error for %s (%s): %s", candidate, ticker, exc)
                continue

    # Fallback to realistic deterministic candles based on known stock prices or trade history
    base_price = 100.0
    if db is not None:
        try:
            sp = db.execute("SELECT current_price, previous_close FROM stock_prices WHERE UPPER(TRIM(stock))=?", (raw_symbol.strip().upper(),)).fetchone()
            if sp and sp["current_price"]:
                base_price = float(sp["current_price"])
            else:
                tp = db.execute("SELECT buy_price, sell_price FROM trades WHERE UPPER(TRIM(stock))=? AND (buy_price > 0 OR sell_price > 0) ORDER BY id DESC LIMIT 1", (raw_symbol.strip().upper(),)).fetchone()
                if tp:
                    base_price = float(tp["buy_price"] or tp["sell_price"] or 100.0)
        except Exception:
            pass

    range_days = {
        "1d": 1,
        "5d": 5,
        "1mo": 30,
        "3mo": 90,
        "6mo": 180,
        "1y": 365,
        "2y": 730,
        "5y": 1825,
        "max": 1825,
    }.get(range_str, 30)

    import random
    seed_val = int(base_price * 100) + sum(ord(c) for c in raw_symbol)
    rng = random.Random(seed_val)

    now_dt = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    trading_days = []
    curr = now_dt
    target_count = min(range_days, 250 if range_str in ("1y", "2y", "5y", "max") else range_days)
    while len(trading_days) < target_count:
        if curr.weekday() < 5:
            trading_days.append(curr)
        curr -= timedelta(days=1)
    trading_days.reverse()

    # Walk backwards to derive starting price
    current_walk = base_price * (0.90 + rng.random() * 0.15)
    fallback_candles = []
    for day in trading_days:
        daily_drift = (rng.random() - 0.48) * 0.025 * current_walk
        o = round(current_walk, 2)
        c = round(max(0.5, current_walk + daily_drift), 2)
        spread = abs(c - o)
        h = round(max(o, c) + rng.random() * max(spread * 0.8, current_walk * 0.012), 2)
        l = round(max(0.2, min(o, c) - rng.random() * max(spread * 0.8, current_walk * 0.012)), 2)
        v = int(rng.randint(25000, 350000))
        fallback_candles.append({
            "time": int(day.timestamp()),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })
        current_walk = c

    # Ensure the last candle close matches current_price if known
    if fallback_candles and base_price > 0:
        fallback_candles[-1]["close"] = round(base_price, 2)
        if fallback_candles[-1]["close"] > fallback_candles[-1]["high"]:
            fallback_candles[-1]["high"] = fallback_candles[-1]["close"]
        if fallback_candles[-1]["close"] < fallback_candles[-1]["low"]:
            fallback_candles[-1]["low"] = fallback_candles[-1]["close"]

    chosen_ticker = candidates[0] if candidates else raw_symbol
    return {
        "status": "ok",
        "stock": raw_symbol,
        "ticker": chosen_ticker,
        "exchange": "NSE" if not chosen_ticker.isdigit() else "BSE",
        "range": range_str,
        "interval": interval,
        "candles": fallback_candles,
        "source": "model",
    }


class DailyPriceScheduler:
    """Background scheduler that updates stock prices every 1 minute during Indian market hours
    (09:15 to 15:45 IST, Mon-Fri) and captures final settlement prices at 15:45 IST."""

    def __init__(self, app):
        self.app = app
        self._thread = None
        self._stop_event = threading.Event()
        self.last_run_time = 0.0

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="PriceScheduler")
        self._thread.start()
        logger.info("Stock price scheduler started (20-sec live updates during 09:15-15:45 IST, Mon-Fri).")

    def stop(self):
        self._stop_event.set()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                now_ist = datetime.now(IST)
                now_ts = time.time()
                # Run every 20 seconds if within Indian market hours (09:15 - 15:45 IST, Mon-Fri)
                if is_market_hours(now_ist):
                    if now_ts - self.last_run_time >= 18.0:
                        with self.app.app_context():
                            from .database import get_db
                            res = update_all_stock_prices(get_db())
                            self.last_run_time = now_ts
                            logger.info(
                                "Live 20-sec market price update: %d updated, %d failed at %s IST.",
                                len(res.get("updated", [])),
                                len(res.get("failed", [])),
                                now_ist.strftime("%H:%M:%S"),
                            )
            except Exception as exc:
                logger.error("Error in price scheduler loop: %s", exc)

            # Sleep 5 seconds before checking again
            self._stop_event.wait(5)
