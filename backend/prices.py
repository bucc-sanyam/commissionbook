import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx

logger = logging.getLogger("commission.prices")

IST = timezone(timedelta(hours=5, minutes=30))
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def sanitize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(" ", "").replace("-", "")


def fetch_stock_quote(raw_symbol: str) -> dict | None:
    """Fetch current market price and previous close from Yahoo Finance for NSE/BSE symbols."""
    clean = sanitize_symbol(raw_symbol)
    if not clean:
        return None

    # Try variants: .NS (NSE), .BO (BSE), raw
    variants = []
    if not clean.endswith(".NS") and not clean.endswith(".BO"):
        variants = [f"{clean}.NS", f"{clean}.BO", clean]
    else:
        variants = [clean]

    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

    for ticker in variants:
        for host in hosts:
            for ua in USER_AGENTS:
                headers = {"User-Agent": ua, "Accept": "application/json"}
                url = f"https://{host}/v8/finance/chart/{ticker}?interval=1d&range=1d"
                try:
                    with httpx.Client(timeout=8.0) as client:
                        resp = client.get(url, headers=headers)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                        result = data.get("chart", {}).get("result")
                        if not result or len(result) == 0:
                            continue
                        meta = result[0].get("meta", {})
                        current_price = meta.get("regularMarketPrice")
                        if current_price is None:
                            # Fallback to indicators close if regularMarketPrice is not set
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
                        change_pct = (change / prev_dec * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if prev_dec else Decimal("0")

                        return {
                            "stock": clean.replace(".NS", "").replace(".BO", ""),
                            "current_price": curr_dec,
                            "previous_close": prev_dec,
                            "change": change,
                            "change_pct": change_pct,
                        }
                except Exception as exc:
                    logger.debug("Failed to fetch price for %s: %s", ticker, exc)
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


def update_all_stock_prices(db) -> dict:
    """Fetch and update prices for all held stocks in the database."""
    stocks = get_all_held_stocks(db)
    updated = []
    failed = []

    for stock in stocks:
        quote = fetch_stock_quote(stock)
        if quote:
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
                    quote["stock"],
                    quote["current_price"],
                    quote["previous_close"],
                    quote["change"],
                    quote["change_pct"],
                ),
            )
            updated.append(quote)
        else:
            failed.append(stock)

    db.commit()
    return {
        "total": len(stocks),
        "updated": updated,
        "failed": failed,
    }


def get_cached_prices(db) -> dict[str, dict]:
    """Return dictionary of stock prices keyed by uppercase stock symbol."""
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
