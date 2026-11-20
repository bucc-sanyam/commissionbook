# Commission Book — Project Summary

> **Purpose of this file:** Single source of truth for new development sessions. Read this + `graphify-out/GRAPH_REPORT.md` first; do not scan the whole repo unless something here is insufficient or contradicted. Keep this file updated after every meaningful change.

---

## 1. Project Overview
**Commission Book** is an equity portfolio, trade ledger, and commission management platform built for traders and account managers. It handles multi-client trade tracking, profit-sharing commission calculations, client portfolios (cumulative holdings, average buy price, FIFO deductions), live market prices, screenshot trade parsing, and client portal access with Web Push alerts.

- **Admin Interface:** Record and close trades, manage clients, reconcile trade batches, configure custom fee structures, import screenshots/trade exports, and view candlestick charts.
- **Client Portal:** Client-facing read-only dashboard allowing clients to view their open positions, trade history, realised P&L, statement downloads, and commission owed/paid with tap-to-reveal privacy controls and push alerts.

---

## 2. Technology Stack
- **Backend Framework:** Python 3.10+ / Flask (`backend/web.py`, `app.py`)
- **Database:**
  - Local/Dev: SQLite (`commission.db`) via `sqlite3`
  - Production: Supabase (PostgreSQL) via `psycopg2` / `supabase-py`
  - Unified Database Abstraction: `backend/database.py` with automatic dialect translation (`?` vs `%s`, schemas, constraints)
- **Frontend / UI:**
  - Jinja2 server-rendered templates (`templates/`)
  - Vanilla JavaScript (`static/app.js`, `static/dashboard.js`, `static/upload.js`)
  - Charts: TradingView Lightweight Charts (`static/lightweight-charts.js`)
  - CSS: Custom responsive design (`static/style.css`) with airport departure-board (Solari flap) live number animations
- **Market Data & Prices:**
  - Real-time stock prices fetched from Yahoo Finance via `yfinance` (`backend/prices.py`, `update_prices.py`) with caching and NSE ticker normalization
- **Deployment:**
  - Serverless on Vercel (`api/index.py`, `vercel.json`)
  - Scheduled market data sync via GitHub Actions (`.github/workflows/market-hours-sync.yml`)

---

## 3. Directory Structure & Key Files

```
Commission/
├── .agents/                      # Antigravity rules and workflows (graphify)
├── .github/workflows/            # GitHub Actions (market-hours-sync.yml)
├── api/
│   └── index.py                  # Vercel serverless entry point
├── backend/
│   ├── database.py               # DB layer: SQLite & Postgres connection pooling, queries & migrations
│   ├── prices.py                 # Live market price fetching, caching, ticker aliasing (NSE)
│   ├── security.py               # Auth, session management, CSRF tokens, rate limiting, encryption
│   ├── storage.py                # Supabase Storage & local upload handling
│   ├── transfer.py               # Ledger export, CSV generation, data transfers
│   ├── validation.py             # Input sanitization and data validation
│   └── web.py                    # Core Flask application, route handlers, view rendering
├── graphify-out/                 # Knowledge graph artifact (GRAPH_REPORT.md, graph.html, graph.json)
├── static/
│   ├── app.js                    # Core frontend client interactions & hotkeys
│   ├── dashboard.js              # Admin dashboard widgets & live updates
│   ├── lightweight-charts.js     # Standalone TradingView lightweight charts library
│   ├── style.css                 # Main application stylesheet
│   ├── sw.js                     # Service Worker for Web Push notifications
│   └── upload.js                 # Drag-and-drop trade statement upload handling
├── supabase/migrations/          # Sequential SQL migrations for Supabase Postgres
├── templates/
│   ├── _macros.html              # Reusable Jinja2 UI components (tables, badges, icons, modals)
│   ├── base.html                 # App shell layout
│   ├── charts.html               # Candlestick stock charts
│   ├── client_detail.html        # Client account overview, trades, payments & statement export
│   ├── clients.html              # Clients list & KPI summary
│   ├── dashboard.html            # Admin dashboard: overview metrics, profit distribution, recent trades
│   ├── holdings.html             # Open positions, cumulative holdings & average buy prices
│   ├── portal.html               # Client-facing portal view
│   ├── trade_form.html           # Trade creation & editing form
│   ├── trades.html               # Global trade ledger & filtering
│   └── uploads.html              # Screenshot and trade import page
├── app.py                        # Local WSGI development server entry point
├── commission.db                 # SQLite local development database
├── PROJECT_SUMMARY.md            # Single source of truth session summary
├── requirements.txt              # Production Python dependencies
├── trade_parser.py               # Text & screenshot OCR trade parser
├── update_prices.py              # CLI utility to refresh cached market prices
└── vercel.json                   # Vercel routing and serverless runtime configuration
```

---

## 4. Financial & Business Logic

### Commission Rules
Supported commission models per client:
1. **Profit Share:** Percentage of positive realised profit on closed trades. Losses strictly carry 0 commission.
2. **Turnover Fee:** Percentage of total turnover (Buy Value + Sell Value) applied only to profitable closed trades.
3. **Flat Fee:** Fixed monetary fee per closed profitable trade.
4. **Override:** Optional per-trade manual fee override that takes precedence over client defaults.

### Portfolio & Holding Dynamics
- **FIFO Deductions:** When an open position is sold or partially closed, shares are deducted on a First-In-First-Out (FIFO) basis (`apply_sale_fifo`).
- **Cumulative Holdings:** Aggregated per client and ticker symbol, calculating average purchase price and unrealized P&L against live market prices.
- **Deduct from Funds (Available Cash):** Optional toggle on buy trades to subtract purchase cost directly from the client's available cash fund balance, preventing negative drift when unchecked.
- **Trade Reconciliation:** Bulk trade reconciliation module that detects duplicate entries and matches trade confirmations against open holdings before committing.

---

## 5. Security & Authentication
- **Admin Authentication:** Password protected with secure session cookies and CSRF tokens.
- **Client Portal Authentication:** Distinct client login via phone/username and bcrypt-hashed passwords. Enforces 8+ character passwords, password change tracking, and session isolation.
- **Web Push Notifications:** Implements RFC 8292 VAPID Web Push notifications (`Urgency: high`) to alert clients on newly opened trades, closed positions, or statement updates.

---

## 6. Development & Deployment Guidelines
- **Local Development:**
  ```bash
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python3 app.py
  ```
- **Live Price Updates:**
  ```bash
  python3 update_prices.py
  ```
- **Codebase Knowledge Graph:**
  ```bash
  graphify update .
  ```
- **Vercel Deployments:** Handled via git pushes to `main`. Background threading is disabled on Vercel serverless to prevent invocation timeout issues.
