#!/usr/bin/env python3
"""
Standalone End-of-Day Stock Price Updater Script
Can be run manually or scheduled via cron (e.g., at 15:45 IST on weekdays):
45 15 * * 1-5 cd /Users/bucc/Downloads/Commission && .venv/bin/python update_prices.py
"""

import sys
from datetime import datetime, timedelta, timezone

from backend.database import get_db
from backend.prices import update_all_stock_prices
from backend.web import create_app

IST = timezone(timedelta(hours=5, minutes=30))


def main():
    now_ist = datetime.now(IST)
    print(f"[{now_ist.strftime('%Y-%m-%d %H:%M:%S IST')}] Running End-of-Day Stock Price Update...")

    app = create_app()
    with app.app_context():
        db = get_db()
        results = update_all_stock_prices(db)
        total = results["total"]
        updated = results["updated"]
        failed = results["failed"]

        print(f"Total held stocks: {total}")
        print(f"Successfully updated: {len(updated)}")
        for quote in updated:
            change_sign = "+" if quote["change"] >= 0 else ""
            print(
                f"  - {quote['stock']}: ₹{quote['current_price']} "
                f"({change_sign}{quote['change']}, {change_sign}{quote['change_pct']}%)"
            )

        if failed:
            print(f"Failed to fetch {len(failed)} stocks: {', '.join(failed)}")

        print("Done!")


if __name__ == "__main__":
    main()
