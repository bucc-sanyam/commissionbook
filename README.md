# Commission Book

A personal bookkeeping app for tracking the stock trades of people you advise, and the commission you earn from each of them.

## Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py          # http://localhost:5050
```
The first time you open the app it asks you to set an admin password. Data is stored in `commission.db` (SQLite), and uploaded screenshots are kept in `uploads/`.

## Features
- **Trades**: username, stock, quantity, buy and sell price, buy and sell date, and commission (calculated automatically or entered by hand). Trades with no sell price are open positions, and you can close them later with one click.
- **Commission rules** for each user, with a global default: *% of profit* (nothing is charged on losing trades), *% of turnover*, or *flat per trade*.
- **Dashboard**: commission earned, received, and outstanding; each client's realised P&L and win rate; monthly chart; commission by user; top stocks. All of it can be filtered by date.
- **Users**: a page per user with a statement, payment history, a merge option for duplicate names, and a delete option.
- **Payments**: record the commission you receive (UPI, bank transfer, or cash) and see what is still outstanding.
- **Excel**: export the filtered trades together with Summary and Payments sheets, and import trades from `.xlsx` (a template is included).
- **Public upload link** (`/upload`): users enter their name, drop or paste screenshots, check the extracted rows in a spreadsheet-style table, and submit. You can require an upload code, set in Settings.

## How screenshot reading works (no AI API calls)
1. OCR runs **in the user's browser** with Tesseract.js, so it is free and has no API limits. Images are enlarged, converted to grayscale, and inverted automatically if they come from a dark-mode app.
2. `trade_parser.py` pulls BUY/SELL orders out of the text using rules built for:
   - Groww order details and order lists (`Buy · 5 shares`, `Qty 10`, `Avg. price ₹…`, `12 Sep 2024`)
   - Zerodha Kite orders (`BUY 10/10 COMPLETE`, `Avg. 1,500.00`; LTP values are ignored) and Console tradebook tables
   - P&L views (`Buy avg` / `Sell avg`), plus generic layouts
3. Buys and sells of the same stock are matched first-in, first-out into trades. The user checks and corrects the rows before saving.

Test the parser directly: `echo "…ocr text…" | .venv/bin/python trade_parser.py`
