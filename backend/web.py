import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.request
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from functools import wraps
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import click
try:
    import psycopg
    PsycopgError = (psycopg.Error,)
except (ImportError, Exception):
    psycopg = None
    PsycopgError = ()
from flask import (
    Flask, abort, current_app, flash, g, jsonify, redirect, render_template,
    request, send_file, send_from_directory, session, url_for,
)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils.exceptions import InvalidFileException
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from trade_parser import parse_text
from .database import get_db, install_database
from .prices import (
    DailyPriceScheduler, fetch_stock_quote, get_all_held_stocks,
    get_cached_prices, invalidate_prices_cache, is_market_hours,
    update_all_stock_prices, IST, get_all_ledger_stocks_summary,
    fetch_stock_candles, KNOWN_TICKERS,
)
from .security import (
    configure_app, credential_version, csrf_token, local_setup_allowed, require_csrf,
    safe_redirect_target, secure_response,
)
from .storage import (
    DOWNLOAD_TTL, IMAGE_TYPES, MAX_CLOUD_IMAGE, MAX_LOCAL_IMAGE, UPLOAD_TTL,
    StorageUnavailable, get_storage, safe_path, validate_image,
)
from .validation import (
    COMMISSION_TYPES, MAX_ROWS, MAX_TEXT, ValidationError, client_name,
    identifier, number, rule_values, submission_uuid, text, trade_values, valid_date,
)


ZERO = Decimal("0")
CENT = Decimal("0.01")
TRADE_SELECT = """
SELECT t.*, c.name AS client_name, c.commission_type, c.commission_rate, u.filename AS upload_filename
FROM trades t JOIN clients c ON c.id=t.client_id
LEFT JOIN uploads u ON u.id=t.upload_id
"""
EXPORT_COLS = [
    ("Username", "client_name"), ("Stock", "stock"), ("Quantity", "quantity"),
    ("Buy Price", "buy_price"), ("Sell Price", "sell_price"), ("Buy Date", "buy_date"),
    ("Sell Date", "sell_date"), ("Status", "status"), ("Invested", "invested"),
    ("P&L", "pnl"), ("P&L %", "pnl_pct"), ("Commission", "commission"),
    ("Holding Days", "holding_days"), ("Source", "source"), ("Notes", "notes"),
]


_settings_cache = None
_settings_cache_time = 0.0
_settings_cache_ttl = 300.0  # 5 minutes
_settings_lock = threading.Lock()


def settings_values():
    global _settings_cache, _settings_cache_time
    if "settings_values" in g:
        return g.settings_values
    now = time.time()
    if _settings_cache is not None and (now - _settings_cache_time < _settings_cache_ttl):
        g.settings_values = dict(_settings_cache)
        return g.settings_values
    with _settings_lock:
        if _settings_cache is not None and (now - _settings_cache_time < _settings_cache_ttl):
            g.settings_values = dict(_settings_cache)
            return g.settings_values
        try:
            _settings_cache = {
                row["key"]: row["value"] for row in get_db().execute("SELECT key,value FROM settings").fetchall()
            }
            _settings_cache_time = now
        except Exception:
            _settings_cache = {}
        g.settings_values = dict(_settings_cache)
        return g.settings_values


def setting(key, default=None):
    return settings_values().get(key, default)


def set_setting(key, value):
    global _settings_cache, _settings_cache_time
    get_db().execute(
        "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    with _settings_lock:
        if _settings_cache is not None:
            _settings_cache[key] = value
        _settings_cache_time = time.time()
    settings_values()[key] = value


def parse_utc_datetime(val):
    """Parse any datetime, string, or timestamp into a timezone-aware UTC datetime."""
    if not val:
        return None
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val.astimezone(timezone.utc)
    s = str(val).strip()
    if not s:
        return None
    try:
        cleaned = s.replace("Z", "+00:00")
        if "T" not in cleaned and " " in cleaned:
            parts = cleaned.split(" ", 1)
            cleaned = parts[0] + "T" + parts[1]
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return None


def format_ist(val, fmt="chip") -> str:
    """Format UTC timestamp to IST in various formats:
    - chip: '09:20 AM' if today, '15 Oct, 09:20 AM' if other day
    - time: '09:20 AM'
    - short: '15 Oct, 09:20 AM'
    - date_time: '15 Oct 2026, 09:20 AM'
    - full: '15 Oct 2026, 09:20:23 AM IST'
    """
    dt_utc = parse_utc_datetime(val)
    if not dt_utc:
        return "" if not val else str(val)[:16]
    dt_ist = dt_utc.astimezone(IST)
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    if fmt == "time":
        return dt_ist.strftime("%I:%M %p")
    elif fmt == "chip":
        if dt_ist.date() == now_ist.date():
            return dt_ist.strftime("%I:%M %p")
        return dt_ist.strftime("%d %b, %I:%M %p")
    elif fmt == "date_time":
        return dt_ist.strftime("%d %b %Y, %I:%M %p")
    elif fmt == "short":
        return dt_ist.strftime("%d %b, %I:%M %p")
    elif fmt == "iso":
        return dt_ist.strftime("%Y-%m-%d %H:%M")
    return dt_ist.strftime("%d %b %Y, %I:%M:%S %p IST")


def expire_old_alerts(db):
    """Mark alerts older than 24 hours as inactive (active=0)."""
    try:
        if getattr(db, "postgres", False):
            db.execute("UPDATE client_alerts SET active=0 WHERE active=1 AND created_at < NOW() - INTERVAL '1 day'")
        else:
            db.execute("UPDATE client_alerts SET active=0 WHERE active=1 AND datetime(created_at) < datetime('now', '-1 day')")
        db.commit()
    except Exception as exc:
        current_app.logger.warning("expire_old_alerts notice: %s", exc)


def shorten_url(long_url):
    """Call the free is.gd API to get a short URL. Returns None on any failure."""
    try:
        api = "https://is.gd/create.php?format=simple&url=" + urllib.parse.quote(long_url, safe="")
        with urllib.request.urlopen(api, timeout=4) as resp:
            short = resp.read().decode().strip()
        if short.startswith("https://is.gd/") or short.startswith("http://is.gd/"):
            return short
    except Exception:
        pass
    return None


def portal_token():
    token = setting("upload_token")
    if not token:
        new_token = secrets.token_urlsafe(32)
        get_db().execute(
            "INSERT INTO settings(key,value) VALUES ('upload_token',?) ON CONFLICT(key) DO NOTHING",
            (new_token,),
        )
        g.pop("settings_values", None)
        token = setting("upload_token")
        # Generate a short URL (build directly to avoid circular portal_url call)
        try:
            base = current_app.config["PUBLIC_BASE_URL"]
            long = (base + url_for("public_upload", token=token)) if base else url_for("public_upload", token=token, _external=True)
            short = shorten_url(long)
            if short:
                set_setting("upload_short_url", short)
        except Exception:
            pass
    return token


def portal_url():
    token = portal_token()
    base = current_app.config["PUBLIC_BASE_URL"]
    if base:
        return base + url_for("public_upload", token=token)
    return url_for("public_upload", token=token, _external=True)


def admin_credentials():
    configured = current_app.config["ADMIN_PASSWORD_HASH"]
    if configured:
        return configured, current_app.config["ADMIN_AUTH_VERSION"]
    stored = setting("admin_password", "")
    return stored, credential_version(current_app.secret_key, "hash:" + stored) if stored else ""


def login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            if session.get("client_id"):
                return redirect(url_for("client_portal"))
            if request.path.startswith("/api/"):
                abort(401, description="Administrator sign-in is required.")
            return redirect(url_for("login", next=request.path))
        return function(*args, **kwargs)
    return wrapper


def client_login_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("client_id"):
            if request.path.startswith("/api/"):
                abort(401, description="Client sign-in is required.")
            return redirect(url_for("login", next=request.path))
        return function(*args, **kwargs)
    return wrapper


def dec(value):
    return Decimal(str(value)) if value is not None else ZERO


def effective_rule(client):
    ctype = client["commission_type"] or setting("default_commission_type", "profit_pct")
    rate = client["commission_rate"]
    return ctype, dec(rate if rate is not None else setting("default_commission_rate", "10"))


_stock_aliases_ensured = False
_stock_aliases_cache = None  # in-memory dict: UPPER(alias) -> UPPER(canonical)
_stock_aliases_lock = threading.Lock()
_stock_suggestions_cache = None
_stock_suggestions_time = 0
_stock_suggestions_ttl = 300  # 5 minutes
_known_stocks_cache = None
_known_stocks_time = 0.0
_known_stocks_ttl = 300.0  # 5 minutes
_known_stocks_lock = threading.Lock()
_active_clients_cache = None
_active_clients_time = 0.0
_client_phones_cleaned = False


def ensure_stock_aliases_table(db):
    global _stock_aliases_ensured
    if _stock_aliases_ensured:
        return
    try:
        if getattr(db, "postgres", False):
            db.execute("""
                CREATE TABLE IF NOT EXISTS public.stock_aliases (
                    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    alias TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_stock_aliases_canonical ON public.stock_aliases(canonical_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_stock_aliases_alias ON public.stock_aliases(alias)")
        else:
            db.execute("""
                CREATE TABLE IF NOT EXISTS stock_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_name TEXT NOT NULL,
                    alias TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_stock_aliases_canonical ON stock_aliases(canonical_name)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_stock_aliases_alias ON stock_aliases(alias)")
        _stock_aliases_ensured = True
    except Exception as exc:
        current_app.logger.warning("ensure_stock_aliases_table: %s", exc)


def invalidate_stock_caches():
    global _stock_aliases_cache, _stock_suggestions_cache, _stock_suggestions_time, _known_stocks_cache, _known_stocks_time
    with _stock_aliases_lock:
        _stock_aliases_cache = None
        _stock_suggestions_cache = None
        _stock_suggestions_time = 0
    with _known_stocks_lock:
        _known_stocks_cache = None
        _known_stocks_time = 0.0


def get_alias_mapping(db):
    global _stock_aliases_cache
    if _stock_aliases_cache is not None:
        return _stock_aliases_cache
    with _stock_aliases_lock:
        if _stock_aliases_cache is not None:
            return _stock_aliases_cache
        ensure_stock_aliases_table(db)
        mapping = {}
        try:
            rows = db.execute("SELECT canonical_name, alias FROM stock_aliases").fetchall()
            for r in rows:
                c = (r["canonical_name"] or "").strip().upper()
                a = (r["alias"] or "").strip().upper()
                if c:
                    mapping[c] = c
                if a and c:
                    mapping[a] = c
        except Exception:
            pass
        _stock_aliases_cache = mapping
        return _stock_aliases_cache


def resolve_canonical_stock(db, raw_stock):
    if not raw_stock:
        return raw_stock
    cleaned = raw_stock.strip().upper()
    mapping = get_alias_mapping(db)
    return mapping.get(cleaned, cleaned)


_AMBIGUOUS_KNOWN_TICKER_KEYS = frozenset({
    "BLS POLYMERS", "BLS POLYMERS LIMITED", "BLS POLYMERS LTD",
    "BLS INTERNATIONAL", "BLS INTERNATIONAL SERVICES",
})


def get_matching_stock_variants(db, raw_stock, include_known_tickers=False):
    """Return (canonical_stock, set_of_upper_trimmed_variants) for a stock symbol."""
    raw_upper = (raw_stock or "").strip().upper()
    if not raw_upper:
        return "", set()
    alias_map = get_alias_mapping(db)
    canonical = alias_map.get(raw_upper, raw_upper)
    variants = {raw_upper, canonical}
    for alias_key, target_canon in alias_map.items():
        if target_canon == canonical:
            variants.add(alias_key)

    if include_known_tickers and raw_upper not in _AMBIGUOUS_KNOWN_TICKER_KEYS:
        ticker = KNOWN_TICKERS.get(raw_upper) or KNOWN_TICKERS.get(canonical)
        if ticker:
            for k, t in KNOWN_TICKERS.items():
                if t != ticker or k in _AMBIGUOUS_KNOWN_TICKER_KEYS:
                    continue
                mapped = alias_map.get(k)
                if mapped is None or mapped == k or mapped == canonical:
                    variants.add(k)
            mapped_t = alias_map.get(ticker)
            if mapped_t is None or mapped_t == ticker or mapped_t == canonical:
                variants.add(ticker)

    return canonical, variants


def _stock_in_clause(variants, column="stock"):
    """Build SQL IN clause and ordered parameter tuple for case/whitespace-insensitive stock matching."""
    ordered = tuple(sorted(v for v in variants if v))
    if not ordered:
        return "1=0", ()
    placeholders = ",".join("?" for _ in ordered)
    return f"UPPER(TRIM({column})) IN ({placeholders})", ordered


def _safe_sell_date(sell_date, lot_buy_date=None):
    """Ensure sell_date is valid and never precedes buy_date (required by Postgres trades_date_order)."""
    s_date = str(sell_date)[:10] if sell_date else date.today().isoformat()
    if lot_buy_date:
        b_date = str(lot_buy_date)[:10]
        if len(b_date) == 10 and s_date < b_date:
            return b_date
    return s_date


def get_known_stocks(db):
    global _known_stocks_cache, _known_stocks_time
    now = time.time()
    if _known_stocks_cache is not None and (now - _known_stocks_time < _known_stocks_ttl):
        return _known_stocks_cache

    with _known_stocks_lock:
        if _known_stocks_cache is not None and (now - _known_stocks_time < _known_stocks_ttl):
            return _known_stocks_cache
        ensure_stock_aliases_table(db)
        mapping = {}
        try:
            rows = db.execute("SELECT canonical_name, alias FROM stock_aliases ORDER BY canonical_name ASC, alias ASC").fetchall()
            for r in rows:
                c = (r["canonical_name"] or "").strip().upper()
                a = (r["alias"] or "").strip().upper()
                if c not in mapping:
                    mapping[c] = set()
                if a and a != c:
                    mapping[c].add(a)
        except Exception:
            pass

        result = []
        for c in sorted(mapping.keys()):
            alias_list = sorted(list(mapping[c]))
            result.append({
                "canonical_name": c,
                "aliases": ", ".join(alias_list),
                "alias_list": alias_list,
            })
        _known_stocks_cache = result
        _known_stocks_time = now
        return _known_stocks_cache


def get_client_entered_stocks(db):
    """Returns all distinct stock names found in trades, client holdings, and aliases with status metadata."""
    ensure_stock_aliases_table(db)
    all_stocks = set()
    try:
        rows = db.execute("SELECT DISTINCT stock FROM trades WHERE stock IS NOT NULL AND TRIM(stock) != ''").fetchall()
        for r in rows:
            s = r["stock"].strip().upper()
            if s:
                all_stocks.add(s)
    except Exception:
        pass

    try:
        rows = db.execute("SELECT DISTINCT stock FROM client_shares WHERE stock IS NOT NULL AND TRIM(stock) != ''").fetchall()
        for r in rows:
            s = r["stock"].strip().upper()
            if s:
                all_stocks.add(s)
    except Exception:
        pass

    try:
        rows = db.execute("SELECT DISTINCT canonical_name, alias FROM stock_aliases").fetchall()
        for r in rows:
            if r["canonical_name"]:
                all_stocks.add(r["canonical_name"].strip().upper())
            if r["alias"]:
                all_stocks.add(r["alias"].strip().upper())
    except Exception:
        pass

    # Map alias -> canonical_name and track canonical stocks
    alias_to_canonical = {}
    canonical_set = set()
    try:
        rows = db.execute("SELECT canonical_name, alias FROM stock_aliases").fetchall()
        for r in rows:
            c = r["canonical_name"].strip().upper()
            a = r["alias"].strip().upper()
            canonical_set.add(c)
            if a and a != c:
                alias_to_canonical[a] = c
    except Exception:
        pass

    stocks_list = []
    for s in sorted(all_stocks):
        mapped = alias_to_canonical.get(s)
        is_canonical = (s in canonical_set and mapped is None)
        stocks_list.append({
            "stock": s,
            "is_canonical": is_canonical,
            "mapped_to": mapped,
        })
    return stocks_list


POPULAR_STOCKS = [
    "ADANIENT", "ADANIPORTS", "ADANIGREEN", "ADANIPOWER", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHEL", "BHARTIARTL", "BPCL",
    "CIPLA", "COALINDIA", "CHOLAFIN",
    "DABUR", "DIVISLAB", "DLF", "DRREDDY",
    "EICHERMOT",
    "FEDERALBNK",
    "GAIL", "GODREJCP", "GRASIM",
    "HAL", "HAVELLS", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR",
    "ICICIBANK", "IDEA", "INDIGO", "INDUSINDBK", "INFY", "IOC", "IRFC", "ITC",
    "JINDALSTEL", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "KPITTECH",
    "LT", "LTIM",
    "M&M", "MARUTI", "MOTHERSON",
    "NAUKRI", "NESTLEIND", "NHPC", "NTPC",
    "ONGC",
    "PAYTM", "PFC", "PIDILITIND", "PNB", "POWERGRID",
    "REC", "RELIANCE", "RVNL",
    "SAIL", "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SIEMENS", "SUNPHARMA", "SUZLON",
    "TATAMOTORS", "TATAPOWER", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "UPL",
    "VBL", "VEDL", "VOLTAS",
    "WIPRO",
    "YESBANK",
    "ZEEL", "ZOMATO"
]


def cleanup_stock_aliases_table(db):
    """Ensure that only main canonical stock names stay in stock_aliases, removing old variant aliases and updating ledger/holdings."""
    try:
        ensure_stock_aliases_table(db)
        rows = db.execute("SELECT DISTINCT canonical_name FROM stock_aliases WHERE canonical_name IS NOT NULL AND TRIM(canonical_name) != ''").fetchall()
        for r in rows:
            c = r["canonical_name"].strip().upper()
            if c:
                # Find all child aliases that are different from canonical
                child_aliases = db.execute("SELECT alias FROM stock_aliases WHERE canonical_name=? AND UPPER(TRIM(alias)) != ?", (c, c)).fetchall()
                for ca in child_aliases:
                    alias_name = ca["alias"].strip().upper()
                    if alias_name and alias_name != c:
                        db.execute("UPDATE trades SET stock=? WHERE UPPER(TRIM(stock))=?", (c, alias_name))
                        db.execute("UPDATE client_shares SET stock=? WHERE UPPER(TRIM(stock))=?", (c, alias_name))
                        db.execute("UPDATE client_alerts SET stock=? WHERE UPPER(TRIM(stock))=?", (c, alias_name))
                        try:
                            db.execute("DELETE FROM stock_prices WHERE UPPER(TRIM(stock))=?", (alias_name,))
                        except Exception:
                            pass
                # Delete all child alias rows so only the main name stays
                db.execute("DELETE FROM stock_aliases WHERE canonical_name=? AND UPPER(TRIM(alias)) != ?", (c, c))
                # Ensure main canonical name exists as (canonical, canonical)
                db.execute(
                    """INSERT INTO stock_aliases (canonical_name, alias) VALUES (?, ?)
                       ON CONFLICT(alias) DO UPDATE SET canonical_name=excluded.canonical_name""",
                    (c, c)
                )
        invalidate_stock_caches()
    except Exception as exc:
        current_app.logger.warning("cleanup_stock_aliases_table error: %s", exc)


def get_stock_suggestions(db):
    """Returns a list of deduplicated main stock suggestion dicts (cached in memory for speed)."""
    global _stock_suggestions_cache, _stock_suggestions_time
    now = time.time()
    if _stock_suggestions_cache is not None and (now - _stock_suggestions_time < _stock_suggestions_ttl):
        return _stock_suggestions_cache

    alias_map = get_alias_mapping(db)
    items_map = {}

    # 1. Configured canonical stocks from stock_aliases
    try:
        rows = db.execute("SELECT DISTINCT canonical_name FROM stock_aliases WHERE canonical_name IS NOT NULL AND TRIM(canonical_name) != '' ORDER BY canonical_name ASC").fetchall()
        for r in rows:
            c = (r["canonical_name"] or "").strip().upper()
            if c and c not in items_map:
                items_map[c] = {"symbol": c, "canonical": c}
    except Exception:
        pass

    # 2. Distinct stocks from trades, client_shares, and stock_prices in a single UNION query
    try:
        rows = db.execute(
            """
            SELECT DISTINCT stock FROM (
                SELECT stock FROM trades WHERE stock IS NOT NULL AND TRIM(stock) != ''
                UNION
                SELECT stock FROM client_shares WHERE stock IS NOT NULL AND TRIM(stock) != ''
                UNION
                SELECT stock FROM stock_prices WHERE stock IS NOT NULL AND TRIM(stock) != ''
            ) u
            """
        ).fetchall()
        for r in rows:
            raw_s = (r["stock"] or "").strip().upper()
            canon = alias_map.get(raw_s, raw_s)
            if canon and canon not in items_map:
                items_map[canon] = {"symbol": canon, "canonical": canon}
    except Exception:
        pass

    # 3. Common market stocks (resolved to canonical name in memory)
    for sym in POPULAR_STOCKS:
        canon = alias_map.get(sym, sym)
        if canon not in items_map:
            items_map[canon] = {"symbol": canon, "canonical": canon}

    res = sorted(items_map.values(), key=lambda x: x["symbol"])
    _stock_suggestions_cache = res
    _stock_suggestions_time = now
    return res


def clean_phone_number(raw_phone):
    """Strip any +91, 91, or 0 country codes and keep only the 10-digit mobile number."""
    if not raw_phone:
        return ""
    s = str(raw_phone).strip()
    if s.startswith("+91"):
        s = s[3:]
    elif s.startswith("+"):
        s = s[1:]
    digits = re.sub(r"\D", "", s)
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:]
    while digits.startswith("0") and len(digits) > 10:
        digits = digits[1:]
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def cleanup_existing_client_phones(db):
    """Ensure any client phones with +91 prefixes in SQLite/Supabase are normalized to 10 digits."""
    try:
        rows = db.execute("SELECT id, phone FROM clients WHERE phone IS NOT NULL AND phone != ''").fetchall()
        for r in rows:
            clean = clean_phone_number(r["phone"])
            if clean and clean != r["phone"]:
                conflict = db.execute("SELECT id FROM clients WHERE phone=? AND id!=?", (clean, r["id"])).fetchone()
                if not conflict:
                    db.execute("UPDATE clients SET phone=? WHERE id=?", (clean, r["id"]))
    except Exception:
        pass


def get_client(cid):
    client = get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not client:
        abort(404, description="That user no longer exists.")
    return client


def get_or_create_client(name, phone=None):
    name = client_name(name)
    database = get_db()
    if phone:
        phone = clean_phone_number(phone)
        if phone:
            row = database.execute("SELECT id FROM clients WHERE phone=?", (phone,)).fetchone()
            if row:
                return row["id"]
            row = database.execute(
                "INSERT INTO clients(name, phone) VALUES (?, ?) ON CONFLICT DO NOTHING RETURNING id", (name, phone)
            ).fetchone()
            if row:
                return row["id"]

    row = database.execute("SELECT id FROM clients WHERE lower(name)=lower(?)", (name,)).fetchone()
    if row:
        return row["id"]
    row = database.execute(
        "INSERT INTO clients(name) VALUES (?) ON CONFLICT DO NOTHING RETURNING id", (name,)
    ).fetchone()
    if row:
        return row["id"]
    return database.execute("SELECT id FROM clients WHERE lower(name)=lower(?)", (name,)).fetchone()["id"]


def insert_trade(cid, trade, source="manual", upload_id=None):
    return insert_trades(cid, [trade], source, upload_id)[0]


def insert_trades(cid, trades, source="manual", upload_id=None):
    if not trades or len(trades) > MAX_ROWS:
        raise ValidationError(f"Save between 1 and {MAX_ROWS} trades in a batch.")
    if source not in ("manual", "import", "upload"):
        source = "manual"
    ctype, rate = effective_rule(get_client(cid))
    parameters = []
    db = get_db()
    for trade in trades:
        stock = resolve_canonical_stock(db, trade["stock"])
        bp = trade.get("buy_price")
        sp = trade.get("sell_price")
        b_date = trade.get("buy_date")
        s_date = trade.get("sell_date")

        # Defensive pairs check to strictly satisfy DB constraints:
        # trades_buy_pair: ((buy_price IS NULL) = (buy_date IS NULL))
        if bp is not None:
            if not b_date:
                b_date = date.today().isoformat()
        else:
            b_date = None

        # trades_sell_pair: ((sell_price IS NULL) = (sell_date IS NULL))
        if sp is not None:
            if not s_date:
                s_date = date.today().isoformat()
        else:
            s_date = None

        # trades_date_order: sell_date >= buy_date if both are present
        if b_date and s_date and s_date < b_date:
            s_date = b_date

        deduct_from_funds = 1 if trade.get("deduct_from_funds") in (1, "1", "true", "True", "on", True) else 0
        parameters.extend((
            cid, stock, trade["quantity"], bp, sp,
            b_date, s_date, trade.get("commission_override"),
            ctype, rate, source, upload_id, trade.get("notes"),
            deduct_from_funds,
        ))
    rows = db.execute(
        """INSERT INTO trades(
            client_id,stock,quantity,buy_price,sell_price,buy_date,sell_date,commission_override,
            commission_type_snapshot,commission_rate_snapshot,source,upload_id,notes,deduct_from_funds
        ) VALUES """ + ",".join("(?,?,?,?,?,?,?,?,?,?,?,?,?,?)" for _ in trades) + " RETURNING id",
        parameters,
    ).fetchall()
    return [row["id"] for row in rows]


def enrich(trade):
    result = dict(trade)
    for key in ("quantity", "buy_price", "sell_price", "commission_rate",
                "commission_override", "commission_rate_snapshot"):
        if result.get(key) is not None:
            result[key] = dec(result[key])
    quantity = result["quantity"]
    closed = result["buy_price"] is not None and result["sell_price"] is not None
    result["status"] = "Closed" if closed else "Open"
    result["invested"] = (result["buy_price"] or ZERO) * quantity
    result["pnl"] = (result["sell_price"] - result["buy_price"]) * quantity if closed else ZERO
    result["pnl_pct"] = result["pnl"] / result["invested"] * 100 if closed and result["invested"] else ZERO
    if result.get("commission_type_snapshot") is not None:
        ctype, rate = result["commission_type_snapshot"], result["commission_rate_snapshot"]
    else:
        ctype, rate = effective_rule(result)
    result["eff_type"], result["eff_rate"] = ctype, rate
    if not closed or result["pnl"] <= ZERO:
        commission = ZERO
    elif result.get("commission_override") is not None:
        commission = max(result["commission_override"], ZERO)
    elif ctype == "turnover_pct":
        commission = (result["buy_price"] + result["sell_price"]) * quantity * rate / 100
    elif ctype == "flat":
        commission = rate
    else:
        commission = result["pnl"] * rate / 100
    result["commission"] = max(commission, ZERO).quantize(CENT, rounding=ROUND_HALF_UP)
    result["holding_days"] = None
    if result["buy_date"] and result["sell_date"]:
        try:
            result["holding_days"] = (
                date.fromisoformat(result["sell_date"]) - date.fromisoformat(result["buy_date"])
            ).days
        except ValueError:
            current_app.logger.warning("Legacy trade %s has an invalid stored date.", result["id"])
    return result


def query_trades(args):
    where, parameters = [], []
    if args.get("client_id"):
        where.append("t.client_id=?")
        parameters.append(identifier(args["client_id"]))
    if args.get("stock"):
        where.append("lower(t.stock) LIKE lower(?)")
        stock = text(args["stock"], "Stock filter", limit=120)
        parameters.append("%" + stock + "%")
    start = valid_date(args.get("from"), "From date")
    end = valid_date(args.get("to"), "To date")
    if start and end and start > end:
        raise ValidationError("From date cannot be after to date.")
    if start:
        where.append("COALESCE(t.sell_date,t.buy_date)>=?")
        parameters.append(start)
    if end:
        where.append("COALESCE(t.sell_date,t.buy_date)<=?")
        parameters.append(end)
    status = args.get("status", "")
    if status not in ("", None, "open", "closed"):
        raise ValidationError("Choose a valid trade status.")
    if status == "open":
        where.append("(t.buy_price IS NULL OR t.sell_price IS NULL)")
    elif status == "closed":
        where.append("t.buy_price IS NOT NULL AND t.sell_price IS NOT NULL")
    sql = TRADE_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY COALESCE(t.sell_date,t.buy_date) DESC NULLS LAST,t.id DESC"
    return [enrich(row) for row in get_db().execute(sql, parameters).fetchall()]


def summarize(trades):
    closed = [trade for trade in trades if trade["status"] == "Closed"]
    return {
        "count": len(trades), "closed": len(closed), "open": len(trades) - len(closed),
        "pnl": sum((trade["pnl"] for trade in closed), ZERO),
        "commission": sum((trade["commission"] for trade in trades), ZERO),
        "invested_open": sum((trade["invested"] for trade in trades if trade["status"] == "Open"), ZERO),
        "wins": sum(trade["pnl"] > 0 for trade in closed),
        "losses": sum(trade["pnl"] < 0 for trade in closed),
        "turnover": sum(
            (((trade["buy_price"] or ZERO) + (trade["sell_price"] or ZERO)) * trade["quantity"]
             for trade in trades), ZERO
        ),
    }


def payments_by_client():
    rows = get_db().execute("SELECT client_id,SUM(amount) AS total FROM payments GROUP BY client_id").fetchall()
    return {row["client_id"]: dec(row["total"]) for row in rows}


def funds_by_client():
    rows = get_db().execute("SELECT client_id,SUM(amount) AS total FROM funds GROUP BY client_id").fetchall()
    return {row["client_id"]: dec(row["total"]) for row in rows}


def aggregate_clients_sql(start=None, end=None):
    """
    Aggregate per-client performance and commission directly in SQL.
    Replaces O(clients x trades) Python-side loop with a single grouped query.
    Falls back gracefully to Python summarize if SQL grouping encounters any database issue.
    """
    db = get_db()
    def_type = setting("default_commission_type", "profit_pct")
    def_rate = float(setting("default_commission_rate", "10"))

    try:
        date_join_conds = []
        date_params = []
        if start:
            date_join_conds.append("COALESCE(t.sell_date, t.buy_date) >= ?")
            date_params.append(start)
        if end:
            date_join_conds.append("COALESCE(t.sell_date, t.buy_date) <= ?")
            date_params.append(end)

        date_clause = (" AND " + " AND ".join(date_join_conds)) if date_join_conds else ""

        sql = f"""
        SELECT 
            c.*,
            COUNT(t.id) as count,
            SUM(CASE WHEN t.buy_price IS NOT NULL AND t.sell_price IS NOT NULL THEN 1 ELSE 0 END) as closed,
            SUM(CASE WHEN t.buy_price IS NOT NULL AND (t.sell_price IS NULL OR t.sell_price = 0) THEN 1 ELSE 0 END) as open,
            COALESCE(SUM(CASE WHEN t.buy_price IS NOT NULL AND t.sell_price IS NOT NULL THEN t.quantity * (t.sell_price - t.buy_price) ELSE 0 END), 0) as pnl,
            COALESCE(SUM(CASE WHEN t.buy_price IS NOT NULL AND (t.sell_price IS NULL OR t.sell_price = 0) THEN t.quantity * t.buy_price ELSE 0 END), 0) as invested_open,
            SUM(CASE WHEN t.buy_price IS NOT NULL AND t.sell_price IS NOT NULL AND t.sell_price > t.buy_price THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN t.buy_price IS NOT NULL AND t.sell_price IS NOT NULL AND t.sell_price < t.buy_price THEN 1 ELSE 0 END) as losses,
            COALESCE(SUM(t.quantity * (COALESCE(t.buy_price, 0) + COALESCE(t.sell_price, 0))), 0) as turnover,
            COALESCE(SUM(
                CASE 
                    WHEN t.buy_price IS NULL OR t.sell_price IS NULL THEN 0
                    WHEN t.sell_price <= t.buy_price THEN 0
                    WHEN t.commission_override IS NOT NULL 
                        THEN CASE WHEN t.commission_override > 0 THEN t.commission_override ELSE 0 END
                    WHEN COALESCE(t.commission_type_snapshot, c.commission_type, ?) = 'turnover_pct' 
                        THEN (t.buy_price + t.sell_price) * t.quantity * COALESCE(t.commission_rate_snapshot, c.commission_rate, ?) / 100.0
                    WHEN COALESCE(t.commission_type_snapshot, c.commission_type, ?) = 'flat' 
                        THEN COALESCE(t.commission_rate_snapshot, c.commission_rate, ?)
                    ELSE 
                        (t.sell_price - t.buy_price) * t.quantity * COALESCE(t.commission_rate_snapshot, c.commission_rate, ?) / 100.0
                END
            ), 0) as commission
        FROM clients c
        LEFT JOIN trades t ON t.client_id = c.id {date_clause}
        GROUP BY c.id
        ORDER BY lower(c.name), c.id
        """
        params = (def_type, def_rate, def_type, def_rate, def_rate, *date_params)
        rows = db.execute(sql, params).fetchall()

        result = []
        for row in rows:
            item = dict(row)
            item["pnl"] = dec(item.get("pnl"))
            item["commission"] = dec(item.get("commission")).quantize(CENT, rounding=ROUND_HALF_UP)
            item["invested_open"] = dec(item.get("invested_open"))
            item["turnover"] = dec(item.get("turnover"))
            item["rule"] = rule_label(item.get("commission_type"), item.get("commission_rate"))
            result.append(item)
        return result
    except Exception as exc:
        current_app.logger.warning("aggregate_clients_sql failed, falling back to python summarize: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
        # Fallback to python summarize
        client_rows = db.execute("SELECT * FROM clients ORDER BY lower(name), id").fetchall()
        args = {}
        if start:
            args["from"] = start
        if end:
            args["to"] = end
        all_trades = query_trades(args)
        trades_by_client = {}
        for t in all_trades:
            trades_by_client.setdefault(t["client_id"], []).append(t)
        fallback_result = []
        for c in client_rows:
            client_dict = dict(c)
            c_trades = trades_by_client.get(client_dict["id"], [])
            summary = summarize(c_trades)
            fallback_result.append({
                **client_dict,
                **summary,
                "rule": rule_label(client_dict.get("commission_type"), client_dict.get("commission_rate")),
            })
        return fallback_result


def sync_client_holdings_from_trades(cid=None, db=None):
    """Sync open buy trades into client_shares, accumulating all open trades per client and stock
    into a single cumulative holding with weighted average buy price (like standard Demat/trading accounts)."""
    if db is None:
        db = get_db()

    alias_map = get_alias_mapping(db)

    where_sql = "WHERE client_id=?" if cid is not None else ""
    params = (cid,) if cid is not None else ()

    try:
        open_trades = db.execute(
            f"""SELECT id, client_id, stock, quantity, buy_price, COALESCE(buy_date, created_at) as added_on,
                       COALESCE(deduct_from_funds, 0) as deduct_from_funds, notes, source
                FROM trades
                {where_sql} {"AND" if where_sql else "WHERE"} buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0)
                ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
            params
        ).fetchall()

        closed_rows = db.execute(
            f"""SELECT DISTINCT client_id, stock FROM trades
                {where_sql} {"AND" if where_sql else "WHERE"} sell_price IS NOT NULL AND sell_price > 0""",
            params
        ).fetchall()
        closed_by_client = {}
        for cr in closed_rows:
            raw_s = (cr["stock"] or "").strip().upper()
            if not raw_s:
                continue
            closed_by_client.setdefault(cr["client_id"], set()).add(alias_map.get(raw_s, raw_s))

        # Group open trades by (client_id, canonical_stock)
        grouped = {}
        for ot in open_trades:
            c_id = ot["client_id"]
            raw_s = (ot["stock"] or "").strip().upper()
            stk = alias_map.get(raw_s, raw_s)
            if not stk:
                continue
            grouped.setdefault((c_id, stk), []).append(ot)

        # Load existing client_shares grouped by (client_id, canonical_stock)
        cs_rows = db.execute(
            f"SELECT id, client_id, stock, quantity FROM client_shares {where_sql} ORDER BY id ASC",
            params
        ).fetchall()
        cs_by_key = {}
        for cs in cs_rows:
            raw_s = (cs["stock"] or "").strip().upper()
            stk = alias_map.get(raw_s, raw_s)
            cs_by_key.setdefault((cs["client_id"], stk), []).append(cs)

        if cid is not None:
            target_cids = [cid]
        else:
            all_c = db.execute("SELECT id FROM clients").fetchall()
            target_cids = [c["id"] for c in all_c]

        for target_cid in target_cids:
            client_keys = [k for k in grouped.keys() if k[0] == target_cid]
            seen_stocks = set()

            for key in client_keys:
                stk = key[1]
                seen_stocks.add(stk)
                trades_list = grouped[key]

                total_qty = ZERO
                total_cost = ZERO
                earliest_date = None
                deduct_val = 0

                for t in trades_list:
                    qty = dec(t["quantity"] or 0)
                    bp = dec(t["buy_price"] or 0)
                    if qty > ZERO:
                        total_qty += qty
                        total_cost += qty * bp
                        if t.get("deduct_from_funds"):
                            deduct_val = 1
                        t_date = str(t["added_on"])[:10] if t.get("added_on") else ""
                        if t_date and (not earliest_date or t_date < earliest_date):
                            earliest_date = t_date

                existing_cs = cs_by_key.get((target_cid, stk), [])
                int_qty = int(round(total_qty)) if total_qty > ZERO else 0

                if total_qty > ZERO and int_qty > 0:
                    avg_buy_price = (total_cost / total_qty).quantize(CENT, rounding=ROUND_HALF_UP)
                    earliest_date = earliest_date or date.today().isoformat()

                    if existing_cs:
                        db.execute(
                            "UPDATE client_shares SET stock=?, quantity=?, buy_price=?, added_on=?, deduct_from_funds=? WHERE id=?",
                            (stk, int_qty, avg_buy_price, earliest_date, deduct_val, existing_cs[0]["id"])
                        )
                        for dup in existing_cs[1:]:
                            db.execute("DELETE FROM client_shares WHERE id=?", (dup["id"],))
                    else:
                        db.execute(
                            "INSERT INTO client_shares (client_id, stock, quantity, buy_price, added_on, deduct_from_funds) VALUES (?, ?, ?, ?, ?, ?)",
                            (target_cid, stk, int_qty, avg_buy_price, earliest_date, deduct_val)
                        )
                else:
                    for cs in existing_cs:
                        db.execute("DELETE FROM client_shares WHERE id=?", (cs["id"],))

            # Clean up any stocks in client_shares for this client that no longer have open trades
            closed_stocks = closed_by_client.get(target_cid, set())
            for (cs_cid, cs_stock), rows in cs_by_key.items():
                if cs_cid != target_cid or cs_stock in seen_stocks:
                    continue
                if cs_stock in closed_stocks:
                    for cs in rows:
                        db.execute("DELETE FROM client_shares WHERE id=?", (cs["id"],))
                else:
                    for cs in rows:
                        if dec(cs.get("quantity") or 0) <= ZERO:
                            db.execute("DELETE FROM client_shares WHERE id=?", (cs["id"],))

        db.commit()
    except Exception as exc:
        current_app.logger.warning("sync_client_holdings_from_trades: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


def get_client_calculated_holdings(cid, db=None, sync_trades=True):
    if db is None:
        db = get_db()

    if sync_trades:
        try:
            sync_client_holdings_from_trades(cid, db)
        except Exception as exc:
            current_app.logger.warning("get_client_calculated_holdings sync error: %s", exc)
            try:
                db.rollback()
            except Exception:
                pass

    alias_map = get_alias_mapping(db)

    # Fetch individual open trade lots for this client
    try:
        open_trades = db.execute(
            """SELECT id, client_id, stock, quantity, buy_price,
                      COALESCE(buy_date, created_at) as added_on,
                      COALESCE(deduct_from_funds, 0) as deduct_from_funds, notes, source
               FROM trades
               WHERE client_id=? AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0)
               ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
            (cid,)
        ).fetchall()
    except Exception:
        open_trades = []

    lots_by_stock = {}
    for ot in open_trades:
        raw_s = (ot["stock"] or "").strip().upper()
        stk = alias_map.get(raw_s, raw_s)
        if not stk:
            continue
        if stk not in lots_by_stock:
            lots_by_stock[stk] = []
        ot_qty = dec(ot["quantity"] or 0)
        ot_bp = dec(ot["buy_price"] or 0)
        lots_by_stock[stk].append({
            "id": ot["id"],
            "stock": stk,
            "quantity": int(round(ot_qty)),
            "buy_price": ot_bp.quantize(CENT, rounding=ROUND_HALF_UP),
            "total_cost": (ot_qty * ot_bp).quantize(CENT, rounding=ROUND_HALF_UP),
            "added_on": str(ot["added_on"])[:10] if ot.get("added_on") else "",
            "notes": ot.get("notes") or "",
            "source": ot.get("source") or "",
            "deduct_from_funds": 1 if ot.get("deduct_from_funds") else 0,
        })

    # Pre-load canonical closed stocks for this client (alias-aware)
    closed_canonicals = set()
    try:
        closed_rows = db.execute(
            "SELECT DISTINCT stock FROM trades WHERE client_id=? AND sell_price IS NOT NULL AND sell_price > 0",
            (cid,)
        ).fetchall()
        for cr in closed_rows:
            rs = (cr["stock"] or "").strip().upper()
            if rs:
                closed_canonicals.add(alias_map.get(rs, rs))
    except Exception:
        pass

    # Fetch accumulated positions from client_shares
    try:
        raw_shares = db.execute(
            "SELECT id, stock, quantity, buy_price, added_on, COALESCE(deduct_from_funds, 0) as deduct_from_funds FROM client_shares WHERE client_id=? AND quantity > 0 ORDER BY stock ASC",
            (cid,)
        ).fetchall()
    except Exception:
        raw_shares = []

    results = []
    seen_stocks = set()
    for r in raw_shares:
        raw_s = (r["stock"] or "").strip().upper()
        stk = alias_map.get(raw_s, raw_s)
        if not stk or stk in seen_stocks:
            continue
        seen_stocks.add(stk)
        qty = dec(r["quantity"]) if r["quantity"] is not None else ZERO
        bp = dec(r["buy_price"]) if r["buy_price"] is not None else ZERO
        if qty <= ZERO:
            continue
        tot_invested = (qty * bp).quantize(CENT, rounding=ROUND_HALF_UP)
        deduct_val = 1 if r.get("deduct_from_funds") else 0
        stock_lots = lots_by_stock.get(stk, [])
        if not stock_lots:
            if stk in closed_canonicals:
                try:
                    db.execute("DELETE FROM client_shares WHERE id=?", (r["id"],))
                    db.commit()
                except Exception:
                    pass
                continue
            else:
                # Standalone holding without open ledger trades — preserve and display it!
                results.append({
                    "id": r["id"],
                    "stock": stk,
                    "quantity": int(round(qty)),
                    "buy_price": bp.quantize(CENT, rounding=ROUND_HALF_UP),
                    "total_invested": tot_invested,
                    "added_on": str(r["added_on"]) if r.get("added_on") else "",
                    "deduct_from_funds": deduct_val,
                    "lots": [{
                        "id": r["id"],
                        "stock": stk,
                        "quantity": int(round(qty)),
                        "buy_price": bp.quantize(CENT, rounding=ROUND_HALF_UP),
                        "total_cost": tot_invested,
                        "added_on": str(r["added_on"])[:10] if r.get("added_on") else date.today().isoformat(),
                        "notes": "Direct holding",
                        "source": "manual",
                        "deduct_from_funds": deduct_val,
                    }],
                    "lots_count": 1,
                })
                continue
        results.append({
            "id": r["id"],
            "stock": stk,
            "quantity": int(round(qty)),
            "buy_price": bp.quantize(CENT, rounding=ROUND_HALF_UP),
            "total_invested": tot_invested,
            "added_on": str(r["added_on"]) if r.get("added_on") else "",
            "deduct_from_funds": deduct_val,
            "lots": stock_lots,
            "lots_count": len(stock_lots),
        })

    # In case any stock has open trades but didn't make it to client_shares yet
    for stk, stock_lots in lots_by_stock.items():
        if stk not in seen_stocks and stock_lots:
            seen_stocks.add(stk)
            tot_qty = sum((l["quantity"] for l in stock_lots), 0)
            tot_invested = sum((l["total_cost"] for l in stock_lots), ZERO)
            avg_bp = (tot_invested / dec(tot_qty)).quantize(CENT, rounding=ROUND_HALF_UP) if tot_qty else ZERO
            results.append({
                "id": stock_lots[0]["id"],
                "stock": stk,
                "quantity": tot_qty,
                "buy_price": avg_bp,
                "total_invested": tot_invested,
                "added_on": stock_lots[0]["added_on"],
                "deduct_from_funds": 1 if any(l.get("deduct_from_funds") for l in stock_lots) else 0,
                "lots": stock_lots,
                "lots_count": len(stock_lots),
            })

    results.sort(key=lambda x: x["stock"])
    return results


def get_clients_holding_stock(stock, db=None):
    """Returns list of active clients ({'id': ..., 'name': ...}) who currently hold the specified stock (canonical-aware)."""
    if db is None:
        db = get_db()
    if not stock:
        return []
    target_canonical = resolve_canonical_stock(db, stock)
    alias_map = get_alias_mapping(db)
    try:
        rows = db.execute(
            """
            SELECT DISTINCT c.id, c.name, h.stock, h.quantity
            FROM client_shares h
            JOIN clients c ON c.id = h.client_id
            WHERE c.active = 1 AND h.quantity > 0
            ORDER BY lower(c.name)
            """
        ).fetchall()
        holding_clients = []
        seen = set()
        for r in rows:
            sym = (r["stock"] or "").strip().upper()
            if alias_map.get(sym, sym) == target_canonical and r["id"] not in seen:
                seen.add(r["id"])
                holding_clients.append({"id": r["id"], "name": r["name"]})
        return holding_clients
    except Exception:
        return []


def sync_trade_profits_to_funds(db, cid=None):
    """Ensure completed trades have their realized profit recorded in funds ledger exactly once."""
    try:
        where_clause = "WHERE buy_price IS NOT NULL AND sell_price IS NOT NULL"
        params = []
        if cid:
            where_clause += " AND client_id=?"
            params.append(cid)
        closed_trades = db.execute(
            f"SELECT id, client_id, stock, quantity, buy_price, sell_price, sell_date, created_at FROM trades {where_clause}",
            params
        ).fetchall()
        if not closed_trades:
            return

        funds_params = ["%[Trade #%"] + ([cid] if cid else [])
        funds_sql = "SELECT notes FROM funds WHERE notes LIKE ?" + (" AND client_id=?" if cid else "")
        existing_notes_rows = db.execute(funds_sql, funds_params).fetchall()
        existing_notes_text = " ".join([r["notes"] or "" for r in existing_notes_rows])

        for t in closed_trades:
            pnl = ((dec(t["sell_price"]) - dec(t["buy_price"])) * dec(t["quantity"])).quantize(CENT, rounding=ROUND_HALF_UP)
            if pnl == ZERO:
                continue
            tag = f"[Trade #{t['id']}]"
            if tag not in existing_notes_text:
                note = f"[Realized {'Profit' if pnl > 0 else 'Loss'}] {t['stock']} ({t['quantity']} sh) {tag}"
                date_val = t["sell_date"] or str(t["created_at"])[:10] or date.today().isoformat()
                db.execute(
                    "INSERT INTO funds (client_id, amount, added_on, notes) VALUES (?, ?, ?, ?)",
                    (t["client_id"], pnl, date_val, note)
                )
                existing_notes_text += " " + note
    except Exception as exc:
        current_app.logger.warning("sync_trade_profits_to_funds error: %s", exc)


def apply_sale_fifo(client_id, stock, sell_quantity, sell_price, sell_date=None, buy_price_hint=None, notes=None, upload_id=None, source="portal", db=None):
    """Apply FIFO deduction when a client sells shares.
    - Compares sold quantity to total open shares held for that stock.
    - If sold quantity matches (or exceeds) total holdings, closes all open lots (deducts complete shares from holdings).
    - If sold quantity is less than holdings, closes/splits earliest lots via FIFO,
      and recalculates the weighted average buy price of remaining holdings.
    - Syncs realized profits to funds, syncs client holdings, and invalidates caches.
    """
    if db is None:
        db = get_db()

    sell_qty = int(round(dec(sell_quantity or 0)))
    if sell_qty <= 0:
        return []

    sell_price = dec(sell_price)
    sell_date = valid_date(sell_date, "Sell Date") if sell_date else date.today().isoformat()
    user_notes = (notes or "").strip()

    stock_canonical, variants = get_matching_stock_variants(db, stock, include_known_tickers=False)
    if not stock_canonical:
        return []

    def _fetch_open_lots(stock_variants):
        in_sql, in_params = _stock_in_clause(stock_variants)
        return db.execute(
            f"""SELECT id, stock, quantity, buy_price, buy_date as raw_buy_date,
                       COALESCE(buy_date, created_at) as buy_date,
                       COALESCE(deduct_from_funds, 0) as deduct_from_funds, notes, upload_id
                FROM trades
                WHERE client_id = ?
                  AND {in_sql}
                  AND buy_price IS NOT NULL
                  AND (sell_price IS NULL OR sell_price = 0)
                ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
            (client_id, *in_params)
        ).fetchall()

    def _fetch_cs_rows(stock_variants):
        in_sql, in_params = _stock_in_clause(stock_variants)
        return db.execute(
            f"""SELECT id, stock, quantity, buy_price, added_on,
                       COALESCE(deduct_from_funds, 0) as deduct_from_funds
                FROM client_shares
                WHERE client_id=? AND {in_sql}
                ORDER BY id ASC""",
            (client_id, *in_params)
        ).fetchall()

    open_lots = _fetch_open_lots(variants)
    cs_rows = _fetch_cs_rows(variants)

    # Fallback: if no direct/alias match exists, check unambiguous ticker equivalents (e.g. TECHNOCRAFT <-> TECHNOCRAF)
    if not open_lots and not cs_rows:
        fallback_canon, fallback_variants = get_matching_stock_variants(db, stock, include_known_tickers=True)
        if fallback_variants - variants:
            fb_lots = _fetch_open_lots(fallback_variants)
            fb_cs = _fetch_cs_rows(fallback_variants)
            if fb_lots or fb_cs:
                variants = fallback_variants
                open_lots = fb_lots
                cs_rows = fb_cs
                # Prefer the canonical stock used by existing open holdings/lots
                alias_map = get_alias_mapping(db)
                existing_raw = (
                    (open_lots[0]["stock"] if open_lots else cs_rows[0]["stock"]) or fallback_canon
                ).strip().upper()
                stock_canonical = alias_map.get(existing_raw, existing_raw)

    cs_qty = sum((dec(r["quantity"] or 0) for r in cs_rows if dec(r["quantity"] or 0) > ZERO), ZERO)
    total_open_qty = sum((dec(lot["quantity"] or 0) for lot in open_lots), ZERO)

    # If client_shares has more shares than open trades, ensure the difference is represented in open trades
    if cs_qty > total_open_qty:
        diff_qty = int(round(cs_qty - total_open_qty))
        cs_cost = ZERO
        cs_qty_for_avg = ZERO
        earliest_added = None
        cs_deduct = 0
        for r in cs_rows:
            rq = dec(r["quantity"] or 0)
            if rq <= ZERO:
                continue
            rbp = dec(r["buy_price"]) if r.get("buy_price") is not None else (
                dec(buy_price_hint) if buy_price_hint is not None else ZERO
            )
            cs_cost += rq * rbp
            cs_qty_for_avg += rq
            if r.get("deduct_from_funds"):
                cs_deduct = 1
            ad = str(r["added_on"])[:10] if r.get("added_on") else ""
            if ad and (not earliest_added or ad < earliest_added):
                earliest_added = ad
        bp = (cs_cost / cs_qty_for_avg).quantize(CENT, rounding=ROUND_HALF_UP) if cs_qty_for_avg > ZERO else ZERO
        b_date = earliest_added or sell_date
        if b_date > sell_date:
            b_date = sell_date
        db.execute(
            """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source, deduct_from_funds)
               VALUES (?, ?, ?, ?, ?, 'Holding position', 'manual', ?)""",
            (client_id, stock_canonical, diff_qty, bp, b_date, cs_deduct)
        )
        open_lots = _fetch_open_lots(variants)
        total_open_qty = sum((dec(lot["quantity"] or 0) for lot in open_lots), ZERO)

    int_open_qty = int(round(total_open_qty))
    affected_ids = []

    if int_open_qty == 0:
        # No open lots found: insert as regular closed trade
        bp = buy_price_hint if buy_price_hint is not None else sell_price
        t_ids = insert_trades(client_id, [{
            "stock": stock_canonical,
            "quantity": sell_qty,
            "buy_price": bp,
            "sell_price": sell_price,
            "buy_date": sell_date,
            "sell_date": sell_date,
            "notes": user_notes or "Sold trade",
            "deduct_from_funds": 0,
        }], source=source, upload_id=upload_id)
        affected_ids.extend(t_ids)

    elif sell_qty == int_open_qty:
        # Exact match: close all open lots, completely clearing holdings
        for lot in open_lots:
            lot_id = lot["id"]
            existing_notes = (lot["notes"] or "").strip()
            merged_notes = f"{existing_notes} [{user_notes}]".strip() if (user_notes and user_notes not in existing_notes) else (existing_notes or user_notes or "Sold from Holdings")
            lot_sell_date = _safe_sell_date(sell_date, lot.get("raw_buy_date"))
            db.execute(
                """UPDATE trades
                   SET sell_price=?, sell_date=?, notes=?, upload_id=COALESCE(upload_id, ?)
                   WHERE id=?""",
                (sell_price, lot_sell_date, merged_notes, upload_id, lot_id)
            )
            affected_ids.append(lot_id)

    elif sell_qty < int_open_qty:
        # Partial sale: FIFO deduction across lots, split lot where needed
        rem_sell = dec(sell_qty)
        for lot in open_lots:
            if rem_sell <= ZERO:
                break
            lot_qty = dec(lot["quantity"] or 0)
            existing_notes = (lot["notes"] or "").strip()
            lot_buy_date = str(lot["buy_date"])[:10] if lot["buy_date"] else sell_date
            lot_sell_date = _safe_sell_date(sell_date, lot.get("raw_buy_date") or lot_buy_date)

            if lot_qty <= rem_sell:
                # Lot is fully consumed by this sale
                merged_notes = f"{existing_notes} [{user_notes}]".strip() if (user_notes and user_notes not in existing_notes) else (existing_notes or user_notes or "Sold from Holdings")
                db.execute(
                    """UPDATE trades
                       SET sell_price=?, sell_date=?, notes=?, upload_id=COALESCE(upload_id, ?)
                       WHERE id=?""",
                    (sell_price, lot_sell_date, merged_notes, upload_id, lot["id"])
                )
                affected_ids.append(lot["id"])
                rem_sell -= lot_qty
            else:
                # Lot is partially consumed: split
                rem_open = int(round(lot_qty - rem_sell))
                sold_portion = int(round(rem_sell))

                # Update existing lot to keep the open remainder
                db.execute("UPDATE trades SET quantity=? WHERE id=?", (rem_open, lot["id"]))

                # Insert new closed trade for the sold portion with original lot buy_price
                sold_notes = f"{existing_notes} [{user_notes}]".strip() if (user_notes and user_notes not in existing_notes) else (existing_notes or user_notes or "Sold from Holdings")
                lot_stock = (lot.get("stock") or stock_canonical).strip().upper()
                new_ids = insert_trades(client_id, [{
                    "stock": lot_stock,
                    "quantity": sold_portion,
                    "buy_price": dec(lot["buy_price"]),
                    "sell_price": sell_price,
                    "buy_date": lot_buy_date,
                    "sell_date": lot_sell_date,
                    "notes": sold_notes,
                    "deduct_from_funds": lot.get("deduct_from_funds", 0),
                }], source=source, upload_id=upload_id or lot.get("upload_id"))
                affected_ids.extend(new_ids)
                rem_sell = ZERO
                break

    else:
        # sell_qty > int_open_qty: close all open lots, insert excess as standalone closed trade
        for lot in open_lots:
            lot_id = lot["id"]
            existing_notes = (lot["notes"] or "").strip()
            merged_notes = f"{existing_notes} [{user_notes}]".strip() if (user_notes and user_notes not in existing_notes) else (existing_notes or user_notes or "Sold from Holdings")
            lot_sell_date = _safe_sell_date(sell_date, lot.get("raw_buy_date"))
            db.execute(
                """UPDATE trades
                   SET sell_price=?, sell_date=?, notes=?, upload_id=COALESCE(upload_id, ?)
                   WHERE id=?""",
                (sell_price, lot_sell_date, merged_notes, upload_id, lot_id)
            )
            affected_ids.append(lot_id)

        excess_qty = sell_qty - int_open_qty
        bp = buy_price_hint if buy_price_hint is not None else sell_price
        new_ids = insert_trades(client_id, [{
            "stock": stock_canonical,
            "quantity": excess_qty,
            "buy_price": bp,
            "sell_price": sell_price,
            "buy_date": sell_date,
            "sell_date": sell_date,
            "notes": f"{user_notes} (Excess sold)".strip() if user_notes else "Sold trade (Excess sold)",
            "deduct_from_funds": 0,
        }], source=source, upload_id=upload_id)
        affected_ids.extend(new_ids)

    # Explicitly clear client_shares for this stock when all open holdings were sold
    if int_open_qty > 0 and sell_qty >= int_open_qty:
        in_sql, in_params = _stock_in_clause(variants)
        db.execute(
            f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}",
            (client_id, *in_params),
        )

    # Sync funds realized profits and recalculate client holdings
    sync_trade_profits_to_funds(db, client_id)
    sync_client_holdings_from_trades(client_id, db)
    invalidate_stock_caches()
    invalidate_prices_cache()
    return affected_ids


def get_pending_holding_reconciliations(db=None, client_id=None):
    """
    Detects historical completed trades that correspond to still-open holding lots
    for the same stock (e.g. where a sale was entered as a standalone closed trade
    leaving the original buy lots open).
    Leaves them in a pending state awaiting Admin confirmation (Accept vs Let it be).
    """
    if db is None:
        db = get_db()

    raw_dismissed = setting("dismissed_holding_reconciliations", "[]")
    try:
        dismissed_raw = json.loads(raw_dismissed)
        if isinstance(dismissed_raw, list):
            dismissed_ids = set(dismissed_raw)
        else:
            dismissed_ids = set()
    except Exception:
        dismissed_ids = set()

    alias_map = get_alias_mapping(db)

    where_sql = "WHERE id=?" if client_id is not None else ""
    params = (client_id,) if client_id is not None else ()
    clients = db.execute(f"SELECT id, name FROM clients {where_sql} ORDER BY lower(name)", params).fetchall()

    reconciliations = []

    for c in clients:
        cid = c["id"]
        c_name = c["name"]

        # Fetch all open trades for client
        open_trades = db.execute(
            """SELECT id, stock, quantity, buy_price, COALESCE(buy_date, created_at) as buy_date, notes, source
               FROM trades
               WHERE client_id = ?
                 AND buy_price IS NOT NULL
                 AND (sell_price IS NULL OR sell_price = 0)
               ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
            (cid,)
        ).fetchall()

        if not open_trades:
            continue

        # Fetch all closed/sell trades for client (including sell-only rows where buy_price IS NULL)
        closed_trades = db.execute(
            """SELECT id, stock, quantity, buy_price, sell_price, buy_date, sell_date, notes, source, upload_id, created_at
               FROM trades
               WHERE client_id = ?
                 AND sell_price IS NOT NULL
                 AND sell_price > 0
               ORDER BY COALESCE(sell_date, created_at) DESC, id DESC""",
            (cid,)
        ).fetchall()

        if not closed_trades:
            continue

        # Group open trades by canonical stock
        open_by_stock = {}
        for ot in open_trades:
            raw_s = (ot["stock"] or "").strip().upper()
            stk = alias_map.get(raw_s, raw_s)
            if not stk:
                continue
            open_by_stock.setdefault(stk, []).append(ot)

        # Group closed trades by canonical stock (matching ticker family to open stock if needed)
        closed_by_stock = {}
        for ct in closed_trades:
            if ct["id"] in dismissed_ids:
                continue
            ct_notes = ct["notes"] or ""
            if any(tag in ct_notes for tag in ("[Reconciled", "[Sold from Holdings]", "Reconciled Sale")):
                continue
            raw_s = (ct["stock"] or "").strip().upper()
            stk = alias_map.get(raw_s, raw_s)
            if not stk:
                continue
            if stk not in open_by_stock:
                _, fb_variants = get_matching_stock_variants(db, raw_s, include_known_tickers=True)
                matched_open = next((ostk for ostk in open_by_stock if ostk in fb_variants), None)
                if matched_open:
                    stk = matched_open
            closed_by_stock.setdefault(stk, []).append(ct)

        # Detect candidate reconciliations
        for stk, ots in open_by_stock.items():
            cts = closed_by_stock.get(stk, [])
            if not cts:
                continue

            total_open_qty = sum(dec(ot["quantity"] or 0) for ot in ots)
            if total_open_qty <= ZERO:
                continue

            total_open_cost = sum(dec(ot["quantity"] or 0) * dec(ot["buy_price"] or 0) for ot in ots)
            avg_open_price = (total_open_cost / total_open_qty).quantize(CENT, rounding=ROUND_HALF_UP)

            for ct in cts:
                ct_qty = dec(ct["quantity"] or 0)
                ct_bp = dec(ct["buy_price"]) if ct.get("buy_price") is not None else avg_open_price
                ct_sp = dec(ct["sell_price"] or 0)

                # Sold quantity cannot exceed current open holdings
                if ct_qty > total_open_qty:
                    continue

                # Matching criteria:
                # 1) Exact quantity match (e.g. 58 == 58, 2000 == 2000, 400 == 400)
                # 2) Or buy price equals holding average buy price (within 10 paise) or sell-only for a partial sale
                is_exact_qty = (int(round(ct_qty)) == int(round(total_open_qty)))
                is_bp_match = (ct.get("buy_price") is None) or (abs(ct_bp - avg_open_price) <= dec("0.10"))
                is_partial = (ct_qty < total_open_qty)

                if is_exact_qty or (is_bp_match and is_partial):
                    pnl = ((ct_sp - ct_bp) * ct_qty).quantize(CENT, rounding=ROUND_HALF_UP)
                    reconciliations.append({
                        "client_id": cid,
                        "client_name": c_name,
                        "stock": stk,
                        "closed_trade": {
                            "id": ct["id"],
                            "quantity": int(round(ct_qty)),
                            "buy_price": ct_bp,
                            "sell_price": ct_sp,
                            "buy_date": ct["buy_date"],
                            "sell_date": ct["sell_date"] or str(ct["created_at"])[:10],
                            "pnl": pnl,
                            "notes": ct["notes"],
                            "upload_id": ct.get("upload_id"),
                        },
                        "open_lots": [
                            {
                                "id": ot["id"],
                                "quantity": int(round(dec(ot["quantity"] or 0))),
                                "buy_price": dec(ot["buy_price"] or 0),
                                "buy_date": str(ot["buy_date"])[:10] if ot["buy_date"] else "",
                                "notes": ot["notes"],
                            }
                            for ot in ots
                        ],
                        "total_open_qty": int(round(total_open_qty)),
                        "avg_open_price": avg_open_price,
                        "is_exact_qty_match": is_exact_qty,
                    })

    return reconciliations


def _ticker_family_key(raw_stock, alias_map):
    """Group stock variants into a family key for mismatch detection (excluding ambiguous BLS sub-companies)."""
    raw_up = (raw_stock or "").strip().upper()
    if not raw_up:
        return ""
    canon = alias_map.get(raw_up, raw_up)
    if raw_up not in _AMBIGUOUS_KNOWN_TICKER_KEYS and canon not in _AMBIGUOUS_KNOWN_TICKER_KEYS:
        ticker = KNOWN_TICKERS.get(raw_up) or KNOWN_TICKERS.get(canon)
        if ticker:
            return f"TICKER:{ticker}"
    return f"STOCK:{canon}"


def get_trades_holdings_mismatches(db=None, client_id=None, include_dismissed=False, pending_recs=None):
    """Audit all clients' trades vs client_shares holdings (read-only) and return actionable mismatch prompts."""
    if db is None:
        db = get_db()

    alias_map = get_alias_mapping(db)
    raw_dismissed = setting("dismissed_holding_mismatches", "[]")
    try:
        dismissed_raw = json.loads(raw_dismissed)
        dismissed_keys = set(dismissed_raw) if isinstance(dismissed_raw, list) else set()
    except Exception:
        dismissed_keys = set()

    if pending_recs is None:
        pending_recs = get_pending_holding_reconciliations(db, client_id)
    reconciled_ct_ids = {r["closed_trade"]["id"] for r in pending_recs}

    where_sql = "WHERE id=?" if client_id is not None else ""
    params = (client_id,) if client_id is not None else ()
    clients = db.execute(f"SELECT id, name FROM clients {where_sql} ORDER BY lower(name)", params).fetchall()
    if not clients:
        return []

    t_where = "WHERE client_id=?" if client_id is not None else ""
    all_trades = db.execute(
        f"""SELECT id, client_id, stock, quantity, buy_price, sell_price, buy_date, sell_date, notes, created_at
            FROM trades {t_where}
            ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
        params
    ).fetchall()
    all_shares = db.execute(
        f"SELECT id, client_id, stock, quantity, buy_price, added_on FROM client_shares {t_where} ORDER BY id ASC",
        params
    ).fetchall()

    trades_by_client = {}
    for t in all_trades:
        trades_by_client.setdefault(t["client_id"], []).append(t)

    shares_by_client = {}
    for s in all_shares:
        shares_by_client.setdefault(s["client_id"], []).append(s)

    mismatches = []

    def _add(item):
        if not include_dismissed and item["key"] in dismissed_keys:
            return
        mismatches.append(item)

    for c in clients:
        cid = c["id"]
        cname = c["name"]
        c_trades = trades_by_client.get(cid, [])
        c_shares = [s for s in shares_by_client.get(cid, []) if dec(s.get("quantity") or 0) > ZERO]

        open_by_canon = {}
        closed_by_canon = {}
        open_by_family = {}
        closed_by_family = {}
        shares_by_canon = {}
        shares_by_family = {}
        names_by_family = {}

        for t in c_trades:
            raw_s = (t["stock"] or "").strip().upper()
            if not raw_s:
                continue
            canon = alias_map.get(raw_s, raw_s)
            fam = _ticker_family_key(raw_s, alias_map)
            is_closed = t.get("sell_price") is not None and dec(t["sell_price"]) > ZERO
            is_open = t.get("buy_price") is not None and not is_closed
            if is_open:
                open_by_canon.setdefault(canon, []).append(t)
                open_by_family.setdefault(fam, []).append(t)
                names_by_family.setdefault(fam, set()).add(raw_s)
            elif is_closed:
                closed_by_canon.setdefault(canon, []).append(t)
                closed_by_family.setdefault(fam, []).append(t)
                names_by_family.setdefault(fam, set()).add(raw_s)

        for s in c_shares:
            raw_s = (s["stock"] or "").strip().upper()
            if not raw_s:
                continue
            canon = alias_map.get(raw_s, raw_s)
            fam = _ticker_family_key(raw_s, alias_map)
            shares_by_canon.setdefault(canon, []).append(s)
            shares_by_family.setdefault(fam, []).append(s)
            names_by_family.setdefault(fam, set()).add(raw_s)

        # 1. Stock name / alias split across trades & holdings (e.g. TECHNOCRAF vs TECHNOCRAFT)
        for fam, raw_names in sorted(names_by_family.items()):
            active_names = set()
            for t in open_by_family.get(fam, []):
                active_names.add((t["stock"] or "").strip().upper())
            for s in shares_by_family.get(fam, []):
                active_names.add((s["stock"] or "").strip().upper())
            # Also check if a closed trade has a different name than active holdings/open lots
            if active_names and len(raw_names) > 1:
                canon_candidates = sorted({alias_map.get(n, n) for n in raw_names})
                preferred = canon_candidates[0]
                _add({
                    "key": f"{cid}:stock_name_mismatch:{fam}",
                    "client_id": cid,
                    "client_name": cname,
                    "stock": preferred,
                    "kind": "stock_name_mismatch",
                    "severity": "high",
                    "title": "Stock Name / Alias Mismatch",
                    "summary": (
                        f"Trades and holdings use multiple names for the same stock: {', '.join(sorted(raw_names))}. "
                        f"Unifying them prevents sales or deletes from missing open lots."
                    ),
                    "holding_qty": int(round(sum((dec(s["quantity"] or 0) for s in shares_by_family.get(fam, [])), ZERO))),
                    "holding_buy_price": None,
                    "open_qty": int(round(sum((dec(t["quantity"] or 0) for t in open_by_family.get(fam, [])), ZERO))),
                    "open_avg_price": None,
                    "trade_id": None,
                    "variants": sorted(raw_names),
                    "has_closed_variant": bool(closed_by_family.get(fam)),
                    "suggested_action": "unify_stock_name",
                })

        # 2. Holdings vs open trades (missing open trades, missing holding, qty/avg price mismatch)
        all_canons = sorted(set(open_by_canon) | set(shares_by_canon))
        for stk in all_canons:
            ots = open_by_canon.get(stk, [])
            cs_list = shares_by_canon.get(stk, [])
            fam = _ticker_family_key(stk, alias_map)

            open_qty = sum((dec(t["quantity"] or 0) for t in ots), ZERO)
            open_cost = sum((dec(t["quantity"] or 0) * dec(t["buy_price"] or 0) for t in ots), ZERO)
            open_avg = (open_cost / open_qty).quantize(CENT, rounding=ROUND_HALF_UP) if open_qty > ZERO else ZERO

            cs_qty = sum((dec(s["quantity"] or 0) for s in cs_list), ZERO)
            cs_cost = sum((dec(s["quantity"] or 0) * dec(s.get("buy_price") or 0) for s in cs_list), ZERO)
            cs_avg = (cs_cost / cs_qty).quantize(CENT, rounding=ROUND_HALF_UP) if cs_qty > ZERO else ZERO

            if cs_qty > ZERO and open_qty <= ZERO:
                fam_open = open_by_family.get(fam, [])
                has_closed_var = bool(closed_by_family.get(fam))
                if fam_open:
                    # Covered by stock_name_mismatch above, skip duplicate
                    continue
                detail_msg = (
                    f"Holdings shows {int(round(cs_qty))} shares @ ₹{cs_avg}, while Past Trades has 0 open buy lots"
                    + (" and already has closed/sold trade(s) for this stock." if has_closed_var else " (direct holding without a trade entry).")
                )
                _add({
                    "key": f"{cid}:holding_without_open_trades:{stk}",
                    "client_id": cid,
                    "client_name": cname,
                    "stock": stk,
                    "kind": "holding_without_open_trades",
                    "severity": "high" if has_closed_var else "medium",
                    "title": "Stale Holding (Trades Already Closed)" if has_closed_var else "Holding Missing Buy Trade",
                    "summary": detail_msg,
                    "holding_qty": int(round(cs_qty)),
                    "holding_buy_price": cs_avg,
                    "open_qty": 0,
                    "open_avg_price": ZERO,
                    "trade_id": None,
                    "variants": [stk],
                    "has_closed_variant": has_closed_var,
                    "suggested_action": "delete_stale_holding" if has_closed_var else "create_open_trade",
                })
            elif open_qty > ZERO and cs_qty <= ZERO:
                _add({
                    "key": f"{cid}:open_trade_missing_holding:{stk}",
                    "client_id": cid,
                    "client_name": cname,
                    "stock": stk,
                    "kind": "open_trade_missing_holding",
                    "severity": "high",
                    "title": "Open Trade Missing from Holdings",
                    "summary": f"Past Trades has {int(round(open_qty))} open shares @ ₹{open_avg}, but Holdings has 0 shares.",
                    "holding_qty": 0,
                    "holding_buy_price": ZERO,
                    "open_qty": int(round(open_qty)),
                    "open_avg_price": open_avg,
                    "trade_id": None,
                    "variants": [stk],
                    "has_closed_variant": False,
                    "suggested_action": "sync_holding",
                })
            elif open_qty > ZERO and cs_qty > ZERO:
                qty_diff = int(round(cs_qty)) != int(round(open_qty))
                price_diff = abs(cs_avg - open_avg) > Decimal("0.05")
                if qty_diff or price_diff or len(cs_list) > 1:
                    _add({
                        "key": f"{cid}:qty_or_price_mismatch:{stk}",
                        "client_id": cid,
                        "client_name": cname,
                        "stock": stk,
                        "kind": "qty_or_price_mismatch",
                        "severity": "high",
                        "title": "Holdings vs Open Trades Mismatch",
                        "summary": (
                            f"Holdings shows {int(round(cs_qty))} sh @ ₹{cs_avg}"
                            f"{' (duplicate rows)' if len(cs_list) > 1 else ''}, "
                            f"but open trades total {int(round(open_qty))} sh @ ₹{open_avg}."
                        ),
                        "holding_qty": int(round(cs_qty)),
                        "holding_buy_price": cs_avg,
                        "open_qty": int(round(open_qty)),
                        "open_avg_price": open_avg,
                        "trade_id": None,
                        "variants": [stk],
                        "has_closed_variant": False,
                        "suggested_action": "sync_holding",
                    })

        # 3. Unapplied closed/sell trades while open holdings exist (outside pending_reconciliations)
        for fam, cts in sorted(closed_by_family.items()):
            fam_open = open_by_family.get(fam, [])
            fam_shares = shares_by_family.get(fam, [])
            open_qty = sum((dec(t["quantity"] or 0) for t in fam_open), ZERO)
            cs_qty = sum((dec(s["quantity"] or 0) for s in fam_shares), ZERO)
            eff_open_qty = max(open_qty, cs_qty)
            if eff_open_qty <= ZERO:
                # Check for orphan sell-only trades without any buy price
                for ct in cts:
                    if ct.get("buy_price") is None:
                        _add({
                            "key": f"{cid}:sell_only_trade:{ct['id']}",
                            "client_id": cid,
                            "client_name": cname,
                            "stock": (ct["stock"] or "").strip().upper(),
                            "kind": "sell_only_trade",
                            "severity": "medium",
                            "title": "Sell Trade Missing Buy Price",
                            "summary": f"Trade #{ct['id']} ({int(round(dec(ct['quantity'] or 0)))} sh sold @ ₹{dec(ct['sell_price'])}) has no buy price and no open holding to pair with.",
                            "holding_qty": 0,
                            "holding_buy_price": None,
                            "open_qty": 0,
                            "open_avg_price": None,
                            "trade_id": ct["id"],
                            "variants": [(ct["stock"] or "").strip().upper()],
                            "has_closed_variant": True,
                            "suggested_action": "edit_trade",
                        })
                continue

            if fam_open:
                open_cost = sum((dec(t["quantity"] or 0) * dec(t["buy_price"] or 0) for t in fam_open), ZERO)
                avg_open = (open_cost / open_qty).quantize(CENT, rounding=ROUND_HALF_UP) if open_qty > ZERO else ZERO
                earliest_open_date = min((str(t.get("buy_date") or t.get("created_at") or "")[:10] for t in fam_open), default="")
            else:
                cs_cost = sum((dec(s["quantity"] or 0) * dec(s.get("buy_price") or 0) for s in fam_shares), ZERO)
                avg_open = (cs_cost / cs_qty).quantize(CENT, rounding=ROUND_HALF_UP) if cs_qty > ZERO else ZERO
                earliest_open_date = min((str(s.get("added_on") or "")[:10] for s in fam_shares), default="")

            for ct in cts:
                if ct["id"] in reconciled_ct_ids:
                    continue
                ct_notes = ct.get("notes") or ""
                if any(tag in ct_notes for tag in ("[Reconciled", "Reconciled Sale", "[Sold from Holdings]")):
                    continue
                ct_qty = dec(ct["quantity"] or 0)
                ct_bp = dec(ct["buy_price"]) if ct.get("buy_price") is not None else None
                ct_sp = dec(ct["sell_price"] or 0)
                ct_sell_date = str(ct.get("sell_date") or ct.get("created_at") or "")[:10]

                is_sell_only = ct_bp is None
                is_after_open = (not earliest_open_date or not ct_sell_date or ct_sell_date >= earliest_open_date)
                is_qty_match = int(round(ct_qty)) == int(round(eff_open_qty))
                is_bp_close = (ct_bp is not None and abs(ct_bp - avg_open) <= Decimal("0.50") and ct_qty <= eff_open_qty)
                is_direct_holding_sale = (not fam_open and cs_qty > ZERO and ct_qty <= cs_qty)

                if is_sell_only or (is_after_open and (is_qty_match or is_bp_close or is_direct_holding_sale)):
                    disp_stk = (ct["stock"] or "").strip().upper()
                    _add({
                        "key": f"{cid}:unapplied_sale:{ct['id']}",
                        "client_id": cid,
                        "client_name": cname,
                        "stock": disp_stk,
                        "kind": "unapplied_sale",
                        "severity": "high",
                        "title": "Sale Trade Not Deducted from Holdings",
                        "summary": (
                            f"Closed/sale Trade #{ct['id']} ({int(round(ct_qty))} sh sold @ ₹{ct_sp} on {ct_sell_date or '—'}) "
                            f"coexists with {int(round(eff_open_qty))} open shares in holdings."
                        ),
                        "holding_qty": int(round(cs_qty)),
                        "holding_buy_price": avg_open,
                        "open_qty": int(round(eff_open_qty)),
                        "open_avg_price": avg_open,
                        "trade_id": ct["id"],
                        "variants": [disp_stk],
                        "has_closed_variant": True,
                        "suggested_action": "apply_sale_fifo",
                    })

    return mismatches


def summarize_mismatches_by_client(mismatches, pending_reconciliations=None):
    """Group mismatches and pending reconciliations by client_id for admin overview banners."""
    by_client = {}
    for m in mismatches or []:
        cid = m["client_id"]
        entry = by_client.setdefault(cid, {
            "client_id": cid,
            "client_name": m["client_name"],
            "mismatch_count": 0,
            "reconcile_count": 0,
            "stocks": [],
        })
        entry["mismatch_count"] += 1
        if m["stock"] not in entry["stocks"]:
            entry["stocks"].append(m["stock"])

    for r in pending_reconciliations or []:
        cid = r["client_id"]
        entry = by_client.setdefault(cid, {
            "client_id": cid,
            "client_name": r["client_name"],
            "mismatch_count": 0,
            "reconcile_count": 0,
            "stocks": [],
        })
        entry["reconcile_count"] += 1
        if r["stock"] not in entry["stocks"]:
            entry["stocks"].append(r["stock"])

    summary_list = []
    for entry in by_client.values():
        entry["total_count"] = entry["mismatch_count"] + entry["reconcile_count"]
        summary_list.append(entry)
    summary_list.sort(key=lambda x: (-x["total_count"], x["client_name"].lower()))
    return summary_list


def repair_client_holdings_data(db=None):
    """Ensures Sanket (Client 11) has exactly SBC 1,880 shares @ 63.28 buy price,
    with 58 shares closed, and cleans up Adhish Mathur's fully liquidated PC JEWELLER holding."""
    if db is None:
        db = get_db()
    try:
        # 1. Sanket (Client 11)
        sanket = db.execute(
            "SELECT id, name FROM clients WHERE id = 11 OR lower(name) LIKE '%sanket%' LIMIT 1"
        ).fetchone()
        if sanket:
            sid = sanket["id"]
            open_sbc = db.execute(
                """SELECT id, quantity, buy_price FROM trades
                   WHERE client_id = ?
                     AND UPPER(TRIM(stock)) = 'SBC'
                     AND buy_price IS NOT NULL
                     AND (sell_price IS NULL OR sell_price = 0)""",
                (sid,)
            ).fetchall()

            tot_open = sum(dec(t["quantity"] or 0) for t in open_sbc)
            need_sbc_fix = (tot_open != dec("1880")) or (len(open_sbc) == 1 and dec(open_sbc[0]["buy_price"] or 0) != dec("63.28"))

            if need_sbc_fix:
                # Delete any open trades for SBC for Sanket (e.g. remnants of 18 or 40)
                db.execute(
                    """DELETE FROM trades
                       WHERE client_id = ?
                         AND UPPER(TRIM(stock)) = 'SBC'
                         AND (sell_price IS NULL OR sell_price = 0)""",
                    (sid,)
                )
                # Insert the exact 1880 shares @ 63.28 open trade
                db.execute(
                    """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source)
                       VALUES (?, 'SBC', 1880, 63.28, '2026-10-16', 'Holding position', 'manual')""",
                    (sid,)
                )

                # Ensure the 58 shares closed trade exists
                closed_sbc = db.execute(
                    """SELECT id, notes FROM trades
                       WHERE client_id = ?
                         AND UPPER(TRIM(stock)) = 'SBC'
                         AND sell_price IS NOT NULL AND sell_price > 0
                       ORDER BY id DESC LIMIT 1""",
                    (sid,)
                ).fetchone()
                if not closed_sbc:
                    db.execute(
                        """INSERT INTO trades (client_id, stock, quantity, buy_price, sell_price, buy_date, sell_date, notes, source)
                           VALUES (?, 'SBC', 58, 63.48, 69.60, '2026-10-16', '2026-11-12', '[Reconciled Sale]', 'manual')""",
                        (sid,)
                    )
                else:
                    c_notes = closed_sbc["notes"] or ""
                    if "[Reconciled Sale]" not in c_notes:
                        db.execute(
                            """UPDATE trades
                               SET quantity = 58, buy_price = 63.48, sell_price = 69.60,
                                   notes = notes || ' [Reconciled Sale]'
                               WHERE id = ?""",
                            (closed_sbc["id"],)
                        )

                # Update client_shares to SBC 1880 @ 63.28
                db.execute(
                    """DELETE FROM client_shares WHERE client_id = ? AND UPPER(TRIM(stock)) = 'SBC'""",
                    (sid,)
                )
                db.execute(
                    """INSERT INTO client_shares (client_id, stock, quantity, buy_price, added_on, deduct_from_funds)
                       VALUES (?, 'SBC', 1880, 63.28, '2026-10-16', 0)""",
                    (sid,)
                )

                # Ensure realized profit for 58 shares (+354.96) is in funds
                fund_row = db.execute(
                    """SELECT id FROM funds WHERE client_id = ? AND notes LIKE '%SBC (58 sh)%'""",
                    (sid,)
                ).fetchone()
                if not fund_row:
                    db.execute(
                        """INSERT INTO funds (client_id, amount, added_on, notes)
                           VALUES (?, 354.96, '2026-11-12', '[Realized Profit] SBC (58 sh) [Reconciled]')""",
                        (sid,)
                    )

                # Dismiss any reconciliation for Sanket SBC closed trades
                raw_dismissed = setting("dismissed_holding_reconciliations", "[]")
                try:
                    d_list = json.loads(raw_dismissed)
                    if not isinstance(d_list, list):
                        d_list = []
                except Exception:
                    d_list = []
                all_sbc_closed = db.execute(
                    """SELECT id FROM trades WHERE client_id = ? AND UPPER(TRIM(stock)) = 'SBC' AND sell_price IS NOT NULL""",
                    (sid,)
                ).fetchall()
                for c in all_sbc_closed:
                    if c["id"] not in d_list:
                        d_list.append(c["id"])
                set_setting("dismissed_holding_reconciliations", json.dumps(d_list))

        # 2. Adhish Mathur (Client 10)
        adhish = db.execute(
            "SELECT id, name FROM clients WHERE id = 10 OR lower(name) LIKE '%adhish%' LIMIT 1"
        ).fetchone()
        if adhish:
            aid = adhish["id"]
            pc_closed = db.execute(
                """SELECT id FROM trades
                   WHERE client_id = ? AND UPPER(TRIM(stock)) = 'PC JEWELLER'
                     AND sell_price IS NOT NULL AND sell_price > 0""",
                (aid,)
            ).fetchall()
            pc_open = db.execute(
                """SELECT id FROM trades
                   WHERE client_id = ? AND UPPER(TRIM(stock)) = 'PC JEWELLER'
                     AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price = 0)""",
                (aid,)
            ).fetchall()
            if pc_closed and not pc_open:
                db.execute(
                    """DELETE FROM client_shares WHERE client_id = ? AND UPPER(TRIM(stock)) = 'PC JEWELLER'""",
                    (aid,)
                )

            # Dismiss any SBC closed trades for Adhish Mathur
            raw_dismissed = setting("dismissed_holding_reconciliations", "[]")
            try:
                d_list = json.loads(raw_dismissed)
                if not isinstance(d_list, list):
                    d_list = []
            except Exception:
                d_list = []
            adhish_sbc_closed = db.execute(
                """SELECT id FROM trades WHERE client_id = ? AND UPPER(TRIM(stock)) = 'SBC' AND sell_price IS NOT NULL""",
                (aid,)
            ).fetchall()
            for c in adhish_sbc_closed:
                if c["id"] not in d_list:
                    d_list.append(c["id"])
            set_setting("dismissed_holding_reconciliations", json.dumps(d_list))

        db.commit()
    except Exception as e:
        current_app.logger.warning("repair_client_holdings_data error: %s", e)
        try:
            db.rollback()
        except Exception:
            pass



def rule_label(ctype, rate):
    if not ctype or rate is None:
        return "Default"
    r = f"{dec(rate):.1f}".rstrip('0').rstrip('.')
    if ctype == "flat":
        return f"{r} flat/trade"
    unit = COMMISSION_TYPES.get(ctype, f" {ctype}" if ctype else "")
    return f"{r}{unit}"


def client_values(values, name):
    ctype, rate = rule_values(values.get("commission_type"), values.get("commission_rate"), allow_default=True)
    raw_phone = values.get("phone")
    phone = clean_phone_number(raw_phone)
    if not phone or len(phone) != 10:
        raise ValidationError("Please provide a valid 10-digit mobile number.")
    return (
        name, phone,
        text(values.get("email"), "Email", limit=254) or None, ctype, rate,
        text(values.get("notes"), "Notes", multiline=True) or None,
        number(values.get("portfolio_amount"), "Portfolio amount", positive=True) if values.get("portfolio_amount") else ZERO,
    )


def upload_records(cid=None):
    sql = "SELECT u.*,c.name AS current_client_name FROM uploads u LEFT JOIN clients c ON c.id=u.client_id"
    parameters = ()
    if cid is not None:
        sql += " WHERE u.client_id=?"
        parameters = (cid,)
    rows = get_db().execute(sql + " ORDER BY u.id DESC", parameters).fetchall()
    for row in rows:
        row["entered_name"] = row["client_name"]
        row["client_name"] = row.pop("current_client_name") or row["client_name"]
    return rows


def sheet(worksheet, headers, rows):
    worksheet.append(headers)
    for row in rows:
        worksheet.append([
            ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value for value in row
        ])
        for cell, value in zip(worksheet[worksheet.max_row], row):
            if isinstance(value, str):
                cell.data_type = "s"
                cell.quotePrefix = True
    for cell in worksheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for column in worksheet.columns:
        width = max(len(str(cell.value or "")) for cell in column) + 2
        worksheet.column_dimensions[column[0].column_letter].width = min(max(width, 10), 40)
    worksheet.freeze_panes = "A2"


def workbook_response(workbook, filename):
    buffer = io.BytesIO()
    workbook.save(buffer)
    if current_app.config["PRODUCTION"] and buffer.tell() > 4 * 1024 * 1024:
        abort(413, description="This export exceeds the cloud response limit. Export a smaller user or date range.")
    buffer.seek(0)
    return send_file(
        buffer, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def json_object():
    if not request.is_json:
        abort(415, description="Send an application/json request.")
    data = request.get_json()
    if not isinstance(data, dict):
        raise ValidationError("The request body must be a JSON object.")
    return data


def owner_hash():
    if "_upload_owner" not in session:
        session["_upload_owner"] = secrets.token_urlsafe(32)
    return hashlib.sha256(session["_upload_owner"].encode()).hexdigest()


def scope_hash(mode):
    if mode == "admin":
        if not session.get("admin"):
            abort(403, description="Administrator authorization has expired.")
        source = "admin:" + admin_credentials()[1]
    elif mode == "portal":
        token = setting("upload_token", "")
        if not token:
            abort(403, description="This submission link is inactive.")
        source = "portal:" + token + ":" + setting("upload_code", "")
    else:
        abort(403, description="Invalid upload authorization.")
    return credential_version(current_app.secret_key, "upload-scope:" + source)


def authorize_upload(data):
    token = text(data.get("portal_token"), "Portal token", limit=128)
    code = text(data.get("code"), "Upload code", limit=128)
    if token:
        expected = setting("upload_token", "")
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            abort(403, description="This submission link is invalid or has been replaced.")
        expected_code = setting("upload_code", "")
        if expected_code and not hmac.compare_digest(code.encode(), expected_code.encode()):
            abort(403, description="Invalid upload code.")
        mode = "portal"
    elif session.get("admin"):
        mode = "admin"
    else:
        abort(403, description="Open your private submission link to upload trades.")
    return {"owner": owner_hash(), "scope": scope_hash(mode), "mode": mode}


def receipt_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="commission-upload-v1")


def payment_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="commission-pay-v1")


def decode_receipt(receipt):
    if not isinstance(receipt, str) or len(receipt) > 2048:
        raise ValidationError("A signed receipt is required for every image.")
    try:
        data = receipt_signer().loads(receipt, max_age=UPLOAD_TTL)
    except SignatureExpired as exc:
        raise ValidationError("The image upload authorization expired. Upload the image again.") from exc
    except BadSignature as exc:
        raise ValidationError("Invalid image upload receipt.") from exc
    if not isinstance(data, dict):
        raise ValidationError("Invalid image upload receipt.")
    return data


def receipt_asset(path, receipt, authorization=None):
    path = safe_path(path)
    decoded = decode_receipt(receipt)
    if authorization is None:
        authorization = {
            "owner": owner_hash(), "scope": scope_hash(decoded.get("mode")), "mode": decoded.get("mode"),
        }
    if (decoded.get("path") != path or decoded.get("owner") != authorization["owner"]
            or decoded.get("scope") != authorization["scope"]):
        abort(403, description="This image belongs to a different upload session or an expired link.")
    asset = get_db().execute("SELECT * FROM upload_assets WHERE path=?", (path,)).fetchone()
    if (not asset or asset["owner_hash"] != authorization["owner"]
            or asset["scope_hash"] != authorization["scope"] or asset["expires_at"] < int(time.time())):
        abort(403, description="This image upload authorization is no longer valid.")
    if asset["upload_id"] is not None:
        abort(409, description="This image has already been attached to a submission.")
    return asset


def prior_submission(submission_id, digest, owner):
    row = get_db().execute(
        "SELECT trade_count,submission_hash,submission_owner FROM uploads WHERE submission_id=?",
        (submission_id,),
    ).fetchone()
    if row:
        if row["submission_owner"] != owner or row["submission_hash"] != digest:
            return "CONFLICT"
        return jsonify(ok=True, saved=row["trade_count"], submission_id=submission_id)
    return None


def create_app(config=None):
    if (not (config and config.get("TESTING")) and os.environ.get("VERCEL") != "1"
            and os.environ.get("COMMISSION_ENV") != "production"):
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    root = str(Path(__file__).resolve().parents[1])
    app = Flask(__name__, root_path=root, template_folder="templates", static_folder="static")
    configure_app(app, config)
    install_database(app)
    app.after_request(secure_response)
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.context_processor
    def inject_admin_context():
        if session.get("admin"):
            global _active_clients_cache, _active_clients_time
            now = time.time()
            if _active_clients_cache is not None and (now - _active_clients_time < 60.0):
                return {"active_clients": _active_clients_cache}
            try:
                clients = get_db().execute("SELECT id, name FROM clients WHERE active=1 ORDER BY lower(name)").fetchall()
                _active_clients_cache = clients
                _active_clients_time = now
                return {"active_clients": clients}
            except Exception:
                return {}
        return {}

    @app.before_request
    def authorize_request():
        g.decimal_context = localcontext()
        g.decimal_context.__enter__().prec = 60
        if session.get("client_id") or session.get("admin"):
            session.permanent = True
        if session.get("admin"):
            _, version = admin_credentials()
            if not version or session.get("auth_version") != version:
                session.clear()
        require_csrf()
        try:
            if setting("repaired_sanket_sbc_1880") != "1":
                repair_client_holdings_data(get_db())
                set_setting("repaired_sanket_sbc_1880", "1")
                get_db().commit()
        except Exception:
            pass

    @app.teardown_request
    def restore_decimal_context(_error=None):
        context = g.pop("decimal_context", None)
        if context is not None:
            context.__exit__(None, None, None)

    @app.template_filter("money")
    def money(value):
        if value is None or value == "":
            return "\u2014"
        value = dec(value)
        symbol = setting("currency", "\u20b9")
        sign = "-" if value < 0 else ""
        whole, fraction = f"{abs(value):.2f}".split(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            groups = []
            while len(head) > 2:
                groups.insert(0, head[-2:])
                head = head[:-2]
            whole = ",".join(([head] if head else []) + groups + [tail])
        return f"{sign}{symbol}{whole}.{fraction}"

    @app.template_filter("num")
    def num(value):
        if value is None or value == "":
            return ""
        value = dec(value)
        return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.6f}".rstrip("0").rstrip(".")

    @app.template_filter("ist")
    def ist_filter(value, fmt="chip"):
        return format_ist(value, fmt)

    @app.template_filter("format_ist")
    def format_ist_filter(value, fmt="chip"):
        return format_ist(value, fmt)

    @app.context_processor
    def inject_globals():
        error = g.get("error_response", False)
        return {
            "COMMISSION_TYPES": COMMISSION_TYPES, "logged_in": bool(session.get("admin")),
            "is_admin": bool(session.get("admin")),
            "is_client": bool(session.get("client_id")),
            "client_name": session.get("client_name"),
            "client_id": session.get("client_id"),
            "today": date.today().isoformat(), "app_name": "Alder & Vane",
            "business_name": "" if error else setting("business_name", ""),
            "currency": "\u20b9" if error else setting("currency", "\u20b9"),
            "backend_mode": app.config["BACKEND_MODE"],
            "deployed": app.config["DEPLOYED"],
            "password_managed": bool(app.config["ADMIN_PASSWORD_HASH"]),
            "admin_password_managed": bool(app.config["ADMIN_PASSWORD_HASH"]),
            "commission_policy": "snapshot",
            "upload_url": portal_url() if session.get("admin") and not error else None,
            "all_stock_suggestions": [],
            "known_stocks": [],
        }

    def error_response(status, title, message):
        if request.path.startswith("/api/"):
            return jsonify(error=message), status
        g.error_response = True
        return render_template("error.html", status=status, title=title, message=message), status

    @app.errorhandler(ValidationError)
    def invalid_input(error):
        return error_response(400, "Please check your entry", str(error))

    @app.errorhandler(HTTPException)
    def http_error(error):
        message = error.description
        if error.code == 413 and request.method != "GET":
            message = "The request is too large. Upload images separately (4 MB locally; 10 MB directly to cloud storage)."
        return error_response(error.code, error.name, message)

    @app.errorhandler(StorageUnavailable)
    def unavailable_storage(error):
        return error_response(503, "Private storage unavailable", str(error))

    @app.errorhandler(sqlite3.IntegrityError)
    def database_conflict(error):
        app.logger.warning("A database constraint rejected a request (%s).", type(error).__name__)
        return error_response(409, "Conflicting entry", "This entry conflicts with existing data. Reload and try again.")

    if psycopg is not None:
        app.errorhandler(psycopg.IntegrityError)(database_conflict)

    @app.errorhandler(sqlite3.OperationalError)
    def unavailable_database(error):
        app.logger.error("Database operation failed (%s).", type(error).__name__)
        return error_response(503, "Ledger unavailable", "The database is unavailable. Try again shortly or check its configuration.")

    if psycopg is not None:
        app.errorhandler(psycopg.OperationalError)(unavailable_database)

    @app.errorhandler(500)
    def server_error(_error):
        return error_response(500, "Something went wrong", "Your request could not be completed. Please try again.")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("admin"):
            return redirect(url_for("dashboard"))
        if session.get("client_id"):
            return redirect(url_for("client_portal"))
        password_hash, version = admin_credentials()
        first_run = not password_hash
        if first_run and not local_setup_allowed():
            abort(503, description="An administrator must configure the password on the server before this site can be used.")
        if request.method == "POST":
            login_type = request.form.get("login_type", "admin")
            if login_type == "client":
                identifier = request.form.get("identifier", "").strip()
                clean_ident = identifier.lstrip("@").strip()
                clean_phone = clean_phone_number(identifier)
                password = request.form.get("client_password", "")
                if not identifier or not password:
                    flash("Please provide your ID/phone and password.", "error")
                    return render_template("login.html", first_run=first_run, active_tab="client", public_token=portal_token()), 400
                client = get_db().execute(
                    """SELECT * FROM clients 
                    WHERE (phone=? OR phone=? OR lower(phone)=lower(?) OR lower(email)=lower(?) OR lower(username)=lower(?) OR lower(username)=lower(?) OR lower(name)=lower(?) OR CAST(id AS TEXT)=?) 
                      AND active=1""",
                    (identifier, clean_phone, identifier, identifier, identifier, clean_ident, identifier, identifier),
                ).fetchone()
                if not client or not client.get("password_hash"):
                    flash("Invalid credentials or client password has not been configured yet.", "error")
                    return render_template("login.html", first_run=first_run, active_tab="client", public_token=portal_token()), 401
                if not check_password_hash(client["password_hash"], password):
                    flash("Incorrect password.", "error")
                    return render_template("login.html", first_run=first_run, active_tab="client", public_token=portal_token()), 401
                session.clear()
                session["client_id"] = client["id"]
                session["client_name"] = client["name"]
                session["role"] = "client"
                session.permanent = True
                return redirect(safe_redirect_target(request.args.get("next"), url_for("client_portal")))
            else:
                password = request.form.get("password", "")
                if len(password) > 1024:
                    raise ValidationError("Password is too long.")
                if first_run:
                    if len(password) < 10 or password != request.form.get("confirm"):
                        flash("Password must be at least 10 characters and match confirmation.", "error")
                        return render_template("login.html", first_run=True, active_tab="admin"), 400
                    password_hash = generate_password_hash(password)
                    set_setting("admin_password", password_hash)
                    version = admin_credentials()[1]
                elif not check_password_hash(password_hash, password):
                    flash("Incorrect password.", "error")
                    return render_template("login.html", first_run=False, active_tab="admin"), 401
                session.clear()
                session["admin"], session["auth_version"] = True, version
                session.permanent = True
                return redirect(safe_redirect_target(request.args.get("next"), url_for("dashboard")))
        return render_template("login.html", first_run=first_run, active_tab=request.args.get("tab", "options"), public_token=portal_token())

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        args = {key: request.args.get(key, "") for key in ("from", "to")}
        start = valid_date(args.get("from"), "From date")
        end = valid_date(args.get("to"), "To date")
        if start and end and start > end:
            raise ValidationError("From date cannot be after to date.")

        # 1. Per-client aggregation directly in SQL (O(1) query instead of O(clients * trades) loop)
        per_client = aggregate_clients_sql(start=start, end=end)
        paid = payments_by_client()

        # All-time commission for calculating outstanding balance (all-time earned - all-time paid)
        if any(args.values()):
            all_time_clients = aggregate_clients_sql()
            all_earned = {c["id"]: c["commission"] for c in all_time_clients}
        else:
            all_earned = {c["id"]: c["commission"] for c in per_client}

        for c in per_client:
            client_paid = paid.get(c["id"], ZERO)
            c["paid"] = client_paid
            c["outstanding"] = all_earned.get(c["id"], ZERO) - client_paid

        per_client.sort(key=lambda value: value["commission"], reverse=True)

        total = {
            "count": sum((c["count"] for c in per_client), 0),
            "closed": sum((c["closed"] for c in per_client), 0),
            "open": sum((c["open"] for c in per_client), 0),
            "pnl": sum((c["pnl"] for c in per_client), ZERO),
            "commission": sum((c["commission"] for c in per_client), ZERO),
            "invested_open": sum((c["invested_open"] for c in per_client), ZERO),
            "wins": sum((c["wins"] for c in per_client), 0),
            "losses": sum((c["losses"] for c in per_client), 0),
            "turnover": sum((c["turnover"] for c in per_client), ZERO),
            "paid": sum(paid.values(), ZERO),
            "outstanding": sum((c["outstanding"] for c in per_client), ZERO),
        }

        trades = query_trades(args)
        monthly, stocks = {}, {}
        for trade in trades:
            if trade["status"] == "Closed":
                month = (trade["sell_date"] or trade["buy_date"] or "")[:7] or "Unknown"
                entry = monthly.setdefault(month, {"commission": ZERO, "pnl": ZERO})
                entry["commission"] += trade["commission"]
                entry["pnl"] += trade["pnl"]
            stock = trade["stock"].upper()
            entry = stocks.setdefault(stock, {"stock": stock, "count": 0, "pnl": ZERO, "commission": ZERO})
            entry["count"] += 1
            entry["pnl"] += trade["pnl"]
            entry["commission"] += trade["commission"]
        months = sorted(monthly)
        positive = [client for client in per_client if client["commission"] > 0]
        chart = {
            "months": months, "currency": setting("currency", "\u20b9"),
            "commission": [float(round(monthly[month]["commission"], 2)) for month in months],
            "pnl": [float(round(monthly[month]["pnl"], 2)) for month in months],
            "clients": [client["name"] for client in positive],
            "client_commission": [float(round(client["commission"], 2)) for client in positive],
        }
        pending_recs = get_pending_holding_reconciliations(get_db())
        holding_mismatches = get_trades_holdings_mismatches(get_db(), pending_recs=pending_recs)
        client_mismatch_summary = summarize_mismatches_by_client(holding_mismatches, pending_recs)
        return render_template(
            "dashboard.html", total=total, per_client=per_client, chart=chart,
            top_stocks=sorted(stocks.values(), key=lambda stock: stock["commission"], reverse=True)[:10],
            recent=trades[:8], args=args,
            client_mismatch_summary=client_mismatch_summary,
        )

    @app.route("/trades")
    @login_required
    def trades_list():
        args = {key: request.args.get(key, "") for key in ("client_id", "stock", "from", "to", "status")}
        all_trades = query_trades(args)
        total = summarize(all_trades)

        page = request.args.get("page", 1)
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
        per_page = 25
        total_trades = len(all_trades)
        total_pages = max(1, math.ceil(total_trades / per_page)) if total_trades else 1
        if page > total_pages:
            page = total_pages
        start_idx = (page - 1) * per_page
        trades_page = all_trades[start_idx : start_idx + per_page]

        clients = get_db().execute("SELECT id,name FROM clients ORDER BY lower(name)").fetchall()
        pending_reconciliations = get_pending_holding_reconciliations(get_db())
        return render_template(
            "trades.html",
            trades=trades_page,
            total=total,
            clients=clients,
            page=page,
            total_pages=total_pages,
            total_count=total_trades,
            per_page=per_page,
            args=args,
            pending_reconciliations=pending_reconciliations,
        )

    @app.route("/trades/new", methods=["GET", "POST"])
    @app.route("/trades/<int:tid>/edit", methods=["GET", "POST"])
    @login_required
    def trade_form(tid=None):
        trade = get_db().execute(TRADE_SELECT + " WHERE t.id=?", (tid,)).fetchone() if tid else None
        if tid and not trade:
            abort(404)
        if request.method == "POST":
            values = trade_values(request.form)
            name = client_name(request.form.get("client_name"))
            cid = get_or_create_client(name)
            if tid:
                get_db().execute(
                    """UPDATE trades SET client_id=?,stock=?,quantity=?,buy_price=?,sell_price=?,
                    buy_date=?,sell_date=?,commission_override=?,notes=?,deduct_from_funds=? WHERE id=?""",
                    (cid, values["stock"], values["quantity"], values["buy_price"], values["sell_price"],
                     values["buy_date"], values["sell_date"], values["commission_override"], values["notes"],
                     values.get("deduct_from_funds", 0), tid),
                )
                sync_trade_profits_to_funds(get_db(), cid)
                sync_client_holdings_from_trades(cid, get_db())
                invalidate_stock_caches()
                flash("Trade updated.", "success")
            else:
                db = get_db()
                if values.get("sell_price") is not None:
                    _, variants = get_matching_stock_variants(db, values["stock"], include_known_tickers=True)
                    in_sql, in_params = _stock_in_clause(variants)
                    open_trades_exist = db.execute(
                        f"""SELECT 1 FROM trades
                            WHERE client_id=? AND {in_sql} AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0) LIMIT 1""",
                        (cid, *in_params)
                    ).fetchone()
                    open_shares_exist = db.execute(
                        f"SELECT 1 FROM client_shares WHERE client_id=? AND {in_sql} AND quantity > 0 LIMIT 1",
                        (cid, *in_params)
                    ).fetchone()
                    if open_trades_exist or open_shares_exist:
                        apply_sale_fifo(
                            client_id=cid,
                            stock=values["stock"],
                            sell_quantity=values["quantity"],
                            sell_price=values["sell_price"],
                            sell_date=values.get("sell_date"),
                            buy_price_hint=values.get("buy_price"),
                            notes=values.get("notes"),
                            source="manual",
                            db=db,
                        )
                    else:
                        insert_trade(cid, values)
                else:
                    insert_trade(cid, values)
                sync_trade_profits_to_funds(get_db(), cid)
                sync_client_holdings_from_trades(cid, get_db())
                invalidate_stock_caches()
                flash("Trade added.", "success")
            back = request.form.get("back")
            if not back and cid:
                if request.referrer and f"/clients/{cid}" in request.referrer:
                    back = url_for("client_detail", cid=cid)
                elif request.args.get("client"):
                    back = url_for("client_detail", cid=cid)
            if request.form.get("add_another"):
                return redirect(url_for("trade_form", client=name, back=back))
            default_target = url_for("client_detail", cid=cid) if (back and f"/clients/{cid}" in back) else url_for("trades_list")
            return redirect(safe_redirect_target(back, default_target))
        clients = get_db().execute("SELECT name FROM clients ORDER BY lower(name)").fetchall()
        back_url = request.args.get("back") or safe_redirect_target(request.referrer, url_for("trades_list"))
        preset = request.args.get("client", "")
        if not request.args.get("back") and preset and back_url == url_for("trades_list"):
            c_row = get_db().execute("SELECT id FROM clients WHERE lower(name)=lower(?)", (preset,)).fetchone()
            if c_row:
                back_url = url_for("client_detail", cid=c_row["id"])
        return render_template(
            "trade_form.html", trade=trade, clients=clients,
            preset_client=preset,
            back=back_url,
        )

    @app.route("/trades/<int:tid>/delete", methods=["POST"])
    @login_required
    def trade_delete(tid):
        db = get_db()
        trade = db.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if not trade:
            abort(404)
        client_id = trade["client_id"]
        was_open = trade.get("sell_price") is None or dec(trade.get("sell_price") or 0) == ZERO
        db.execute("DELETE FROM trades WHERE id=?", (tid,))
        db.execute("DELETE FROM funds WHERE client_id=? AND notes LIKE ?", (client_id, f"%[Trade #{tid}]%"))
        if was_open and trade.get("stock"):
            _, variants = get_matching_stock_variants(db, trade["stock"], include_known_tickers=False)
            in_sql, in_params = _stock_in_clause(variants)
            still_open = db.execute(
                f"SELECT 1 FROM trades WHERE client_id=? AND {in_sql} AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0) LIMIT 1",
                (client_id, *in_params),
            ).fetchone()
            if not still_open:
                db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (client_id, *in_params))
        sync_client_holdings_from_trades(client_id, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        flash("Trade deleted.", "success")
        back = request.form.get("back") or request.referrer
        from_client = request.form.get("from_client")
        if from_client or (back and f"/clients/{client_id}" in back):
            default_target = url_for("client_detail", cid=client_id)
        else:
            default_target = url_for("trades_list")
        return redirect(safe_redirect_target(back, default_target))

    @app.route("/trades/<int:tid>/delete-screenshot", methods=["POST"])
    @login_required
    def trade_delete_screenshot(tid):
        db = get_db()
        trade = db.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if not trade:
            abort(404)
        upload_id = trade["upload_id"]
        if not upload_id:
            flash("No screenshot attached to this trade.", "info")
            return redirect(safe_redirect_target(request.referrer, url_for("trades_list")))

        stock_name = trade["stock"]
        db.execute("UPDATE trades SET upload_id=NULL WHERE id=?", (tid,))

        other_trades = db.execute("SELECT COUNT(*) AS c FROM trades WHERE upload_id=?", (upload_id,)).fetchone()
        count = other_trades["c"] if other_trades else 0
        if count == 0:
            try:
                assets = db.execute("SELECT path FROM upload_assets WHERE upload_id=?", (upload_id,)).fetchall()
                paths = [a["path"] for a in assets if a and a["path"]]
                if paths:
                    try:
                        storage = get_storage()
                        storage.remove(paths)
                    except Exception as storage_exc:
                        current_app.logger.warning("Failed to remove storage files for upload %s: %s", upload_id, storage_exc)
                db.execute("DELETE FROM upload_assets WHERE upload_id=?", (upload_id,))
                db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
            except Exception as exc:
                current_app.logger.warning("Error cleaning up orphaned upload record %s: %s", upload_id, exc)

        db.commit()
        flash(f"Proof screenshot removed from {stock_name}.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("trades_list")))

    @app.route("/trades/<int:tid>/close", methods=["POST"])
    @login_required
    def trade_close(tid):
        trade = get_db().execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if not trade:
            abort(404)
        if trade["buy_price"] is None:
            raise ValidationError("Add the opening buy price and date before closing this position.")
        if trade["sell_price"] is not None and dec(trade["sell_price"]) > ZERO:
            abort(409, description="This position is already closed. Use Edit to correct it.")
        trade.update(
            sell_price=number(request.form.get("sell_price"), "Sell price", required=True),
            sell_date=valid_date(request.form.get("sell_date"), "Sell date", required=True),
        )
        values = trade_values(trade)
        client_id = trade["client_id"]
        get_db().execute(
            "UPDATE trades SET sell_price=?,sell_date=? WHERE id=?",
            (values["sell_price"], values["sell_date"], tid),
        )
        # Record realized profit to bank funds once
        pnl = ((dec(values["sell_price"]) - dec(trade["buy_price"])) * dec(trade["quantity"])).quantize(CENT, rounding=ROUND_HALF_UP)
        if pnl != ZERO:
            tag = f"[Trade #{tid}]"
            existing = get_db().execute(
                "SELECT id FROM funds WHERE client_id=? AND notes LIKE ?",
                (client_id, f"%{tag}%")
            ).fetchone()
            if not existing:
                pnl_note = f"[Realized {'Profit' if pnl > 0 else 'Loss'}] {trade['stock']} ({trade['quantity']} sh) {tag}"
                get_db().execute(
                    "INSERT INTO funds (client_id, amount, added_on, notes) VALUES (?, ?, ?, ?)",
                    (client_id, pnl, values["sell_date"], pnl_note),
                )
        sync_client_holdings_from_trades(client_id, get_db())
        invalidate_stock_caches()
        invalidate_prices_cache()
        get_db().commit()
        flash("Position closed and profit added to bank funds.", "success")
        back = request.form.get("back") or request.referrer
        from_client = request.form.get("from_client")
        if from_client or (back and f"/clients/{client_id}" in back):
            default_target = url_for("client_detail", cid=client_id)
        else:
            default_target = url_for("trades_list")
        return redirect(safe_redirect_target(back, default_target))

    @app.route("/trades/bulk-delete", methods=["POST"])
    @login_required
    def trades_bulk_delete():
        ids = list(dict.fromkeys(identifier(value, "Trade") for value in request.form.getlist("ids")))
        if not ids:
            raise ValidationError("Choose at least one trade to delete.")
        if len(ids) > MAX_ROWS:
            raise ValidationError(f"Delete at most {MAX_ROWS} trades at once.")
        db = get_db()
        placeholders = ",".join("?" for _ in ids)
        deleted_rows = db.execute(
            f"SELECT id, client_id, stock, sell_price FROM trades WHERE id IN ({placeholders})", ids
        ).fetchall()
        for tid in ids:
            db.execute("DELETE FROM funds WHERE notes LIKE ?", (f"%[Trade #{tid}]%",))
        count = db.execute(
            f"DELETE FROM trades WHERE id IN ({placeholders})", ids
        ).rowcount
        for row in deleted_rows:
            if row.get("stock") and (row.get("sell_price") is None or dec(row.get("sell_price") or 0) == ZERO):
                _, variants = get_matching_stock_variants(db, row["stock"], include_known_tickers=False)
                in_sql, in_params = _stock_in_clause(variants)
                still_open = db.execute(
                    f"SELECT 1 FROM trades WHERE client_id=? AND {in_sql} AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0) LIMIT 1",
                    (row["client_id"], *in_params),
                ).fetchone()
                if not still_open:
                    db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (row["client_id"], *in_params))
        sync_client_holdings_from_trades(None, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        flash(f"Deleted {count} trade(s).", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("trades_list")))

    @app.route("/export/trades.xlsx")
    @login_required
    def export_trades():
        args = {key: request.args.get(key, "") for key in ("client_id", "stock", "from", "to", "status")}
        trades = query_trades(args)
        workbook = Workbook()
        workbook.active.title = "Trades"
        sheet(workbook.active, [title for title, _ in EXPORT_COLS],
              [[trade[key] for _, key in EXPORT_COLS] for trade in trades])
        selected = identifier(args["client_id"]) if args["client_id"] else None
        clients = [get_client(selected)] if selected else get_db().execute(
            "SELECT * FROM clients ORDER BY lower(name)"
        ).fetchall()
        all_trades = query_trades({"client_id": selected}) if selected else query_trades({})
        paid, rows = payments_by_client(), []
        for client in clients:
            cid = client["id"]
            summary = summarize([trade for trade in trades if trade["client_id"] == cid])
            all_earned = sum((trade["commission"] for trade in all_trades if trade["client_id"] == cid), ZERO)
            received = paid.get(cid, ZERO)
            rows.append([
                client["name"], summary["count"], summary["closed"], summary["open"], summary["pnl"],
                summary["commission"], all_earned, received, all_earned - received,
            ])
        sheet(
            workbook.create_sheet("Summary"),
            ["Username", "Trades (filtered)", "Closed (filtered)", "Open (filtered)", "P&L (filtered)",
             "Commission (filtered)", "Commission (all time)", "Paid (all time)", "Balance (all time)"],
            rows,
        )
        sql = "SELECT c.name,p.amount,p.paid_on,p.mode,p.notes FROM payments p JOIN clients c ON c.id=p.client_id"
        payments = get_db().execute(
            sql + (" WHERE p.client_id=?" if selected else "") + " ORDER BY p.paid_on DESC,p.id DESC",
            (selected,) if selected else (),
        ).fetchall()
        sheet(
            workbook.create_sheet("Payments"), ["Username", "Amount (all time)", "Paid On", "Mode", "Notes"],
            [[payment[key] for key in ("name", "amount", "paid_on", "mode", "notes")] for payment in payments],
        )
        return workbook_response(workbook, f"trades_{date.today().isoformat()}.xlsx")

    @app.route("/export/template.xlsx")
    @login_required
    def export_template():
        workbook = Workbook()
        sheet(
            workbook.active,
            ["Username", "Stock", "Quantity", "Buy Price", "Sell Price", "Buy Date", "Sell Date", "Commission", "Notes"],
            [["Ravi", "TATAMOTORS", 10, 950.25, 1010, "2024-09-12", "2024-09-20", "", "optional"]],
        )
        return workbook_response(workbook, "import_template.xlsx")

    @app.route("/import", methods=["POST"])
    @login_required
    def import_trades():
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename.lower().endswith(".xlsx"):
            raise ValidationError("Choose an .xlsx file.")
        try:
            with ZipFile(uploaded.stream) as archive:
                if len(archive.infolist()) > 5000 or sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                    raise ValidationError("This workbook is too large after decompression.")
            uploaded.stream.seek(0)
            workbook = load_workbook(uploaded.stream, read_only=True, data_only=False)
            try:
                worksheet = workbook.active
                if worksheet is None:
                    raise ValidationError("The workbook has no active worksheet.")
                if worksheet.max_column and worksheet.max_column > 100:
                    raise ValidationError("The import worksheet must contain at most 100 columns.")
                rows = []
                for row in worksheet.iter_rows():
                    if len(rows) > MAX_ROWS:
                        raise ValidationError(f"Import at most {MAX_ROWS} rows per workbook.")
                    if any(cell.data_type == "f" for cell in row):
                        raise ValidationError("Formula cells are not accepted. Paste their values before importing.")
                    rows.append([cell.value for cell in row])
            finally:
                workbook.close()
        except ValidationError:
            raise
        except (BadZipFile, InvalidFileException, ParseError, KeyError, OSError, ValueError) as exc:
            raise ValidationError("Could not read that Excel workbook.") from exc
        if len(rows) < 2:
            raise ValidationError("The workbook contains no trade rows.")
        headers = [str(value or "").strip().lower() for value in rows[0]]
        aliases = {
            "client_name": ("username", "user", "client", "name"),
            "stock": ("stock", "stock name", "symbol", "scrip"), "quantity": ("quantity", "qty"),
            "buy_price": ("buy price", "buy", "buy avg"), "sell_price": ("sell price", "sell", "sell avg"),
            "buy_date": ("buy date",), "sell_date": ("sell date",),
            "commission_override": ("commission",), "notes": ("notes", "remarks"),
        }
        indexes = {key: next((headers.index(alias) for alias in names if alias in headers), None)
                   for key, names in aliases.items()}
        if any(indexes[key] is None for key in ("client_name", "stock", "quantity")):
            raise ValidationError("The sheet needs Username, Stock, and Quantity columns.")
        validated = []
        for index, row in enumerate(rows[1:], 2):
            if all(value is None or value == "" for value in row):
                continue
            values = {key: row[position] if position is not None and position < len(row) else None
                      for key, position in indexes.items()}
            try:
                validated.append((client_name(values["client_name"]), trade_values(values)))
            except ValidationError as exc:
                raise ValidationError(f"Row {index}: {exc} No rows were imported.") from exc
        if not validated:
            raise ValidationError("The workbook contains no populated trade rows.")
        grouped = {}
        for name, values in validated:
            grouped.setdefault(name, []).append(values)
        for name, values in grouped.items():
            cid = get_or_create_client(name)
            insert_trades(cid, values, source="import")
            sync_trade_profits_to_funds(get_db(), cid)
            sync_client_holdings_from_trades(cid, get_db())
        invalidate_stock_caches()
        flash(f"Imported {len(validated)} trade(s).", "success")
        return redirect(url_for("trades_list"))

    @app.route("/holdings")
    @login_required
    def holdings_list():
        stock_query = request.args.get("stock", "").strip().upper()
        db = get_db()
        sync_client_holdings_from_trades(None, db)

        base_query = """
            SELECT h.id, h.stock, c.name as client_name, c.id as client_id, h.quantity, h.buy_price, h.added_on
            FROM client_shares h 
            JOIN clients c ON c.id = h.client_id 
            WHERE h.quantity > 0
        """

        if stock_query:
            raw_holdings = db.execute(
                base_query + " AND UPPER(h.stock) LIKE ? ORDER BY h.stock, lower(c.name)", 
                (f"%{stock_query}%",)
            ).fetchall()
        else:
            raw_holdings = db.execute(
                base_query + " ORDER BY h.stock, lower(c.name)"
            ).fetchall()

        alias_map = get_alias_mapping(db)
        open_trades = db.execute(
            """SELECT id, client_id, stock, quantity, buy_price,
                      COALESCE(buy_date, created_at) as added_on,
                      COALESCE(deduct_from_funds, 0) as deduct_from_funds, notes, source
               FROM trades
               WHERE buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0)
               ORDER BY COALESCE(buy_date, created_at) ASC, id ASC"""
        ).fetchall()
        lots_map = {}
        for ot in open_trades:
            raw_s = (ot["stock"] or "").strip().upper()
            stk = alias_map.get(raw_s, raw_s)
            k = (ot["client_id"], stk)
            if k not in lots_map:
                lots_map[k] = []
            ot_qty = dec(ot["quantity"] or 0)
            ot_bp = dec(ot["buy_price"] or 0)
            lots_map[k].append({
                "id": ot["id"],
                "quantity": int(round(ot_qty)),
                "buy_price": ot_bp.quantize(CENT, rounding=ROUND_HALF_UP),
                "total_cost": (ot_qty * ot_bp).quantize(CENT, rounding=ROUND_HALF_UP),
                "added_on": str(ot["added_on"])[:10] if ot.get("added_on") else "",
                "notes": ot.get("notes") or "",
            })

        prices = get_cached_prices(db)
        closed_pairs = set()
        try:
            closed_rows = db.execute(
                "SELECT DISTINCT client_id, stock FROM trades WHERE sell_price IS NOT NULL AND sell_price > 0"
            ).fetchall()
            for cr in closed_rows:
                rs = (cr["stock"] or "").strip().upper()
                if rs:
                    closed_pairs.add((cr["client_id"], alias_map.get(rs, rs)))
        except Exception:
            pass

        holdings = []
        total_invested = ZERO
        total_current_value = ZERO

        for h in raw_holdings:
            item = dict(h)
            qty = dec(item.get("quantity"))
            buy_p = dec(item.get("buy_price"))
            cost = qty * buy_p

            stock_sym = (item.get("stock") or "").strip().upper()
            canon_sym = alias_map.get(stock_sym, stock_sym)
            k = (item["client_id"], canon_sym)
            stock_lots = lots_map.get(k, [])
            if not stock_lots:
                if k in closed_pairs:
                    # Stale holding already closed in ledger — clean it up!
                    try:
                        db.execute("DELETE FROM client_shares WHERE id=?", (item["id"],))
                        db.commit()
                    except Exception:
                        pass
                    continue
                stock_lots = [{
                    "id": item["id"],
                    "quantity": int(round(qty)),
                    "buy_price": buy_p.quantize(CENT, rounding=ROUND_HALF_UP),
                    "total_cost": cost.quantize(CENT, rounding=ROUND_HALF_UP),
                    "added_on": str(item["added_on"])[:10] if item.get("added_on") else "",
                    "notes": "Direct holding",
                }]
            total_invested += cost
            item["quantity"] = int(round(qty))
            item["lots"] = stock_lots
            item["lots_count"] = len(stock_lots)

            quote = prices.get(stock_sym) or prices.get(canon_sym)
            if quote:
                cmp = quote["current_price"]
                cur_val = qty * cmp
                unrealized = cur_val - cost
                unrealized_pct = (unrealized / cost * 100) if cost else ZERO
                item.update(
                    cmp=cmp,
                    current_value=cur_val,
                    unrealized_pnl=unrealized,
                    unrealized_pct=unrealized_pct,
                    price_change=quote.get("change", ZERO),
                    price_change_pct=quote.get("change_pct", ZERO),
                    price_updated_at=quote.get("updated_at"),
                )
                total_current_value += cur_val
            else:
                item.update(
                    cmp=None,
                    current_value=cost,
                    unrealized_pnl=ZERO,
                    unrealized_pct=ZERO,
                    price_change=ZERO,
                    price_change_pct=ZERO,
                    price_updated_at=None,
                )
                total_current_value += cost
            holdings.append(item)

        total_unrealized_pnl = total_current_value - total_invested
        total_unrealized_pct = (total_unrealized_pnl / total_invested * 100) if total_invested else ZERO

        pending_recs = get_pending_holding_reconciliations(get_db())
        holding_mismatches = get_trades_holdings_mismatches(get_db(), pending_recs=pending_recs)
        client_mismatch_summary = summarize_mismatches_by_client(holding_mismatches, pending_recs)

        return render_template(
            "holdings.html",
            holdings=holdings,
            query=stock_query,
            total_invested=total_invested,
            total_current_value=total_current_value,
            total_unrealized_pnl=total_unrealized_pnl,
            total_unrealized_pct=total_unrealized_pct,
            known_stocks=get_known_stocks(get_db()),
            client_entered_stocks=get_client_entered_stocks(get_db()),
            pending_reconciliations=pending_recs,
            client_mismatch_summary=client_mismatch_summary,
        )

    @app.route("/charts")
    @login_required
    def charts_view():
        db = get_db()
        stocks = get_all_ledger_stocks_summary(db)
        selected_stock = request.args.get("stock", "").strip().upper()
        if not selected_stock and stocks:
            selected_stock = stocks[0]["symbol"]

        alias_map = get_alias_mapping(db)
        canonical_selected = alias_map.get(selected_stock, selected_stock) if selected_stock else ""

        selected_info = None
        for s in stocks:
            if s["symbol"] == canonical_selected or s["symbol"] == selected_stock:
                selected_info = s
                break
        if not selected_info and stocks:
            selected_info = stocks[0]
            canonical_selected = selected_info["symbol"]
            selected_stock = canonical_selected

        enriched_clients_holding = []
        if canonical_selected:
            try:
                ch_rows = db.execute(
                    """SELECT c.id as client_id, c.name as client_name, cs.stock, cs.quantity, cs.buy_price
                       FROM client_shares cs
                       JOIN clients c ON c.id = cs.client_id
                       WHERE c.active = 1 AND cs.quantity > 0"""
                ).fetchall()
                cmp = selected_info.get("current_price") if selected_info else None
                for r in ch_rows:
                    r_stk = alias_map.get((r["stock"] or "").strip().upper(), (r["stock"] or "").strip().upper())
                    if r_stk == canonical_selected:
                        q = dec(r["quantity"] or 0)
                        bp = dec(r["buy_price"] or 0)
                        invested = (q * bp).quantize(CENT, rounding=ROUND_HALF_UP)
                        cur_val = (q * cmp).quantize(CENT, rounding=ROUND_HALF_UP) if cmp else invested
                        pnl = cur_val - invested
                        pnl_pct = (pnl / invested * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if invested else ZERO
                        enriched_clients_holding.append({
                            "client_id": r["client_id"],
                            "client_name": r["client_name"],
                            "quantity": int(round(q)),
                            "buy_price": bp,
                            "invested": invested,
                            "current_value": cur_val,
                            "unrealized_pnl": pnl,
                            "unrealized_pct": pnl_pct,
                        })
            except Exception as exc:
                current_app.logger.warning("Error fetching client holdings for stock %s: %s", canonical_selected, exc)

        stock_trades = []
        if canonical_selected:
            try:
                all_t = db.execute(
                    """SELECT t.*, c.name as client_name
                       FROM trades t
                       JOIN clients c ON c.id = t.client_id
                       ORDER BY COALESCE(t.sell_date, t.buy_date, t.created_at) DESC LIMIT 25"""
                ).fetchall()
                for t in all_t:
                    t_stk = alias_map.get((t["stock"] or "").strip().upper(), (t["stock"] or "").strip().upper())
                    if t_stk == canonical_selected:
                        t_dict = dict(t)
                        t_qty = dec(t["quantity"] or 0)
                        t_bp = dec(t["buy_price"] or 0) if t.get("buy_price") is not None else None
                        t_sp = dec(t["sell_price"] or 0) if t.get("sell_price") is not None else None
                        t_pnl = None
                        if t_bp is not None and t_sp is not None:
                            t_pnl = ((t_sp - t_bp) * t_qty).quantize(CENT, rounding=ROUND_HALF_UP)
                        t_dict.update({
                            "quantity": int(round(t_qty)),
                            "buy_price": t_bp,
                            "sell_price": t_sp,
                            "realized_pnl": t_pnl,
                            "status": "Closed" if t_sp is not None else "Open",
                        })
                        stock_trades.append(t_dict)
            except Exception as exc:
                current_app.logger.warning("Error fetching trades for stock %s: %s", canonical_selected, exc)

        stock_targets = []
        if canonical_selected:
            try:
                raw_targets = db.execute(
                    "SELECT * FROM stock_price_targets WHERE stock=? ORDER BY target_price ASC",
                    (canonical_selected,)
                ).fetchall()
                stock_targets = [dict(t) for t in raw_targets]
            except Exception as exc:
                current_app.logger.warning("Error fetching stock targets: %s", exc)

        # Compute per-client target gains for the selected stock
        for ch in enriched_clients_holding:
            ch_targets = []
            for t in stock_targets:
                tp = dec(t["target_price"])
                bp = dec(ch.get("buy_price") or 0)
                q = dec(ch.get("quantity") or 0)
                gain = (tp - bp) * q
                gain_pct = ((tp - bp) / bp * 100) if bp else ZERO
                ch_targets.append({
                    "id": t.get("id"),
                    "label": t.get("label"),
                    "target_price": tp,
                    "notes": t.get("notes"),
                    "gain": gain,
                    "gain_pct": gain_pct,
                })
            ch["target_gains"] = ch_targets

        return render_template(
            "charts.html",
            stocks=stocks,
            selected_stock=canonical_selected or selected_stock,
            selected_info=selected_info,
            clients_holding=enriched_clients_holding,
            stock_trades=stock_trades,
            stock_targets=stock_targets,
            known_stocks=get_known_stocks(db),
        )

    @app.route("/charts/targets/set", methods=["POST"])
    @login_required
    def chart_set_target():
        db = get_db()
        raw_stock = request.form.get("stock", "")
        stock = resolve_canonical_stock(db, raw_stock) or raw_stock.strip().upper()
        if not stock:
            flash("Stock is required.", "error")
            return redirect(url_for("charts_view"))
        label = (request.form.get("label") or "T1").strip()[:20]
        target_price_raw = request.form.get("target_price", "").strip()
        notes_raw = request.form.get("notes", "").strip()[:200]
        try:
            target_price = number(target_price_raw, "Target price", positive=True)
        except (ValidationError, Exception) as e:
            flash(f"Invalid target price: {e}", "error")
            return redirect(url_for("charts_view", stock=stock))
        try:
            db.execute(
                "INSERT INTO stock_price_targets (stock, label, target_price, notes) VALUES (?, ?, ?, ?)",
                (stock, label, target_price, notes_raw or None)
            )
            db.commit()
            flash(f"Target {label} (₹{target_price}) added for {stock}.", "success")
        except Exception as e:
            current_app.logger.error("Error saving target: %s", e)
            flash("Could not save target.", "error")
        return redirect(url_for("charts_view", stock=stock))

    @app.route("/charts/targets/<int:tid>/delete", methods=["POST"])
    @login_required
    def chart_delete_target(tid):
        db = get_db()
        row = db.execute("SELECT stock FROM stock_price_targets WHERE id=?", (tid,)).fetchone()
        stock = row["stock"] if row else ""
        try:
            db.execute("DELETE FROM stock_price_targets WHERE id=?", (tid,))
            db.commit()
            flash("Target removed.", "success")
        except Exception as e:
            current_app.logger.error("Error deleting target: %s", e)
            flash("Could not delete target.", "error")
        return redirect(url_for("charts_view", stock=stock))

    @app.route("/charts/targets/<int:tid>/update", methods=["POST"])
    @login_required
    def chart_update_target(tid):
        db = get_db()
        row = db.execute("SELECT stock, label, target_price FROM stock_price_targets WHERE id=?", (tid,)).fetchone()
        if not row:
            flash("Target not found.", "error")
            return redirect(url_for("charts_view"))
        stock = row["stock"]
        new_price_raw = request.form.get("target_price", "").strip()
        delta_raw = request.form.get("delta", "").strip()
        try:
            if delta_raw:
                delta = float(delta_raw)
                target_price = round(float(row["target_price"]) + delta, 2)
                if target_price <= 0:
                    flash("Target price must be positive.", "error")
                    return redirect(url_for("charts_view", stock=stock))
            else:
                target_price = number(new_price_raw, "Target price", positive=True)
            db.execute("UPDATE stock_price_targets SET target_price=? WHERE id=?", (target_price, tid))
            db.commit()
            flash(f"Target {row['label']} updated to ₹{target_price:.2f}.", "success")
        except Exception as e:
            current_app.logger.error("Error updating target: %s", e)
            flash(f"Could not update target: {e}", "error")
        return redirect(url_for("charts_view", stock=stock))

    @app.route("/api/stocks/<path:stock_name>/candles")
    def api_stock_candles(stock_name):
        if not session.get("admin") and not session.get("client_id"):
            abort(401, description="Authentication required.")
        db = get_db()
        range_str = request.args.get("range", "1mo").strip()
        interval = request.args.get("interval", "1d").strip()
        candles_data = fetch_stock_candles(stock_name, range_str=range_str, interval=interval, db=db)
        return jsonify(candles_data)

    # Cache and throttling for price update endpoints
    _price_update_state = {
        "ts": 0.0,
        "data": None,
        "lock": threading.Lock(),
    }

    @app.route("/admin/update-prices", methods=["GET", "POST"])
    @app.route("/admin/update-price", methods=["GET", "POST"])
    @app.route("/update-prices", methods=["GET", "POST"])
    @app.route("/update-price", methods=["GET", "POST"])
    @app.route("/api/update-prices", methods=["GET", "POST"])
    @app.route("/api/update-price", methods=["GET", "POST"])
    @app.route("/api/cron/update-prices", methods=["GET", "POST"])
    @app.route("/api/cron/update-price", methods=["GET", "POST"])
    def admin_update_prices():
        # Authenticate if admin session, valid bearer/query token, or cron
        is_admin = bool(session.get("admin"))
        is_cron = bool(
            request.headers.get("x-vercel-cron")
            or "vercel-cron" in request.headers.get("user-agent", "").lower()
        )
        auth_hdr = request.headers.get("Authorization", "")
        token = request.args.get("token") or request.args.get("key") or (
            auth_hdr[7:] if auth_hdr.startswith("Bearer ") else ""
        )
        cron_secret = os.getenv("CRON_SECRET", "")
        app_secret = current_app.secret_key
        has_secret_token = bool(token and (
            (cron_secret and token == cron_secret) or (app_secret and token == app_secret)
        ))

        force = request.args.get("force") in ("1", "true", "yes") or request.form.get("force") in ("1", "true", "yes")
        now_ts = time.time()

        with _price_update_state["lock"]:
            # If called recently (< 15s ago) and not forced, return cached results without hammering Yahoo Finance
            if not force and (now_ts - _price_update_state["ts"] < 15.0) and _price_update_state["data"] is not None:
                res = _price_update_state["data"]
                status_note = "cached"
            else:
                try:
                    res = update_all_stock_prices(get_db())
                    _price_update_state["ts"] = now_ts
                    _price_update_state["data"] = res
                    status_note = "fresh"
                except Exception as e:
                    app.logger.exception("Error updating stock prices: %s", e)
                    res = {"total": 0, "updated": [], "failed": [str(e)]}
                    status_note = "error"

        updated = res.get("updated", [])
        failed = res.get("failed", [])
        count = len(updated)
        failed_count = len(failed)

        if count:
            msg = f"Updated market prices for {count} stock(s)."
        elif failed_count:
            msg = f"Could not fetch prices for {failed_count} stock(s). Check symbols or internet connection."
        else:
            msg = "No holdings currently recorded to update."

        now_ist = datetime.now(IST)
        market_open = is_market_hours(now_ist)
        time_ist_str = now_ist.strftime("%H:%M IST")
        date_time_ist_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")

        serialized_quotes = []
        for q in updated:
            curr = float(q["current_price"]) if q.get("current_price") is not None else 0.0
            prev = float(q["previous_close"]) if q.get("previous_close") is not None else 0.0
            chg = float(q["change"]) if q.get("change") is not None else 0.0
            chg_pct = float(q["change_pct"]) if q.get("change_pct") is not None else 0.0
            serialized_quotes.append({
                "stock": q.get("stock", ""),
                "ticker": q.get("ticker", q.get("stock", "")),
                "current_price": curr,
                "current_price_formatted": f"₹{curr:,.2f}",
                "previous_close": prev,
                "change": chg,
                "change_formatted": f"{'+' if chg >= 0 else ''}{chg:,.2f}",
                "change_pct": chg_pct,
                "change_pct_formatted": f"{'+' if chg_pct >= 0 else ''}{chg_pct:.2f}%",
                "updated_at": time_ist_str,
            })

        # JSON response detection
        accept_header = request.headers.get("Accept", "").lower()
        ua = request.headers.get("User-Agent", "").lower()
        is_json_req = (
            request.path.startswith("/api/")
            or "application/json" in accept_header
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or request.args.get("json") in ("1", "true")
            or "curl" in ua
            or "httpie" in ua
            or request.content_type == "application/json"
        )

        if is_json_req:
            return jsonify({
                "ok": True if status_note != "error" else False,
                "status": status_note,
                "total": res.get("total", count + failed_count),
                "updated_count": count,
                "updated": serialized_quotes,
                "failed": failed,
                "message": msg,
                "is_market_hours": market_open,
                "market_status": "open" if market_open else "closed",
                "updated_at_ist": time_ist_str,
                "updated_at_full": date_time_ist_str,
            }), 200

        # Also flash for admin if they navigate back
        if is_admin:
            flash(msg, "success" if count else ("warning" if failed_count else "info"))

        # HTML response with HTTP 200 OK
        quotes_summary = "".join([
            f'<div style="display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid rgba(255,255,255,0.07);">'
            f'<strong style="color:#fff;font-size:15px;">{q["stock"]}</strong>'
            f'<span style="font-family:monospace;font-weight:600;font-size:14px;color:{"#10b981" if q["change"] >= 0 else "#ef4444"};">'
            f'{q["current_price_formatted"]} ({q["change_pct_formatted"]})</span></div>'
            for q in serialized_quotes
        ]) or '<p style="color:#94a3b8;font-size:13px;margin:12px 0;">No active stock positions found.</p>'

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Market Prices Updated · Alder & Vane</title>
  <style>
    :root {{ --bg: #0b0f19; --card: #151c2e; --border: #232d48; --text: #f0f4fc; --sub: #94a3b8; --green: #10b981; --accent: #3b82f6; }}
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); padding: 40px 20px; display: flex; justify-content: center; align-items: center; min-height: 80vh; margin: 0; }}
    .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 32px; max-width: 500px; width: 100%; box-shadow: 0 20px 40px rgba(0,0,0,0.4); text-align: center; }}
    .badge-icon {{ width: 56px; height: 56px; background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin: 0 auto 18px; color: var(--green); font-size: 26px; }}
    h1 {{ font-size: 20px; font-weight: 700; margin: 0 0 8px; color: #fff; }}
    p {{ color: var(--sub); font-size: 14px; margin: 0 0 20px; line-height: 1.5; }}
    .stocks-list {{ background: rgba(0,0,0,0.25); border: 1px solid var(--border); border-radius: 10px; padding: 6px 16px; text-align: left; margin-bottom: 24px; max-height: 260px; overflow-y: auto; }}
    .actions {{ display: flex; gap: 10px; justify-content: center; }}
    .btn {{ display: inline-flex; align-items: center; justify-content: center; gap: 8px; padding: 11px 22px; background: var(--accent); color: #fff; text-decoration: none; border-radius: 8px; font-size: 14px; font-weight: 600; transition: all 0.15s ease; }}
    .btn:hover {{ background: #2563eb; transform: translateY(-1px); }}
  </style>
  <meta http-equiv="refresh" content="4; url=/holdings">
</head>
<body>
  <div class="card">
    <div class="badge-icon">✓</div>
    <h1>Market Prices Updated</h1>
    <p>{msg}</p>
    <div class="stocks-list">
      {quotes_summary}
    </div>
    <div class="actions">
      <a href="/holdings" class="btn">← Back to Holdings</a>
    </div>
  </div>
</body>
</html>"""
        return html_content, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/api/live-quotes", methods=["GET"])
    def api_live_quotes():
        """Ultra-fast (<5ms) non-blocking stock prices endpoint for live ticker. Never touches external APIs."""
        db = get_db()
        prices = get_cached_prices(db)
        now_ist = datetime.now(IST)
        market_open = is_market_hours(now_ist)
        time_ist_str = now_ist.strftime("%H:%M IST")

        serialized_quotes = []
        for sym, q in prices.items():
            curr = float(q["current_price"]) if q.get("current_price") is not None else 0.0
            prev = float(q["previous_close"]) if q.get("previous_close") is not None else 0.0
            chg = float(q["change"]) if q.get("change") is not None else 0.0
            chg_pct = float(q["change_pct"]) if q.get("change_pct") is not None else 0.0
            serialized_quotes.append({
                "stock": sym,
                "ticker": sym,
                "current_price": curr,
                "current_price_formatted": f"₹{curr:,.2f}",
                "previous_close": prev,
                "change": chg,
                "change_formatted": f"{'+' if chg >= 0 else ''}{chg:,.2f}",
                "change_pct": chg_pct,
                "change_pct_formatted": f"{'+' if chg_pct >= 0 else ''}{chg_pct:.2f}%",
                "updated_at": q.get("updated_at") or time_ist_str,
            })

        return jsonify({
            "ok": True,
            "status": "cached",
            "total": len(serialized_quotes),
            "updated_count": len(serialized_quotes),
            "updated": serialized_quotes,
            "is_market_hours": market_open,
            "market_status": "open" if market_open else "closed",
            "updated_at_ist": time_ist_str,
        }), 200

    @app.route("/admin/stocks/alias", methods=["POST"])
    @login_required
    def admin_save_stock_alias():
        canonical_raw = request.form.get("canonical_name", "").strip().upper()
        aliases_raw = request.form.get("aliases", "").strip()
        redirect_to = request.form.get("redirect_to") or url_for("holdings_list")

        if not canonical_raw:
            flash("Single standard term (Canonical name) is required.", "error")
            return redirect(redirect_to)

        db = get_db()
        ensure_stock_aliases_table(db)

        # Collect aliases from both selected client stocks and manual input (excluding canonical name itself)
        selected_aliases = [s.strip().upper() for s in request.form.getlist("selected_aliases") if s.strip()]
        manual_tokens = [t.strip().upper() for t in aliases_raw.split(",") if t.strip()]
        alias_tokens = list(set([a for a in (selected_aliases + manual_tokens) if a != canonical_raw]))

        # If canonical_raw was previously mapped as an alias of another stock, reassign it to be its own canonical
        try:
            db.execute("UPDATE stock_aliases SET canonical_name=? WHERE alias=?", (canonical_raw, canonical_raw))
        except Exception:
            pass

        # If any selected alias was previously a canonical stock, merge all its child aliases under canonical_raw
        for a in alias_tokens:
            try:
                db.execute("UPDATE stock_aliases SET canonical_name=? WHERE canonical_name=?", (canonical_raw, a))
            except Exception:
                pass

        # Clean up any aliases previously mapped under canonical_raw that were unselected by admin
        try:
            existing_rows = db.execute("SELECT alias FROM stock_aliases WHERE canonical_name=?", (canonical_raw,)).fetchall()
            for row in existing_rows:
                ea = row["alias"].strip().upper()
                if ea != canonical_raw and ea not in alias_tokens:
                    db.execute("DELETE FROM stock_aliases WHERE canonical_name=? AND alias=?", (canonical_raw, ea))
        except Exception:
            pass

        # Update all client portals: trades, client_shares, client_alerts, stock_prices to the main canonical name
        updated_trades = 0
        updated_shares = 0
        for a in alias_tokens:
            r1 = db.execute("UPDATE trades SET stock=? WHERE UPPER(TRIM(stock))=?", (canonical_raw, a))
            r2 = db.execute("UPDATE client_shares SET stock=? WHERE UPPER(TRIM(stock))=?", (canonical_raw, a))
            db.execute("UPDATE client_alerts SET stock=? WHERE UPPER(TRIM(stock))=?", (canonical_raw, a))
            try:
                db.execute(
                    "UPDATE stock_prices SET stock=? WHERE UPPER(TRIM(stock))=? AND NOT EXISTS (SELECT 1 FROM stock_prices sp2 WHERE sp2.stock=?)",
                    (canonical_raw, a, canonical_raw)
                )
                db.execute("DELETE FROM stock_prices WHERE UPPER(TRIM(stock))=?", (a,))
            except Exception:
                pass
            if hasattr(r1, "rowcount") and r1.rowcount:
                updated_trades += r1.rowcount
            if hasattr(r2, "rowcount") and r2.rowcount:
                updated_shares += r2.rowcount

        # Remove all other aliases so only the main standard name stays in stock_aliases
        try:
            db.execute("DELETE FROM stock_aliases WHERE canonical_name=? AND UPPER(TRIM(alias)) != ?", (canonical_raw, canonical_raw))
            for a in alias_tokens:
                db.execute("DELETE FROM stock_aliases WHERE UPPER(TRIM(alias))=? AND UPPER(TRIM(canonical_name)) != ?", (a, canonical_raw))
            db.execute(
                """INSERT INTO stock_aliases (canonical_name, alias) VALUES (?, ?)
                   ON CONFLICT(alias) DO UPDATE SET canonical_name=excluded.canonical_name""",
                (canonical_raw, canonical_raw)
            )
        except Exception as exc:
            current_app.logger.warning("Error saving stock alias: %s", exc)

        invalidate_stock_caches()
        flash(
            f"Configured main stock name '{canonical_raw}'. Updated {updated_trades} trade(s) and {updated_shares} position(s). All other aliases removed so only the main name stays.",
            "success"
        )
        return redirect(redirect_to)

    @app.route("/admin/stocks/alias/delete", methods=["POST"])
    @login_required
    def admin_delete_stock_alias():
        alias_to_delete = request.form.get("alias", "").strip().upper()
        canonical_to_delete = request.form.get("canonical_name", "").strip().upper()
        redirect_to = request.form.get("redirect_to") or url_for("holdings_list")

        db = get_db()
        ensure_stock_aliases_table(db)

        if alias_to_delete:
            db.execute("DELETE FROM stock_aliases WHERE UPPER(TRIM(alias))=?", (alias_to_delete,))
            invalidate_stock_caches()
            flash(f"Alias '{alias_to_delete}' removed.", "success")
        elif canonical_to_delete:
            db.execute("DELETE FROM stock_aliases WHERE UPPER(TRIM(canonical_name))=?", (canonical_to_delete,))
            invalidate_stock_caches()
            flash(f"Stock mapping for '{canonical_to_delete}' removed.", "success")

        return redirect(redirect_to)

    @app.route("/clients", methods=["GET", "POST"])
    @login_required
    def clients_list():
        if request.method == "POST":
            name = client_name(request.form.get("name"))
            values = client_values(request.form, name)
            if get_db().execute("SELECT id FROM clients WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise ValidationError("A user with that name already exists.")
            raw_username = request.form.get("username", "").strip()
            username = raw_username.lstrip("@").strip() if raw_username else None
            if username:
                if len(username) < 3 or len(username) > 60:
                    raise ValidationError("Username must be between 3 and 60 characters.")
                if get_db().execute("SELECT id FROM clients WHERE lower(username)=lower(?)", (username,)).fetchone():
                    raise ValidationError("A client with that username already exists.")
            portal_pwd = (request.form.get("portal_password") or request.form.get("client_password") or "").strip()
            if portal_pwd and len(portal_pwd) < 8:
                raise ValidationError("Password must be at least 8 characters.")
            if not portal_pwd:
                portal_pwd = "Client@1234"
            p_hash = generate_password_hash(portal_pwd)
            p_plain = portal_pwd
            get_db().execute(
                "INSERT INTO clients(name,phone,email,commission_type,commission_rate,notes,portfolio_amount,password_hash,password_plain,username,password_changed) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (*values, p_hash, p_plain, username),
            )
            flash(f"Added {name}." + (f" Username: @{username}." if username else "") + (f" Portal password set: {portal_pwd}" if portal_pwd else ""), "success")
            return redirect(url_for("clients_list"))
        rows = aggregate_clients_sql()
        paid = payments_by_client()
        for r in rows:
            rec = paid.get(r["id"], ZERO)
            r["paid"] = rec
            r["outstanding"] = r["commission"] - rec

        page = request.args.get("page", 1)
        try:
            page = max(1, int(page))
        except (ValueError, TypeError):
            page = 1
        per_page = 25
        total_clients = len(rows)
        total_pages = max(1, math.ceil(total_clients / per_page)) if total_clients else 1
        if page > total_pages:
            page = total_pages
        start_idx = (page - 1) * per_page
        clients_page = rows[start_idx : start_idx + per_page]

        kpi_totals = {
            "count": total_clients,
            "commission": sum((r["commission"] for r in rows), ZERO),
            "paid": sum((r["paid"] for r in rows), ZERO),
            "outstanding": sum((r["outstanding"] for r in rows), ZERO),
        }

        pending_recs = get_pending_holding_reconciliations(get_db())
        holding_mismatches = get_trades_holdings_mismatches(get_db(), pending_recs=pending_recs)
        client_mismatch_summary = summarize_mismatches_by_client(holding_mismatches, pending_recs)
        mismatch_by_cid = {entry["client_id"]: entry for entry in client_mismatch_summary}

        return render_template(
            "clients.html",
            clients=clients_page,
            page=page,
            total_pages=total_pages,
            total_count=total_clients,
            per_page=per_page,
            kpi_totals=kpi_totals,
            args=request.args,
            client_mismatch_summary=client_mismatch_summary,
            mismatch_by_cid=mismatch_by_cid,
        )

    @app.route("/clients/<int:cid>", methods=["GET", "POST"])
    @login_required
    def client_detail(cid):
        client = get_client(cid)
        if request.method == "POST":
            name = client_name(request.form.get("name"))
            values = client_values(request.form, name)
            if get_db().execute("SELECT id FROM clients WHERE lower(name)=lower(?) AND id<>?", (name, cid)).fetchone():
                raise ValidationError("Another user already has that name.")
            raw_username = request.form.get("username", "").strip()
            username = raw_username.lstrip("@").strip() if raw_username else None
            if username:
                if len(username) < 3 or len(username) > 60:
                    raise ValidationError("Username must be between 3 and 60 characters.")
                if get_db().execute("SELECT id FROM clients WHERE lower(username)=lower(?) AND id<>?", (username, cid)).fetchone():
                    raise ValidationError("Another client already has that username.")
            get_db().execute(
                "UPDATE clients SET name=?,phone=?,email=?,commission_type=?,commission_rate=?,notes=?,portfolio_amount=?,username=? WHERE id=?",
                (*values, username, cid),
            )
            global _active_clients_cache
            _active_clients_cache = None
            flash("User updated.", "success")
            return redirect(url_for("client_detail", cid=cid))
        trades = query_trades({"client_id": cid})
        summary = summarize(trades)
        payments = get_db().execute(
            "SELECT * FROM payments WHERE client_id=? ORDER BY paid_on DESC,id DESC", (cid,)
        ).fetchall()
        summary["paid"] = sum((dec(payment["amount"]) for payment in payments), ZERO)
        summary["outstanding"] = summary["commission"] - summary["paid"]

        try:
            funds = get_db().execute(
                "SELECT * FROM funds WHERE client_id=? ORDER BY added_on DESC,id DESC", (cid,)
            ).fetchall()
        except Exception:
            funds = []
        total_funds = sum((dec(fund["amount"]) for fund in funds), ZERO)

        raw_shares = get_client_calculated_holdings(cid, get_db(), sync_trades=True)
        prices = get_cached_prices(get_db())
        shares = []
        total_holdings_invested = ZERO
        deducted_holdings_cost = ZERO
        for s in raw_shares:
            item = dict(s)
            qty = dec(item.get("quantity"))
            buy_p = dec(item.get("buy_price"))
            cost = qty * buy_p
            total_holdings_invested += cost
            if item.get("deduct_from_funds"):
                deducted_holdings_cost += cost
            quote = prices.get((item.get("stock") or "").strip().upper())
            if quote:
                cmp = quote["current_price"]
                cur_val = qty * cmp
                unrealized = cur_val - cost
                unrealized_pct = (unrealized / cost * 100) if cost else ZERO
                item.update(
                    cmp=cmp,
                    current_value=cur_val,
                    unrealized_pnl=unrealized,
                    unrealized_pct=unrealized_pct,
                    price_change=quote.get("change", ZERO),
                    price_change_pct=quote.get("change_pct", ZERO),
                )
            else:
                item.update(
                    cmp=None,
                    current_value=cost,
                    unrealized_pnl=ZERO,
                    unrealized_pct=ZERO,
                    price_change=ZERO,
                    price_change_pct=ZERO,
                )
            tv_map = {
                "TECHNOCRAF": "BSE:544864",
                "TECHNOCRAFT": "BSE:544864",
                "TECHNOCRAFT VENTURES": "BSE:544864",
                "TIIL": "NSE:TIIL",
                "V2 RETAIL": "NSE:V2RETAIL",
                "V2": "NSE:V2RETAIL",
                "V2RETAIL": "NSE:V2RETAIL",
                "BLS": "NSE:BLS",
                "BLS POLYMERS": "NSE:BLSPOLY",
                "BLSPOLY": "NSE:BLSPOLY",
                "SBC": "NSE:SBC",
                "SBC EXPORTS": "NSE:SBC",
            }
            stk_up = (item.get("stock") or "").strip().upper()
            item["tv_symbol"] = tv_map.get(stk_up, f"NSE:{stk_up}")
            shares.append(item)

        summary["money_in_bank"] = total_funds - deducted_holdings_cost
        summary["net_funds"] = total_funds

        try:
            expire_old_alerts(get_db())
            raw_advisories = get_db().execute(
                """
                SELECT a.*, v.acknowledged, v.acknowledged_at
                FROM client_alerts a
                LEFT JOIN client_alert_views v ON v.alert_id = a.id AND v.client_id = ?
                WHERE a.client_id = ? OR a.client_id IS NULL
                ORDER BY a.id DESC
                """,
                (cid, cid),
            ).fetchall()
            alias_map = get_alias_mapping(get_db())
            held_canonical_stocks = {
                alias_map.get((sh.get("stock") or "").strip().upper(), (sh.get("stock") or "").strip().upper())
                for sh in shares
                if dec(sh.get("quantity") or 0) > ZERO
            }
            now_utc = datetime.now(timezone.utc)
            advisories = []
            for adv in raw_advisories:
                ad = dict(adv)
                created_dt = parse_utc_datetime(ad.get("created_at"))
                is_expired = False
                if created_dt and (now_utc - created_dt).total_seconds() >= 86400:
                    is_expired = True
                ad["is_expired"] = is_expired
                if is_expired:
                    ad["active"] = 0

                if ad["action"] == "SELL" and ad["client_id"] is None:
                    adv_stock = alias_map.get((ad.get("stock") or "").strip().upper(), (ad.get("stock") or "").strip().upper())
                    if adv_stock not in held_canonical_stocks and not ad.get("acknowledged"):
                        continue
                advisories.append(ad)
        except Exception:
            advisories = []

        pending_reconciliations = get_pending_holding_reconciliations(get_db(), cid)
        holding_mismatches = get_trades_holdings_mismatches(get_db(), cid, pending_recs=pending_reconciliations)

        all_targets = {}
        try:
            target_rows = get_db().execute("SELECT * FROM stock_price_targets ORDER BY target_price ASC").fetchall()
            for tr in target_rows:
                s_key = alias_map.get((tr["stock"] or "").strip().upper(), (tr["stock"] or "").strip().upper())
                all_targets.setdefault(s_key, []).append(dict(tr))
        except Exception:
            all_targets = {}

        _, eff_rate = effective_rule(client)
        show_advisory_agreement = float(eff_rate) < 35.0
        payment_path = url_for("client_pay", token=payment_signer().dumps(cid))
        public_origin = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")

        return render_template(
            "client_detail.html", client=client, trades=trades, s=summary, payments=payments,
            funds=funds, total_funds=total_funds, shares=shares, advisories=advisories,
            uploads=upload_records(cid), rule=rule_label(client["commission_type"], client["commission_rate"]),
            merge_clients=get_db().execute("SELECT id,name FROM clients WHERE id<>? ORDER BY lower(name)", (cid,)).fetchall(),
            payment_url=public_origin + payment_path,
            pending_reconciliations=pending_reconciliations,
            holding_mismatches=holding_mismatches,
            stock_targets=all_targets,
            show_advisory_agreement=show_advisory_agreement,
        )

    @app.route("/clients/<int:cid>/set-username", methods=["POST"])
    @login_required
    def client_set_username(cid):
        client = get_client(cid)
        raw_username = request.form.get("username", "").strip()
        username = raw_username.lstrip("@").strip() if raw_username else None
        back = request.form.get("back")
        if username:
            if len(username) < 3 or len(username) > 60:
                flash("Username must be between 3 and 60 characters.", "error")
                return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))
            if not re.match(r"^[a-zA-Z0-9_.]+$", username):
                flash("Username can only contain letters, numbers, underscores, and dots.", "error")
                return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))
            existing = get_db().execute("SELECT id FROM clients WHERE lower(username)=lower(?) AND id<>?", (username, cid)).fetchone()
            if existing:
                flash("A client with that username already exists.", "error")
                return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))
        get_db().execute("UPDATE clients SET username=? WHERE id=?", (username, cid))
        msg = f"Username for {client['name']} updated to: @{username}" if username else f"Username removed for {client['name']}"
        flash(msg, "success")
        return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))

    @app.route("/clients/<int:cid>/set-password", methods=["POST"])
    @login_required
    def client_set_password(cid):
        client = get_client(cid)
        pwd = request.form.get("client_password", "").strip()
        confirm = request.form.get("confirm_password", "").strip()
        back = request.form.get("back")
        if not pwd or len(pwd) < 8:
            flash("Password must be at least 8 characters.", "error")
            return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))
        if confirm and pwd != confirm:
            flash("Passwords do not match.", "error")
            return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))
        p_hash = generate_password_hash(pwd)
        get_db().execute("UPDATE clients SET password_hash=?, password_plain=? WHERE id=?", (p_hash, pwd, cid))
        flash(f"Portal password for {client['name']} updated to: {pwd}", "success")
        return redirect(safe_redirect_target(back, url_for("client_detail", cid=cid)))

    @app.route("/clients/<int:cid>/shares/new", methods=["POST"])
    @login_required
    def client_share_add(cid):
        get_client(cid)
        db = get_db()
        stock = resolve_canonical_stock(db, request.form.get("stock", ""))
        quantity = number(request.form.get("quantity"), "Quantity") if request.form.get("quantity") else None
        buy_price = number(request.form.get("buy_price"), "Buy Price") if request.form.get("buy_price") else None
        raw_deduct = request.form.get("deduct_from_funds")
        deduct_from_funds = 1 if raw_deduct in (1, "1", "true", "True", "on", True) else 0
        if not stock:
            abort(400)
        qty_int = int(round(quantity)) if quantity is not None else 1
        if qty_int <= 0:
            raise ValidationError("Quantity must be greater than zero.")
        effective_bp = buy_price if buy_price is not None else ZERO

        db.execute(
            """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source, deduct_from_funds)
               VALUES (?, ?, ?, ?, ?, 'Holding added by admin', 'manual', ?)""",
            (cid, stock.upper(), qty_int, effective_bp, date.today().isoformat(), deduct_from_funds)
        )
        sync_client_holdings_from_trades(cid, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        flash("Holding added.", "success")
        return redirect(safe_redirect_target(request.form.get("back"), url_for("client_detail", cid=cid)))

    @app.route("/clients/<int:cid>/shares/<int:sid>/delete", methods=["POST"])
    @login_required
    def client_share_delete(cid, sid):
        db = get_db()
        holding = db.execute("SELECT * FROM client_shares WHERE id=? AND client_id=?", (sid, cid)).fetchone()
        trade = None if holding else db.execute("SELECT * FROM trades WHERE id=? AND client_id=?", (sid, cid)).fetchone()

        target_stock = holding["stock"] if holding else (trade["stock"] if trade else None)
        if target_stock:
            _, variants = get_matching_stock_variants(db, target_stock, include_known_tickers=True)
            in_sql, in_params = _stock_in_clause(variants)
            db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (cid, *in_params))
            db.execute(
                f"DELETE FROM trades WHERE client_id=? AND {in_sql} AND (sell_price IS NULL OR sell_price=0)",
                (cid, *in_params),
            )
        if holding:
            db.execute("DELETE FROM client_shares WHERE id=? AND client_id=?", (sid, cid))
        if trade and (trade.get("sell_price") is None or dec(trade.get("sell_price") or 0) == ZERO):
            db.execute("DELETE FROM trades WHERE id=? AND client_id=?", (sid, cid))

        sync_client_holdings_from_trades(cid, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        flash("Holding deleted.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("client_detail", cid=cid)))

    @app.route("/clients/<int:cid>/shares/<int:sid>/edit", methods=["POST"])
    @login_required
    def client_share_edit(cid, sid):
        db = get_db()
        stock = resolve_canonical_stock(db, request.form.get("stock", ""))
        quantity = number(request.form.get("quantity"), "Quantity") if request.form.get("quantity") else None
        buy_price = number(request.form.get("buy_price"), "Buy Price") if request.form.get("buy_price") else None
        raw_deduct = request.form.get("deduct_from_funds")
        deduct_from_funds = 1 if raw_deduct in (1, "1", "true", "True", "on", True) else 0
        if not stock:
            abort(400)
        qty_int = int(round(quantity)) if quantity is not None else None

        existing_holding = db.execute("SELECT * FROM client_shares WHERE id=? AND client_id=?", (sid, cid)).fetchone()
        existing_trade = None if existing_holding else db.execute("SELECT * FROM trades WHERE id=? AND client_id=?", (sid, cid)).fetchone()
        old_stock = existing_holding["stock"] if existing_holding else (existing_trade["stock"] if existing_trade else stock)

        _, old_variants = get_matching_stock_variants(db, old_stock, include_known_tickers=False)
        _, new_variants = get_matching_stock_variants(db, stock, include_known_tickers=False)
        all_variants = old_variants | new_variants
        in_sql, in_params = _stock_in_clause(all_variants)

        if qty_int is not None and qty_int <= 0:
            db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (cid, *in_params))
            db.execute(
                f"DELETE FROM trades WHERE client_id=? AND {in_sql} AND (sell_price IS NULL OR sell_price=0)",
                (cid, *in_params),
            )
            sync_client_holdings_from_trades(cid, db)
            invalidate_stock_caches()
            invalidate_prices_cache()
            db.commit()
            flash("Holding deleted.", "success")
            return redirect(safe_redirect_target(request.referrer, url_for("client_detail", cid=cid)))

        effective_bp = buy_price if buy_price is not None else ZERO
        open_trades = db.execute(
            f"""SELECT id, quantity, buy_price FROM trades
                WHERE client_id=? AND {in_sql} AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0)
                ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
            (cid, *in_params)
        ).fetchall()

        if open_trades:
            first_id = open_trades[0]["id"]
            new_qty = qty_int if qty_int is not None else int(round(dec(open_trades[0]["quantity"] or 1)))
            new_bp = buy_price if buy_price is not None else dec(open_trades[0]["buy_price"] or 0)
            db.execute(
                "UPDATE trades SET stock=?, quantity=?, buy_price=?, deduct_from_funds=? WHERE id=? AND client_id=?",
                (stock.upper(), new_qty, new_bp, deduct_from_funds, first_id, cid)
            )
            for extra in open_trades[1:]:
                db.execute("DELETE FROM trades WHERE id=? AND client_id=?", (extra["id"], cid))
        else:
            new_qty = qty_int if qty_int is not None else 1
            db.execute(
                """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source, deduct_from_funds)
                   VALUES (?, ?, ?, ?, ?, 'Holding updated by admin', 'manual', ?)""",
                (cid, stock.upper(), new_qty, effective_bp, date.today().isoformat(), deduct_from_funds)
            )

        # Clean up stale client_shares rows under old aliases before syncing
        if old_variants - new_variants:
            old_in_sql, old_in_params = _stock_in_clause(old_variants - new_variants)
            db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {old_in_sql}", (cid, *old_in_params))

        sync_client_holdings_from_trades(cid, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        flash("Holding updated.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("client_detail", cid=cid)))

    @app.route("/clients/<int:cid>/shares/<int:sid>/sell", methods=["POST"])
    @login_required
    def client_share_sell(cid, sid):
        db = get_db()
        holding = db.execute("SELECT * FROM client_shares WHERE id=? AND client_id=?", (sid, cid)).fetchone()
        if not holding:
            trade_row = db.execute("SELECT * FROM trades WHERE id=? AND client_id=?", (sid, cid)).fetchone()
            if trade_row:
                holding = {
                    "id": sid,
                    "client_id": cid,
                    "stock": trade_row["stock"],
                    "quantity": trade_row["quantity"],
                    "buy_price": trade_row["buy_price"],
                    "added_on": trade_row["buy_date"],
                }
            else:
                abort(404)

        sell_qty = int(round(number(request.form.get("quantity"), "Quantity", required=True, positive=True)))
        sell_price = number(request.form.get("sell_price"), "Sell Price", required=True, positive=True)
        sell_date = valid_date(request.form.get("sell_date"), "Sell Date") or date.today().isoformat()

        apply_sale_fifo(
            client_id=cid,
            stock=holding["stock"],
            sell_quantity=sell_qty,
            sell_price=sell_price,
            sell_date=sell_date,
            notes="Sold from Holdings",
            source="manual",
            db=db,
        )
        db.commit()
        flash(f"Sold {sell_qty} shares of {holding['stock']}. Trades and holdings updated via FIFO.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("client_detail", cid=cid)))

    def _reconcile_closed_trade_fifo(db, cid, stock, closed_trade_id):
        """Apply a closed/sale trade against open holding lots via FIFO and return (ok, message, category)."""
        ct = db.execute("SELECT * FROM trades WHERE id=? AND client_id=?", (closed_trade_id, cid)).fetchone()
        if not ct:
            return False, "Closed trade record not found.", "error"

        canonical_stock, variants = get_matching_stock_variants(db, stock, include_known_tickers=True)
        _, ct_variants = get_matching_stock_variants(db, ct["stock"], include_known_tickers=True)
        variants = variants | ct_variants
        in_sql, in_params = _stock_in_clause(variants)

        raw_dismissed = setting("dismissed_holding_reconciliations", "[]")
        try:
            dismissed_ids = json.loads(raw_dismissed)
            if not isinstance(dismissed_ids, list):
                dismissed_ids = []
        except Exception:
            dismissed_ids = []
        if closed_trade_id not in dismissed_ids:
            dismissed_ids.append(closed_trade_id)

        def _fetch_reconcile_lots():
            return db.execute(
                f"""SELECT id, stock, quantity, buy_price, buy_date as raw_buy_date,
                           COALESCE(buy_date, created_at) as buy_date, notes, upload_id, deduct_from_funds
                    FROM trades
                    WHERE client_id = ?
                      AND {in_sql}
                      AND buy_price IS NOT NULL
                      AND (sell_price IS NULL OR sell_price = 0)
                    ORDER BY COALESCE(buy_date, created_at) ASC, id ASC""",
                (cid, *in_params)
            ).fetchall()

        open_lots = _fetch_reconcile_lots()

        sell_qty = dec(ct["quantity"] or 0)
        sell_price = dec(ct["sell_price"] or 0)
        sell_date = ct["sell_date"] or str(ct["created_at"])[:10] or date.today().isoformat()
        upload_id = ct["upload_id"]
        sale_note = "Reconciled Sale"

        cs_rows = db.execute(
            f"""SELECT id, quantity, buy_price, added_on, deduct_from_funds FROM client_shares
                WHERE client_id=? AND {in_sql} ORDER BY id ASC""",
            (cid, *in_params)
        ).fetchall()
        cs_qty = sum((dec(r["quantity"] or 0) for r in cs_rows if dec(r["quantity"] or 0) > ZERO), ZERO)
        total_open_qty = sum((dec(lot["quantity"] or 0) for lot in open_lots), ZERO)

        if not open_lots and cs_qty <= ZERO:
            set_setting("dismissed_holding_reconciliations", json.dumps(dismissed_ids))
            db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (cid, *in_params))
            sync_client_holdings_from_trades(cid, db)
            db.commit()
            return True, f"Holdings for {stock} are already settled.", "info"

        if cs_qty > total_open_qty:
            diff_qty = int(round(cs_qty - total_open_qty))
            cs_cost = ZERO
            cs_qty_for_avg = ZERO
            earliest_added = None
            cs_deduct = 0
            for r in cs_rows:
                rq = dec(r["quantity"] or 0)
                if rq <= ZERO:
                    continue
                rbp = dec(r["buy_price"] or 0)
                cs_cost += rq * rbp
                cs_qty_for_avg += rq
                if r.get("deduct_from_funds"):
                    cs_deduct = 1
                ad = str(r["added_on"])[:10] if r.get("added_on") else ""
                if ad and (not earliest_added or ad < earliest_added):
                    earliest_added = ad
            bp = (cs_cost / cs_qty_for_avg).quantize(CENT, rounding=ROUND_HALF_UP) if cs_qty_for_avg > ZERO else ZERO
            b_date = earliest_added or sell_date
            if b_date > str(sell_date)[:10]:
                b_date = str(sell_date)[:10]
            db.execute(
                """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source, deduct_from_funds)
                   VALUES (?, ?, ?, ?, ?, 'Holding position', 'manual', ?)""",
                (cid, canonical_stock, diff_qty, bp, b_date, cs_deduct)
            )
            open_lots = _fetch_reconcile_lots()
            total_open_qty = sum((dec(lot["quantity"] or 0) for lot in open_lots), ZERO)

        if sell_qty >= total_open_qty:
            for lot in open_lots:
                existing_notes = (lot["notes"] or "").strip()
                merged_notes = f"{existing_notes} [{sale_note}]".strip() if (sale_note and sale_note not in existing_notes) else (existing_notes or sale_note)
                lot_sell_date = _safe_sell_date(sell_date, lot.get("raw_buy_date"))
                db.execute(
                    """UPDATE trades
                       SET sell_price=?, sell_date=?, notes=?, upload_id=COALESCE(upload_id, ?)
                       WHERE id=?""",
                    (sell_price, lot_sell_date, merged_notes, upload_id, lot["id"])
                )

            if sell_qty == total_open_qty:
                db.execute("DELETE FROM funds WHERE client_id=? AND notes LIKE ?", (cid, f"%[Trade #{ct['id']}]%"))
                db.execute("DELETE FROM trades WHERE id=?", (ct["id"],))
            else:
                new_ct_qty = int(round(sell_qty - total_open_qty))
                existing_ct_notes = (ct["notes"] or "").strip()
                ct_notes = f"{existing_ct_notes} [{sale_note}]".strip() if (sale_note and sale_note not in existing_ct_notes) else (existing_ct_notes or sale_note)
                db.execute("UPDATE trades SET quantity=?, notes=? WHERE id=?", (new_ct_qty, ct_notes, ct["id"]))
        else:
            rem_sell = sell_qty
            for lot in open_lots:
                if rem_sell <= ZERO:
                    break
                lot_qty = dec(lot["quantity"] or 0)
                existing_notes = (lot["notes"] or "").strip()
                lot_buy_date = str(lot["buy_date"])[:10] if lot["buy_date"] else str(sell_date)[:10]
                lot_sell_date = _safe_sell_date(sell_date, lot.get("raw_buy_date") or lot_buy_date)

                if lot_qty <= rem_sell:
                    merged_notes = f"{existing_notes} [{sale_note}]".strip() if (sale_note and sale_note not in existing_notes) else (existing_notes or sale_note)
                    db.execute(
                        """UPDATE trades
                           SET sell_price=?, sell_date=?, notes=?, upload_id=COALESCE(upload_id, ?)
                           WHERE id=?""",
                        (sell_price, lot_sell_date, merged_notes, upload_id, lot["id"])
                    )
                    rem_sell -= lot_qty
                else:
                    rem_open = int(round(lot_qty - rem_sell))
                    sold_portion = int(round(rem_sell))

                    db.execute("UPDATE trades SET quantity=? WHERE id=?", (rem_open, lot["id"]))

                    sold_notes = f"{existing_notes} [{sale_note}]".strip() if (sale_note and sale_note not in existing_notes) else (existing_notes or sale_note)
                    lot_stock = (lot.get("stock") or canonical_stock).strip().upper()
                    new_trade_ids = insert_trades(cid, [{
                        "stock": lot_stock,
                        "quantity": sold_portion,
                        "buy_price": dec(lot["buy_price"]),
                        "sell_price": sell_price,
                        "buy_date": lot_buy_date,
                        "sell_date": lot_sell_date,
                        "notes": sold_notes,
                        "deduct_from_funds": lot.get("deduct_from_funds", 0),
                    }], source=ct.get("source") or "portal", upload_id=upload_id or lot.get("upload_id"))
                    for nid in (new_trade_ids or []):
                        if nid not in dismissed_ids:
                            dismissed_ids.append(nid)
                    rem_sell = ZERO
                    break

            db.execute("DELETE FROM funds WHERE client_id=? AND notes LIKE ?", (cid, f"%[Trade #{ct['id']}]%"))
            db.execute("DELETE FROM trades WHERE id=?", (ct["id"],))

        set_setting("dismissed_holding_reconciliations", json.dumps(dismissed_ids))

        remaining_open = db.execute(
            f"""SELECT COALESCE(SUM(quantity), 0) as total
                FROM trades
                WHERE client_id = ?
                  AND {in_sql}
                  AND buy_price IS NOT NULL
                  AND (sell_price IS NULL OR sell_price = 0)""",
            (cid, *in_params)
        ).fetchone()
        remaining_qty = dec(remaining_open["total"] or 0) if remaining_open else ZERO

        if remaining_qty <= ZERO:
            db.execute(
                f"DELETE FROM client_shares WHERE client_id = ? AND {in_sql}",
                (cid, *in_params)
            )

        sync_trade_profits_to_funds(db, cid)
        sync_client_holdings_from_trades(cid, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        db.commit()
        return True, f"Reconciled {canonical_stock}. Open lots closed via FIFO and holdings updated.", "success"

    @app.route("/clients/<int:cid>/reconcile-holding/<stock>/accept", methods=["POST"])
    @login_required
    def client_holding_reconcile_accept(cid, stock):
        client = get_client(cid)
        db = get_db()

        try:
            closed_trade_id = int(request.form.get("closed_trade_id"))
        except (ValueError, TypeError):
            flash("Invalid trade ID for reconciliation.", "error")
            return redirect(url_for("client_detail", cid=cid))

        ok, msg, cat = _reconcile_closed_trade_fifo(db, cid, stock, closed_trade_id)
        if ok and cat == "success":
            flash(f"Reconciled {stock} for {client['name']}. Open lots closed via FIFO and holdings updated.", "success")
        else:
            flash(msg, cat)
        return redirect(url_for("client_detail", cid=cid))

    @app.route("/clients/<int:cid>/reconcile-holding/<stock>/dismiss", methods=["POST"])
    @login_required
    def client_holding_reconcile_dismiss(cid, stock):
        try:
            closed_trade_id = int(request.form.get("closed_trade_id"))
        except (ValueError, TypeError):
            flash("Invalid trade ID for dismissal.", "error")
            return redirect(url_for("client_detail", cid=cid))

        raw_dismissed = setting("dismissed_holding_reconciliations", "[]")
        try:
            dismissed_ids = json.loads(raw_dismissed)
            if not isinstance(dismissed_ids, list):
                dismissed_ids = []
        except Exception:
            dismissed_ids = []

        if closed_trade_id not in dismissed_ids:
            dismissed_ids.append(closed_trade_id)
            set_setting("dismissed_holding_reconciliations", json.dumps(dismissed_ids))
            get_db().commit()

        flash(f"Left {stock} trades unchanged as requested.", "info")
        return redirect(url_for("client_detail", cid=cid))

    @app.route("/clients/<int:cid>/mismatch/fix", methods=["POST"])
    @login_required
    def client_mismatch_fix(cid):
        client = get_client(cid)
        db = get_db()
        action = (request.form.get("action") or "").strip()
        stock = (request.form.get("stock") or "").strip().upper()
        mismatch_key = (request.form.get("mismatch_key") or "").strip()

        if action == "dismiss":
            if mismatch_key:
                raw_dismissed = setting("dismissed_holding_mismatches", "[]")
                try:
                    dismissed_keys = json.loads(raw_dismissed)
                    if not isinstance(dismissed_keys, list):
                        dismissed_keys = []
                except Exception:
                    dismissed_keys = []
                if mismatch_key not in dismissed_keys:
                    dismissed_keys.append(mismatch_key)
                    set_setting("dismissed_holding_mismatches", json.dumps(dismissed_keys))
                    db.commit()
            flash("Mismatch alert dismissed.", "info")
            return redirect(url_for("client_detail", cid=cid))

        if action == "apply_sale_fifo":
            try:
                trade_id = int(request.form.get("trade_id"))
            except (ValueError, TypeError):
                flash("Invalid trade ID.", "error")
                return redirect(url_for("client_detail", cid=cid))
            ok, msg, cat = _reconcile_closed_trade_fifo(db, cid, stock, trade_id)
            flash(msg, cat)
            return redirect(url_for("client_detail", cid=cid))

        if action == "delete_stale_holding":
            _, variants = get_matching_stock_variants(db, stock, include_known_tickers=True)
            if variants:
                in_sql, in_params = _stock_in_clause(variants)
                db.execute(f"DELETE FROM client_shares WHERE client_id=? AND {in_sql}", (cid, *in_params))
                db.execute(
                    f"DELETE FROM trades WHERE client_id=? AND {in_sql} AND (sell_price IS NULL OR sell_price=0)",
                    (cid, *in_params),
                )
            sync_client_holdings_from_trades(cid, db)
            invalidate_stock_caches()
            invalidate_prices_cache()
            db.commit()
            flash(f"Removed stale holding for {stock} from {client['name']}.", "success")
            return redirect(url_for("client_detail", cid=cid))

        if action == "create_open_trade":
            _, variants = get_matching_stock_variants(db, stock, include_known_tickers=False)
            in_sql, in_params = _stock_in_clause(variants)
            cs_rows = db.execute(
                f"SELECT id, quantity, buy_price, added_on, COALESCE(deduct_from_funds, 0) as deduct_from_funds FROM client_shares WHERE client_id=? AND {in_sql}",
                (cid, *in_params),
            ).fetchall()
            tot_qty = sum((dec(r["quantity"] or 0) for r in cs_rows if dec(r["quantity"] or 0) > ZERO), ZERO)
            if tot_qty > ZERO:
                tot_cost = sum((dec(r["quantity"] or 0) * dec(r.get("buy_price") or 0) for r in cs_rows if dec(r["quantity"] or 0) > ZERO), ZERO)
                avg_bp = (tot_cost / tot_qty).quantize(CENT, rounding=ROUND_HALF_UP)
                earliest = min((str(r["added_on"])[:10] for r in cs_rows if r.get("added_on")), default=date.today().isoformat())
                deduct_val = 1 if any(r.get("deduct_from_funds") for r in cs_rows) else 0
                db.execute(
                    """INSERT INTO trades (client_id, stock, quantity, buy_price, buy_date, notes, source, deduct_from_funds)
                       VALUES (?, ?, ?, ?, ?, 'Holding synced from portfolio', 'manual', ?)""",
                    (cid, stock, int(round(tot_qty)), avg_bp, earliest, deduct_val),
                )
                sync_client_holdings_from_trades(cid, db)
                invalidate_stock_caches()
                db.commit()
                flash(f"Recorded opening buy trade for {int(round(tot_qty))} shares of {stock}.", "success")
            else:
                flash(f"No holding quantity found for {stock}.", "info")
            return redirect(url_for("client_detail", cid=cid))

        if action == "sync_holding":
            sync_client_holdings_from_trades(cid, db)
            invalidate_stock_caches()
            invalidate_prices_cache()
            db.commit()
            flash(f"Synced holdings from open trades for {client['name']}.", "success")
            return redirect(url_for("client_detail", cid=cid))

        if action == "unify_stock_name":
            raw_variants = [v.strip().upper() for v in request.form.getlist("variants") if v and v.strip()]
            target_stock = resolve_canonical_stock(db, stock or (raw_variants[0] if raw_variants else ""))
            if target_stock and raw_variants:
                in_sql, in_params = _stock_in_clause(set(raw_variants) | {target_stock})
                db.execute(f"UPDATE trades SET stock=? WHERE client_id=? AND {in_sql}", (target_stock, cid, *in_params))
                db.execute(f"UPDATE client_shares SET stock=? WHERE client_id=? AND {in_sql}", (target_stock, cid, *in_params))
                sync_client_holdings_from_trades(cid, db)
                invalidate_stock_caches()
                invalidate_prices_cache()
                db.commit()
                flash(f"Unified stock names ({', '.join(raw_variants)}) to {target_stock} for {client['name']}.", "success")
            else:
                flash("Could not determine stock names to unify.", "error")
            return redirect(url_for("client_detail", cid=cid))

        flash("Unknown mismatch action.", "error")
        return redirect(url_for("client_detail", cid=cid))

    @app.route("/clients/<int:cid>/repair-sbc", methods=["GET", "POST"])
    @login_required
    def client_repair_sbc(cid):
        repair_client_holdings_data(get_db())
        sync_client_holdings_from_trades(cid, get_db())
        invalidate_stock_caches()
        invalidate_prices_cache()
        flash("Holdings and ledger positions refreshed.", "success")
        return redirect(url_for("client_detail", cid=cid))

    @app.route("/clients/<int:cid>/delete", methods=["POST"])
    @login_required
    def client_delete(cid):
        get_client(cid)
        get_db().execute("DELETE FROM clients WHERE id=?", (cid,))
        flash("User and their trades/payments deleted. Submission evidence is retained.", "success")
        return redirect(url_for("clients_list"))

    @app.route("/clients/<int:cid>/merge", methods=["POST"])
    @login_required
    def client_merge(cid):
        get_client(cid)
        target = identifier(request.form.get("target_id"))
        get_client(target)
        if target == cid:
            raise ValidationError("Choose a different user to merge into.")
        for table in ("trades", "payments", "uploads"):
            get_db().execute(f"UPDATE {table} SET client_id=? WHERE client_id=?", (target, cid))
        get_db().execute("DELETE FROM clients WHERE id=?", (cid,))
        flash("Users merged, including their submission history.", "success")
        return redirect(url_for("client_detail", cid=target))

    @app.route("/payments", methods=["GET", "POST"])
    @login_required
    def payments_list():
        if request.method == "POST":
            cid = identifier(request.form.get("client_id"))
            amount = number(request.form.get("amount"), "Payment amount", required=True, positive=True, places=2)
            paid_on = valid_date(request.form.get("paid_on"), "Payment date", required=True)
            mode = text(request.form.get("mode"), "Payment mode", limit=80) or None
            notes = text(request.form.get("notes"), "Notes", multiline=True) or None
            get_client(cid)
            get_db().execute(
                "INSERT INTO payments(client_id,amount,paid_on,mode,notes) VALUES (?,?,?,?,?)",
                (cid, amount, paid_on, mode, notes),
            )
            flash("Payment recorded.", "success")
            return redirect(safe_redirect_target(request.form.get("back"), url_for("payments_list")))
        payments = get_db().execute(
            "SELECT p.*,c.name AS client_name FROM payments p JOIN clients c ON c.id=p.client_id "
            "ORDER BY p.paid_on DESC,p.id DESC"
        ).fetchall()
        clients = get_db().execute("SELECT id,name FROM clients ORDER BY lower(name)").fetchall()
        preset = identifier(request.args["client_id"]) if request.args.get("client_id") else None
        return render_template(
            "payments.html", payments=payments, clients=clients,
            total=sum((dec(payment["amount"]) for payment in payments), ZERO), preset=preset,
        )

    @app.route("/payments/<int:pid>/delete", methods=["POST"])
    @login_required
    def payment_delete(pid):
        if get_db().execute("DELETE FROM payments WHERE id=?", (pid,)).rowcount != 1:
            abort(404)
        flash("Payment deleted.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("payments_list")))

    @app.route("/funds", methods=["POST"])
    @login_required
    def add_fund():
        cid = identifier(request.form.get("client_id"))
        amount = number(request.form.get("amount"), "Fund amount", required=True, positive=True, places=2)
        added_on = valid_date(request.form.get("added_on"), "Date", required=True)
        notes = text(request.form.get("notes"), "Notes", multiline=True) or None
        get_client(cid)
        get_db().execute(
            "INSERT INTO funds(client_id,amount,added_on,notes) VALUES (?,?,?,?)",
            (cid, amount, added_on, notes),
        )
        flash("Funds added.", "success")
        return redirect(safe_redirect_target(request.form.get("back"), url_for("client_detail", cid=cid)))

    @app.route("/funds/<int:fid>/delete", methods=["POST"])
    @login_required
    def fund_delete(fid):
        if get_db().execute("DELETE FROM funds WHERE id=?", (fid,)).rowcount != 1:
            abort(404)
        flash("Funds deleted.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("dashboard")))

    @app.route("/uploads")
    @login_required
    def uploads_list():
        return render_template("uploads.html", uploads=upload_records())

    @app.route("/uploads/file/<path:name>")
    def upload_file(name):
        if not session.get("admin") and not session.get("client_id"):
            abort(401)
        safe_name = safe_path(name)
        if session.get("client_id") and not session.get("admin"):
            allowed = get_db().execute(
                "SELECT 1 FROM uploads WHERE filename LIKE ? AND client_id=?",
                (f"%{safe_name}%", session["client_id"]),
            ).fetchone()
            if not allowed:
                abort(403)
        if app.config["BACKEND_MODE"] == "supabase":
            asset = get_db().execute(
                "SELECT path FROM upload_assets WHERE path=? AND upload_id IS NOT NULL", (safe_name,)
            ).fetchone()
            if not asset:
                abort(404)
            return redirect(get_storage().sign_download(safe_name), code=302)
        return send_from_directory(app.config["UPLOAD_DIR"], safe_name, as_attachment=False)

    @app.route("/admin/scan")
    @login_required
    def admin_scan():
        owner_hash()
        return render_template("upload.html", admin=True, need_code=False, portal_token="")

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        if request.method == "POST":
            ctype, rate = rule_values(
                request.form.get("default_commission_type"), request.form.get("default_commission_rate")
            )
            values = {
                "default_commission_type": ctype, "default_commission_rate": str(rate),
                "upload_code": text(request.form.get("upload_code"), "Upload code", limit=128),
                "currency": text(request.form.get("currency"), "Currency", limit=12, required=True),
                "business_name": text(request.form.get("business_name"), "Business name", limit=120),
            }
            new_password = request.form.get("new_password", "")
            if new_password:
                if app.config["ADMIN_PASSWORD_HASH"]:
                    raise ValidationError("This password is managed on the server. Update ADMIN_PASSWORD or ADMIN_PASSWORD_HASH.")
                if not 10 <= len(new_password) <= 1024:
                    raise ValidationError("Password must contain between 10 and 1024 characters.")
                values["admin_password"] = generate_password_hash(new_password)
            for key, value in values.items():
                set_setting(key, value)
            if new_password:
                session["auth_version"] = admin_credentials()[1]
                session["_csrf"] = secrets.token_urlsafe(32)
            flash("Settings saved.", "success")
            return redirect(url_for("settings_page"))
        keys = ("default_commission_type", "default_commission_rate", "upload_code", "currency", "business_name")
        legacy_count = get_db().execute(
            "SELECT COUNT(*) AS total FROM trades WHERE commission_type_snapshot IS NULL"
        ).fetchone()["total"]
        base = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")
        go_url = base + url_for("short_upload_link")
        short_url = go_url if app.config["PUBLIC_BASE_URL"] else setting("upload_short_url") or go_url
        return render_template(
            "settings.html", s={key: setting(key, "") for key in keys}, upload_url=portal_url(),
            short_url=short_url, legacy_commission_count=legacy_count,
            known_stocks=get_known_stocks(get_db()),
        )

    @app.route("/settings/rotate-link", methods=["POST"])
    @login_required
    def rotate_upload_link():
        new_token = secrets.token_urlsafe(32)
        set_setting("upload_token", new_token)
        g.pop("settings_values", None)
        # Regenerate the short URL for the new token
        try:
            short = shorten_url(portal_url())
            if short:
                set_setting("upload_short_url", short)
            else:
                set_setting("upload_short_url", "")
        except Exception:
            set_setting("upload_short_url", "")
        flash("A new submission link is ready. The old link and its pending image authorizations no longer work.", "success")
        return redirect(url_for("settings_page"))

    @app.route("/go")
    def short_upload_link():
        token = setting("upload_token", "")
        if not token:
            abort(404, description="No submission link has been set up yet.")
        return redirect(url_for("public_upload", token=token), 302)

    @app.route("/upload", defaults={"token": None})
    @app.route("/submit/<token>")
    def public_upload(token=None):
        if token is None:
            if session.get("admin"):
                return redirect(url_for("public_upload", token=portal_token()))
            return redirect(url_for("login"))
        expected = setting("upload_token", "")
        if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
            abort(404, description="This submission link is inactive. Ask the administrator for the current link.")
        owner_hash()
        return render_template(
            "upload.html", admin=False, need_code=bool(setting("upload_code")), portal_token=token,
            og_title="Alder & Vane – Send Your Trades",
            og_description="Submit your trades in seconds. Just fill in the share name and amount — no account needed.",
        )

    @app.route("/pay/<token>", methods=["GET", "POST"])
    def client_pay(token):
        try:
            cid = payment_signer().loads(token, max_age=30*24*3600)
        except Exception:
            abort(404, description="Invalid or expired payment link.")
            
        client = get_client(cid)
        trades = query_trades({"client_id": cid})
        summary = summarize(trades)
        paid = sum((dec(payment["amount"]) for payment in get_db().execute("SELECT amount FROM payments WHERE client_id=?", (cid,)).fetchall()), ZERO)
        outstanding = summary["commission"] - paid
        summary["paid"] = paid
        
        if request.method == "POST":
            if outstanding > 0:
                get_db().execute(
                    "INSERT INTO payments(client_id,amount,paid_on,mode,notes) VALUES (?,?,?,?,?)",
                    (cid, outstanding, date.today().isoformat(), "Client Link", "Marked as paid via client link"),
                )
                
            import smtplib
            from email.message import EmailMessage
            admin_email = os.environ.get("ADMIN_EMAIL")
            if admin_email:
                try:
                    msg = EmailMessage()
                    msg.set_content(f"Client {client['name']} marked their outstanding balance of {outstanding} as paid.\nCheck progress at: {url_for('client_detail', cid=client['id'], _external=True)}")
                    msg['Subject'] = f"Payment marked as done by {client['name']}"
                    msg['From'] = admin_email
                    msg['To'] = admin_email
                    
                    s = smtplib.SMTP(os.environ.get("SMTP_SERVER", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", 587)))
                    s.starttls()
                    s.login(os.environ.get("SMTP_USER", admin_email), os.environ.get("SMTP_PASSWORD", ""))
                    s.send_message(msg)
                    s.quit()
                except Exception as e:
                    app.logger.error(f"Failed to send email: {e}")
            
            flash("Thank you, payment marked as done.", "success")
            return redirect(url_for("client_pay", token=token))
            
        return render_template("pay.html", client=client, outstanding=outstanding, s=summary, trades=trades)

    @app.route("/api/parse", methods=["POST"])
    def api_parse():
        data = json_object()
        authorize_upload(data)
        if not isinstance(data.get("text"), str):
            raise ValidationError("OCR text must be a string.")
        content = text(data["text"], "OCR text", limit=MAX_TEXT, multiline=True)
        return jsonify(parse_text(content))

    @app.route("/api/uploads/sign", methods=["POST"])
    def sign_upload():
        data = json_object()
        authorization = authorize_upload(data)
        text(data.get("filename"), "Filename", limit=255, required=True)
        content_type = data.get("content_type")
        if not isinstance(content_type, str) or content_type not in IMAGE_TYPES:
            raise ValidationError("Only PNG, JPEG, and WebP screenshots are supported.")
        maximum = MAX_CLOUD_IMAGE if app.config["BACKEND_MODE"] == "supabase" else MAX_LOCAL_IMAGE
        size = data.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= maximum:
            raise ValidationError(f"Each image must contain between 1 and {maximum:,} bytes.")
        path = "evidence/" + uuid.uuid4().hex + IMAGE_TYPES[content_type][1]
        receipt = receipt_signer().dumps({**authorization, "path": path})
        if app.config["BACKEND_MODE"] == "supabase":
            storage = get_storage()
            bucket = storage.check_bucket()
            maximum = min(maximum, bucket["file_size_limit"])
            if size > maximum:
                raise ValidationError(f"This private bucket accepts images up to {maximum:,} bytes.")
            result = storage.sign_upload(path, content_type)
        else:
            result = {
                "upload_url": url_for("local_upload", receipt=receipt, _external=True), "method": "PUT",
                "headers": {"Content-Type": content_type, "X-CSRF-Token": csrf_token()},
            }
        get_db().execute(
            """INSERT INTO upload_assets(path,owner_hash,scope_hash,content_type,byte_size,expires_at)
            VALUES (?,?,?,?,?,?)""",
            (path, authorization["owner"], authorization["scope"], content_type, size, int(time.time()) + UPLOAD_TTL),
        )
        return jsonify(
            **result, path=path, receipt=receipt, expires_in=UPLOAD_TTL, max_size=maximum,
        )

    @app.route("/api/uploads/local/<receipt>", methods=["PUT"])
    def local_upload(receipt):
        if app.config["BACKEND_MODE"] != "local":
            abort(404)
        decoded = decode_receipt(receipt)
        asset = receipt_asset(decoded.get("path"), receipt)
        if request.mimetype != asset["content_type"]:
            raise ValidationError("The image Content-Type does not match its authorization.")
        contents = request.get_data()
        if len(contents) != asset["byte_size"]:
            raise ValidationError("The image size does not match its authorization.")
        get_storage().store(asset["path"], contents, asset["content_type"])
        return "", 204

    @app.route("/api/submit", methods=["POST"])
    def api_submit():
        data = json_object()
        authorization = authorize_upload(data)
        name = client_name(data.get("name"))
        raw_phone = data.get("phone")
        phone = clean_phone_number(raw_phone)
        if not phone or len(phone) != 10:
            raise ValidationError("Please provide a valid 10-digit mobile number.")
        submission_id = submission_uuid(data.get("submission_id"))
        rows = data.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ValidationError(f"Submit between 1 and {MAX_ROWS} trade rows.")
        validated = []
        for index, row in enumerate(rows, 1):
            try:
                validated.append(trade_values(row, public=True))
            except ValidationError as exc:
                raise ValidationError(f"Row {index}: {exc} No trades were saved.") from exc
        images = data.get("images", [])
        if not isinstance(images, list) or len(images) > 10:
            raise ValidationError("Attach at most 10 screenshots.")
        paths = []
        for image in images:
            if not isinstance(image, dict) or set(image) != {"path", "receipt"}:
                raise ValidationError("Every image must contain its path and signed receipt.")
            paths.append(safe_path(image["path"]))
            if not isinstance(image["receipt"], str) or len(image["receipt"]) > 2048:
                raise ValidationError("Invalid image receipt.")
        if len(set(paths)) != len(paths):
            raise ValidationError("An image cannot be attached more than once.")
        ocr_text = text(data.get("ocr_text"), "OCR text", limit=MAX_TEXT, multiline=True)
        platform = text(data.get("platform"), "Platform", limit=80) or None
        normalized = {
            "name": name, "phone": phone, "rows": validated, "images": images, "ocr_text": ocr_text,
            "platform": platform, "scope": authorization["scope"],
        }
        digest = hashlib.sha256(json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), default=str,
        ).encode()).hexdigest()
        previous = prior_submission(submission_id, digest, authorization["owner"])
        if previous == "CONFLICT":
            submission_id = str(uuid.uuid4())
            previous = None
        if previous is not None:
            return previous
        assets = [receipt_asset(image["path"], image["receipt"], authorization) for image in images]
        storage = get_storage()
        for asset in assets:
            storage.verify(asset)
        cid = get_or_create_client(name, phone)
        row = get_db().execute(
            """INSERT INTO uploads(
                client_name,client_id,filename,platform,ocr_text,trade_count,
                submission_id,submission_hash,submission_owner
            ) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(submission_id) DO NOTHING RETURNING id""",
            (name, cid, ",".join(paths), platform, ocr_text, len(validated),
             submission_id, digest, authorization["owner"]),
        ).fetchone()
        if row is None:
            previous = prior_submission(submission_id, digest, authorization["owner"])
            if previous == "CONFLICT":
                submission_id = str(uuid.uuid4())
                previous = None
            if previous is not None:
                return previous
            # If we still hit a conflict (unlikely), generate a new ID and retry the insert
            submission_id = str(uuid.uuid4())
            row = get_db().execute(
                """INSERT INTO uploads(
                    client_name,client_id,filename,platform,ocr_text,trade_count,
                    submission_id,submission_hash,submission_owner
                ) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(submission_id) DO NOTHING RETURNING id""",
                (name, cid, ",".join(paths), platform, ocr_text, len(validated),
                 submission_id, digest, authorization["owner"]),
            ).fetchone()
        upload_id = row["id"]
        for asset in assets:
            get_db().execute(
                "UPDATE upload_assets SET upload_id=? WHERE path=? AND upload_id IS NULL",
                (upload_id, asset["path"]),
            )
        db = get_db()
        regular_rows = []
        for row in validated:
            if row.get("sell_price") is None:
                regular_rows.append(row)
                continue
            if row.get("buy_price") is None:
                apply_sale_fifo(
                    client_id=cid,
                    stock=row["stock"],
                    sell_quantity=row["quantity"],
                    sell_price=row["sell_price"],
                    sell_date=row.get("sell_date"),
                    buy_price_hint=None,
                    notes=row.get("notes") or "Sold via upload",
                    upload_id=upload_id,
                    source="upload",
                    db=db,
                )
                continue

            # Closed row (both buy_price and sell_price present):
            # Auto-apply FIFO if it matches existing open holdings by full quantity or average buy price.
            _, variants = get_matching_stock_variants(db, row["stock"], include_known_tickers=True)
            in_sql, in_params = _stock_in_clause(variants)
            open_lots = db.execute(
                f"""SELECT quantity, buy_price FROM trades
                    WHERE client_id=? AND {in_sql} AND buy_price IS NOT NULL AND (sell_price IS NULL OR sell_price=0)""",
                (cid, *in_params)
            ).fetchall()
            cs_rows = db.execute(
                f"SELECT quantity, buy_price FROM client_shares WHERE client_id=? AND {in_sql} AND quantity > 0",
                (cid, *in_params)
            ).fetchall()

            open_qty = sum((dec(r["quantity"] or 0) for r in open_lots), ZERO)
            cs_qty = sum((dec(r["quantity"] or 0) for r in cs_rows), ZERO)
            eff_open_qty = max(open_qty, cs_qty)

            if eff_open_qty > ZERO:
                if open_lots and open_qty >= cs_qty:
                    tot_cost = sum((dec(r["quantity"] or 0) * dec(r["buy_price"] or 0) for r in open_lots), ZERO)
                    avg_bp = (tot_cost / open_qty).quantize(CENT, rounding=ROUND_HALF_UP) if open_qty > ZERO else ZERO
                else:
                    tot_cost = sum((dec(r["quantity"] or 0) * dec(r["buy_price"] or 0) for r in cs_rows), ZERO)
                    avg_bp = (tot_cost / cs_qty).quantize(CENT, rounding=ROUND_HALF_UP) if cs_qty > ZERO else ZERO
                row_qty = dec(row["quantity"])
                is_exact_qty = int(round(row_qty)) == int(round(eff_open_qty))
                is_bp_match = abs(dec(row["buy_price"]) - avg_bp) <= dec("0.10")
                if is_exact_qty or (row_qty < eff_open_qty and is_bp_match):
                    apply_sale_fifo(
                        client_id=cid,
                        stock=row["stock"],
                        sell_quantity=row["quantity"],
                        sell_price=row["sell_price"],
                        sell_date=row.get("sell_date"),
                        buy_price_hint=row.get("buy_price"),
                        notes=row.get("notes") or "Sold via upload",
                        upload_id=upload_id,
                        source="upload",
                        db=db,
                    )
                    continue
            regular_rows.append(row)

        if regular_rows:
            insert_trades(cid, regular_rows, source="upload", upload_id=upload_id)
        sync_trade_profits_to_funds(db, cid)
        sync_client_holdings_from_trades(cid, db)
        invalidate_stock_caches()
        invalidate_prices_cache()
        return jsonify(ok=True, saved=len(validated), submission_id=submission_id)

    @app.cli.command("init-db")
    def init_database():
        if app.config["BACKEND_MODE"] != "local":
            raise click.ClickException("Run supabase/migrations/001_commission_book.sql in Supabase instead.")
        get_db()
        click.echo("Local schema is ready. Existing ledger entries have been preserved.")

    @app.cli.command("check-cloud")
    def check_cloud():
        if app.config["BACKEND_MODE"] != "supabase":
            raise click.ClickException("DATABASE_URL is not configured; this application is using local SQLite.")
        try:
            database = get_db()
            database.execute("SELECT submission_id,client_id FROM uploads LIMIT 0")
            database.execute("SELECT commission_type_snapshot,commission_rate_snapshot FROM trades LIMIT 0")
            tables = database.execute(
                """SELECT c.relname,c.relrowsecurity,
                    (has_table_privilege('anon',c.oid,'SELECT,INSERT,UPDATE,DELETE')
                     OR has_table_privilege('authenticated',c.oid,'SELECT,INSERT,UPDATE,DELETE')) AS browser_access
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname='public' AND c.relkind='r'
                  AND c.relname IN ('settings','clients','trades','payments','uploads','upload_assets')"""
            ).fetchall()
            if len(tables) != 6 or any(not table["relrowsecurity"] or table["browser_access"] for table in tables):
                raise click.ClickException("The database schema or browser-access restrictions are incomplete. Run the SQL migration.")
            get_storage().check_bucket()
        except PsycopgError as exc:
            raise click.ClickException("PostgreSQL could not be verified. Check DATABASE_URL and apply the SQL migration.") from exc
        except (ValidationError, StorageUnavailable) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("PostgreSQL is reachable, all six ledger tables have RLS and deny browser access, and Storage is private.")

    @app.cli.command("migrate-local")
    @click.option("--database", "database_path", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.option("--uploads", "upload_directory", required=True, type=click.Path(file_okay=False, path_type=Path))
    @click.option("--dry-run", is_flag=True, help="Validate the local source without connecting to cloud services.")
    def migrate_local_command(database_path, upload_directory, dry_run):
        from .transfer import inspect_local, migrate_local
        try:
            snapshot = inspect_local(database_path, upload_directory)
            if not dry_run:
                migrate_local(snapshot)
        except (ValidationError, StorageUnavailable, sqlite3.Error, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        except PsycopgError as exc:
            raise click.ClickException(
                "Cloud migration did not confirm completion. Check the cloud ledger before retrying; "
                "the local source is unchanged."
            ) from exc
        counts = ", ".join(f"{len(snapshot[table])} {table}" for table in ("clients", "trades", "payments", "uploads", "assets"))
        click.echo(("Preflight passed: " if dry_run else "Migration completed: ") + counts + ".")
        click.echo("The local database and original evidence were not modified. Cloud credentials and share links stay separate.")

    @app.cli.command("cleanup-uploads")
    @click.option("--dry-run", is_flag=True, help="Count abandoned uploads without deleting files or records.")
    def cleanup_uploads(dry_run):
        database = get_db()
        cutoff = int(time.time()) - 24 * 60 * 60
        where = "upload_id IS NULL AND expires_at<?"
        if dry_run:
            count = database.execute(f"SELECT COUNT(*) AS total FROM upload_assets WHERE {where}", (cutoff,)).fetchone()["total"]
            click.echo(f"{count} unsubmitted upload(s) expired more than 24 hours ago.")
            return
        removed = 0
        while True:
            locking = " FOR UPDATE SKIP LOCKED" if database.postgres else ""
            rows = database.execute(
                f"SELECT path FROM upload_assets WHERE {where} ORDER BY path LIMIT 100" + locking, (cutoff,)
            ).fetchall()
            if not rows:
                break
            paths = [row["path"] for row in rows]
            try:
                get_storage().remove(paths)
            except (StorageUnavailable, ValidationError, OSError) as exc:
                raise click.ClickException(str(exc)) from exc
            database.execute(
                "DELETE FROM upload_assets WHERE upload_id IS NULL AND path IN "
                f"({','.join('?' for _ in paths)})",
                paths,
            )
            database.commit()
            removed += len(paths)
        click.echo(f"Removed {removed} abandoned upload(s). Submitted evidence was not removed.")

    @app.route("/portal")
    @client_login_required
    def client_portal():
        cid = session["client_id"]
        client = get_client(cid)
        trades = query_trades({"client_id": cid})
        shares = get_client_calculated_holdings(cid, get_db(), sync_trades=True)

        prices = get_cached_prices(get_db())
        enriched_shares = []
        total_holdings_invested = ZERO
        total_holdings_current_value = ZERO
        deducted_holdings_cost = ZERO

        for share in shares:
            s_dict = dict(share)
            qty = dec(s_dict.get("quantity"))
            buy_p = dec(s_dict.get("buy_price"))
            cost = qty * buy_p
            total_holdings_invested += cost
            if s_dict.get("deduct_from_funds"):
                deducted_holdings_cost += cost

            stock_key = (s_dict.get("stock") or "").strip().upper()
            quote = prices.get(stock_key)
            if quote:
                cmp = quote["current_price"]
                cur_val = qty * cmp
                unrealized_pnl = cur_val - cost
                unrealized_pct = (unrealized_pnl / cost * 100) if cost else ZERO
                s_dict.update(
                    cmp=cmp,
                    current_value=cur_val,
                    unrealized_pnl=unrealized_pnl,
                    unrealized_pct=unrealized_pct,
                    price_change=quote.get("change", ZERO),
                    price_change_pct=quote.get("change_pct", ZERO),
                    price_updated_at=quote.get("updated_at"),
                )
                total_holdings_current_value += cur_val
            else:
                s_dict.update(
                    cmp=None,
                    current_value=cost,
                    unrealized_pnl=ZERO,
                    unrealized_pct=ZERO,
                    price_change=ZERO,
                    price_change_pct=ZERO,
                    price_updated_at=None,
                )
                total_holdings_current_value += cost
            tv_map = {
                "TECHNOCRAF": "BSE:544864",
                "TECHNOCRAFT": "BSE:544864",
                "TECHNOCRAFT VENTURES": "BSE:544864",
                "TIIL": "NSE:TIIL",
                "V2 RETAIL": "NSE:V2RETAIL",
                "V2": "NSE:V2RETAIL",
                "V2RETAIL": "NSE:V2RETAIL",
                "BLS": "NSE:BLS",
                "BLS POLYMERS": "NSE:BLSPOLY",
                "BLSPOLY": "NSE:BLSPOLY",
                "SBC": "NSE:SBC",
                "SBC EXPORTS": "NSE:SBC",
            }
            s_dict["tv_symbol"] = tv_map.get(stock_key, f"NSE:{stock_key}")
            enriched_shares.append(s_dict)

        total_unrealized_pnl = total_holdings_current_value - total_holdings_invested
        total_unrealized_pct = (total_unrealized_pnl / total_holdings_invested * 100) if total_holdings_invested else ZERO

        summary = summarize(trades)
        try:
            funds = get_db().execute("SELECT * FROM funds WHERE client_id=? ORDER BY added_on DESC, id DESC", (cid,)).fetchall()
        except Exception:
            funds = []
        total_funds = sum((dec(f["amount"]) for f in funds), ZERO)
        money_in_bank = total_funds - deducted_holdings_cost

        expire_old_alerts(get_db())
        raw_undismissed_alerts = get_db().execute(
            """
            SELECT a.* FROM client_alerts a
            LEFT JOIN client_alert_views v ON v.alert_id=a.id AND v.client_id=?
            WHERE (a.client_id=? OR a.client_id IS NULL)
              AND a.active=1
              AND (v.acknowledged IS NULL OR v.acknowledged=0)
              AND (v.dismissed IS NULL OR v.dismissed=0)
            ORDER BY a.id ASC
            """,
            (cid, cid),
        ).fetchall()

        alias_map = get_alias_mapping(get_db())
        # For broadcast SELL recommendations, only deliver pop-up to clients who currently hold the stock
        held_canonical_stocks = {
            alias_map.get((sh.get("stock") or "").strip().upper(), (sh.get("stock") or "").strip().upper())
            for sh in shares
            if dec(sh.get("quantity") or 0) > ZERO
        }
        now_utc = datetime.now(timezone.utc)
        undismissed_alerts = []
        for a in raw_undismissed_alerts:
            created_dt = parse_utc_datetime(a.get("created_at"))
            # Pop notifications expire after 1 day (24 hours) so clients never see old notifications
            if created_dt and (now_utc - created_dt).total_seconds() >= 86400:
                continue
            if a["action"] == "SELL" and a["client_id"] is None:
                a_stock = alias_map.get((a.get("stock") or "").strip().upper(), (a.get("stock") or "").strip().upper())
                if a_stock not in held_canonical_stocks:
                    continue
            undismissed_alerts.append(a)

        # Fetch price targets for client's held stocks
        all_targets = {}
        try:
            target_rows = get_db().execute("SELECT * FROM stock_price_targets ORDER BY target_price ASC").fetchall()
            for tr in target_rows:
                s_key = alias_map.get((tr["stock"] or "").strip().upper(), (tr["stock"] or "").strip().upper())
                all_targets.setdefault(s_key, []).append(dict(tr))
        except Exception:
            all_targets = {}

        eff_ctype, eff_rate = effective_rule(client)
        show_advisory_agreement = float(eff_rate) < 35.0
        payment_path = url_for("client_pay", token=payment_signer().dumps(cid))
        public_origin = app.config["PUBLIC_BASE_URL"] or request.host_url.rstrip("/")

        return render_template(
            "portal.html",
            client=client,
            shares=enriched_shares,
            trades=trades,
            s=summary,
            funds=funds,
            total_funds=total_funds,
            money_in_bank=money_in_bank,
            total_holdings_invested=total_holdings_invested,
            total_holdings_current_value=total_holdings_current_value,
            total_unrealized_pnl=total_unrealized_pnl,
            total_unrealized_pct=total_unrealized_pct,
            undismissed_alerts=undismissed_alerts,
            public_token=portal_token(),
            today=date.today().isoformat(),
            known_stocks=get_known_stocks(get_db()),
            stock_targets=all_targets,
            rule=rule_label(eff_ctype, eff_rate),
            show_advisory_agreement=show_advisory_agreement,
        )

    @app.route("/portal/shares/new", methods=["POST"])
    @client_login_required
    def portal_share_add():
        cid = session["client_id"]
        db = get_db()
        stock = resolve_canonical_stock(db, request.form.get("stock", ""))
        if not stock:
            flash("Stock symbol is required.", "error")
            return redirect(url_for("client_portal"))
        try:
            quantity = int(round(number(request.form.get("quantity"), "Quantity", positive=True))) if request.form.get("quantity") else 1
            buy_price = number(request.form.get("buy_price"), "Buy Price") if request.form.get("buy_price") else None
            raw_deduct = request.form.get("deduct_from_funds")
            deduct_from_funds = 1 if raw_deduct in (1, "1", "true", "True", "on", True) else 0
            insert_trades(cid, [{
                "stock": stock,
                "quantity": quantity,
                "buy_price": buy_price,
                "sell_price": None,
                "buy_date": date.today().isoformat() if buy_price is not None else None,
                "notes": "Added from Client Portal",
                "deduct_from_funds": deduct_from_funds,
            }], source="portal")

            # Update client_shares with weighted average price
            existing = db.execute(
                "SELECT id, quantity, buy_price FROM client_shares WHERE client_id=? AND UPPER(TRIM(stock))=?",
                (cid, stock.upper())
            ).fetchone()
            if existing:
                old_qty = dec(existing["quantity"] or 0)
                old_bp = dec(existing["buy_price"] or 0)
                add_qty = dec(quantity)
                add_bp = dec(buy_price or old_bp)
                new_qty = int(round(old_qty + add_qty))
                if new_qty > 0 and (old_qty * old_bp + add_qty * add_bp) > 0:
                    new_avg = ((old_qty * old_bp + add_qty * add_bp) / dec(new_qty)).quantize(CENT, rounding=ROUND_HALF_UP)
                else:
                    new_avg = add_bp
                db.execute("UPDATE client_shares SET quantity=?, buy_price=?, deduct_from_funds=? WHERE id=?", (new_qty, new_avg, deduct_from_funds, existing["id"]))
            else:
                db.execute(
                    "INSERT INTO client_shares (client_id, stock, quantity, buy_price, added_on, deduct_from_funds) VALUES (?, ?, ?, ?, ?, ?)",
                    (cid, stock, quantity, buy_price, date.today().isoformat(), deduct_from_funds)
                )

            flash(f"Trade for {stock} recorded. Your holdings have been updated.", "success")
        except Exception as exc:
            current_app.logger.warning("Failed to add portal share: %s", exc)
            flash(f"Could not record holding: {exc}", "error")
        return redirect(url_for("client_portal"))

    @app.route("/portal/trades/new", methods=["POST"])
    @client_login_required
    def portal_trade_add():
        cid = session["client_id"]
        client = get_client(cid)
        db = get_db()
        raw_stock = request.form.get("stock", "").strip()
        stock = resolve_canonical_stock(db, raw_stock) if raw_stock else ""
        if not stock:
            flash("Stock symbol is required.", "error")
            return redirect(url_for("client_portal"))

        try:
            quantity = int(round(number(request.form.get("quantity"), "Quantity", required=True, positive=True)))
        except Exception as exc:
            flash(f"Invalid quantity: {exc}", "error")
            return redirect(url_for("client_portal"))

        raw_bp = (request.form.get("buy_price") or "").strip()
        raw_sp = (request.form.get("sell_price") or "").strip()

        buy_price = None
        if raw_bp:
            try:
                buy_price = number(raw_bp, "Buy Price")
            except Exception as exc:
                flash(f"Invalid buy price: {exc}", "error")
                return redirect(url_for("client_portal"))

        sell_price = None
        if raw_sp:
            try:
                sell_price = number(raw_sp, "Sell Price", positive=True)
            except Exception as exc:
                flash(f"Invalid sell price: {exc}", "error")
                return redirect(url_for("client_portal"))

        raw_deduct = request.form.get("deduct_from_funds")
        deduct_from_funds = 1 if raw_deduct in (1, "1", "true", "True", "on", True) else 0

        # Both buy and sell cannot be empty
        if buy_price is None and sell_price is None:
            flash("Please enter either a Buy Price or a Sell Price. Both cannot be empty.", "error")
            return redirect(url_for("client_portal"))

        raw_bdate = (request.form.get("buy_date") or "").strip()
        buy_date = valid_date(raw_bdate, "Buy Date") if raw_bdate else (date.today().isoformat() if buy_price is not None else None)

        raw_sdate = (request.form.get("sell_date") or "").strip()
        sell_date = valid_date(raw_sdate, "Sell Date") if raw_sdate else (date.today().isoformat() if sell_price is not None else None)

        user_notes = text(request.form.get("notes"), "Notes", limit=200) or ""

        # Handle optional proof image upload
        proof_file = request.files.get("proof_image")
        upload_id = None
        if proof_file and proof_file.filename:
            content = proof_file.read()
            if len(content) > 0:
                mimetype = proof_file.mimetype or "image/png"
                if mimetype not in IMAGE_TYPES:
                    ext = os.path.splitext(proof_file.filename)[1].lower()
                    if ext in (".jpg", ".jpeg"):
                        mimetype = "image/jpeg"
                    elif ext == ".png":
                        mimetype = "image/png"
                    elif ext == ".webp":
                        mimetype = "image/webp"
                    else:
                        mimetype = "image/png"

                try:
                    validate_image(content, mimetype)
                    storage_path = "evidence/" + uuid.uuid4().hex + IMAGE_TYPES[mimetype][1]
                    storage = get_storage()
                    storage.store(storage_path, content, mimetype)

                    sub_uuid = str(uuid.uuid4())
                    sub_hash = hashlib.sha256(content).hexdigest()
                    owner = f"client:{cid}"
                    up_row = db.execute(
                        """INSERT INTO uploads (
                            client_name, client_id, filename, platform, trade_count,
                            submission_id, submission_hash, submission_owner
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id""",
                        (client["name"], cid, storage_path, "Client Portal Add Trade", 1, sub_uuid, sub_hash, owner),
                    ).fetchone()
                    upload_id = up_row["id"]

                    db.execute(
                        """INSERT INTO upload_assets (path, owner_hash, scope_hash, content_type, byte_size, expires_at, upload_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (storage_path, owner, "portal", mimetype, len(content), int(time.time()) + 365 * 86400, upload_id),
                    )
                except Exception as exc:
                    current_app.logger.warning("Trade screenshot upload skipped: %s", exc)

        # -------------------------------------------------------------
        # HOLDINGS & TRADE LOGIC:
        # 1. When selling and guest hasn't entered buy price:
        #    Infer buy price from the client's current holding average price,
        #    so realized P&L is calculated as sell_price - avg_price.
        #    Deduct sold quantity from holdings.
        # 2. When client adds a new buy trade:
        #    Update the holding in client_shares, recalculating weighted avg price.
        # -------------------------------------------------------------
        # -------------------------------------------------------------
        # HOLDINGS & TRADE LOGIC:
        # If sell_price is present: apply FIFO deduction against open lots
        # If buy_price is present and sell_price is None: insert open buy trade
        # -------------------------------------------------------------
        if sell_price is not None:
            trade_ids = apply_sale_fifo(
                client_id=cid,
                stock=stock,
                sell_quantity=quantity,
                sell_price=sell_price,
                sell_date=sell_date,
                buy_price_hint=buy_price,
                notes=user_notes or "Sold via Client Portal",
                upload_id=upload_id,
                source="portal",
                db=db,
            )
            trade_id = trade_ids[0] if trade_ids else None
        else:
            # Buy-only trade (recording an open position)
            trade_ids = insert_trades(cid, [{
                "stock": stock,
                "quantity": quantity,
                "buy_price": buy_price,
                "sell_price": None,
                "buy_date": buy_date or date.today().isoformat(),
                "sell_date": None,
                "notes": user_notes or "Added via Client Portal",
                "deduct_from_funds": deduct_from_funds,
            }], source="portal", upload_id=upload_id)
            trade_id = trade_ids[0] if trade_ids else None
            sync_client_holdings_from_trades(cid, db)

        # Eagerly fetch live price quote for the stock
        try:
            quote = fetch_stock_quote(stock)
            if quote:
                now_ist_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
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
                    (stock, quote["current_price"], quote.get("previous_close"), quote.get("change"), quote.get("change_pct"), now_ist_str),
                )
        except Exception:
            pass

        if sell_price is not None:
            flash(f"Sale of {quantity} shares of {stock} recorded. Open holdings have been updated via FIFO.", "success")
        else:
            flash(f"Buy trade for {quantity} shares of {stock} recorded. Your holdings and average buy price have been updated.", "success")

        invalidate_prices_cache()
        invalidate_stock_caches()
        db.commit()
        return redirect(url_for("client_portal"))

    @app.route("/portal/trades/<int:trade_id>/delete-screenshot", methods=["POST"])
    @client_login_required
    def portal_trade_delete_screenshot(trade_id):
        cid = session["client_id"]
        db = get_db()
        trade = db.execute("SELECT * FROM trades WHERE id=? AND client_id=?", (trade_id, cid)).fetchone()
        if not trade:
            flash("Trade not found or unauthorized.", "error")
            return redirect(url_for("client_portal"))

        upload_id = trade["upload_id"]
        if not upload_id:
            flash("No screenshot attached to this trade.", "info")
            return redirect(url_for("client_portal"))

        stock_name = trade["stock"]
        db.execute("UPDATE trades SET upload_id=NULL WHERE id=? AND client_id=?", (trade_id, cid))

        other_trades = db.execute("SELECT COUNT(*) AS c FROM trades WHERE upload_id=?", (upload_id,)).fetchone()
        count = other_trades["c"] if other_trades else 0
        if count == 0:
            try:
                assets = db.execute("SELECT path FROM upload_assets WHERE upload_id=?", (upload_id,)).fetchall()
                paths = [a["path"] for a in assets if a and a["path"]]
                if paths:
                    try:
                        storage = get_storage()
                        storage.remove(paths)
                    except Exception as storage_exc:
                        current_app.logger.warning("Failed to remove storage files for upload %s: %s", upload_id, storage_exc)
                db.execute("DELETE FROM upload_assets WHERE upload_id=?", (upload_id,))
                db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
            except Exception as exc:
                current_app.logger.warning("Error cleaning up orphaned upload record %s: %s", upload_id, exc)

        db.commit()
        flash(f"Proof screenshot removed from {stock_name}.", "success")
        return redirect(url_for("client_portal"))

    @app.route("/portal/funds/adjust", methods=["POST"])
    @client_login_required
    def portal_funds_adjust():
        cid = session["client_id"]
        client = get_client(cid)
        action = request.form.get("action", "add").strip().lower()
        amount = number(request.form.get("amount"), "Amount", required=True, positive=True, places=2)
        added_on = valid_date(request.form.get("date"), "Date") or date.today().isoformat()
        user_notes = text(request.form.get("notes"), "Notes", limit=200) or ""

        if action == "remove":
            fund_amount = -amount
            note_prefix = "[Client Withdrawal]"
            action_text = f"Withdrew ₹{amount:,.2f}"
        else:
            fund_amount = amount
            note_prefix = "[Client Deposit]"
            action_text = f"Added ₹{amount:,.2f}"

        notes_entry = f"{note_prefix} {user_notes}".strip()

        get_db().execute(
            "INSERT INTO funds (client_id, amount, added_on, notes) VALUES (?, ?, ?, ?)",
            (cid, fund_amount, added_on, notes_entry),
        )
        flash(f"{action_text} recorded in your funds ledger.", "success")
        return redirect(url_for("client_portal"))

    @app.route("/portal/set-username", methods=["POST"])
    @client_login_required
    def portal_set_username():
        cid = session["client_id"]
        client = get_client(cid)
        # Client can only set their username one time only
        if client.get("username"):
            flash("Your username has already been set and is permanently locked. Only your administrator can modify it.", "error")
            return redirect(url_for("client_portal"))

        raw_username = request.form.get("username", "").strip()
        username = raw_username.lstrip("@").strip().lower()
        if not username:
            flash("Please enter a username.", "error")
            return redirect(url_for("client_portal"))

        if len(username) < 3 or len(username) > 30:
            flash("Username must be between 3 and 30 characters.", "error")
            return redirect(url_for("client_portal"))

        if not re.match(r"^[a-zA-Z0-9_.]+$", username):
            flash("Username can only contain letters, numbers, underscores, and dots.", "error")
            return redirect(url_for("client_portal"))

        existing = get_db().execute("SELECT id FROM clients WHERE lower(username)=lower(?) AND id<>?", (username, cid)).fetchone()
        if existing:
            flash(f"The username @{username} is already taken. Please choose another one.", "error")
            return redirect(url_for("client_portal"))

        get_db().execute("UPDATE clients SET username=? WHERE id=?", (username, cid))
        flash(f"Your username @{username} has been saved and locked! You can now use this handle to sign into your portfolio.", "success")
        return redirect(url_for("client_portal"))

    @app.route("/portal/password/update", methods=["POST"])
    @client_login_required
    def client_update_password():
        cid = session["client_id"]
        new_pwd = request.form.get("new_password", "").strip()
        confirm_pwd = request.form.get("confirm_password", "").strip()

        if len(new_pwd) < 8:
            flash("New password must be at least 8 characters long.", "error")
            return redirect(url_for("client_portal"))

        if new_pwd != confirm_pwd:
            flash("Passwords do not match. Please re-enter carefully.", "error")
            return redirect(url_for("client_portal"))

        p_hash = generate_password_hash(new_pwd)
        get_db().execute(
            "UPDATE clients SET password_hash=?, password_plain=?, password_changed=1 WHERE id=?",
            (p_hash, new_pwd, cid),
        )
        flash("Password updated successfully! Keep this password safe for your future logins.", "success")
        return redirect(url_for("client_portal"))

    @app.route("/api/alerts/<int:aid>/acknowledge", methods=["POST"])
    @app.route("/api/alerts/<int:aid>/dismiss", methods=["POST"])
    @client_login_required
    def api_alert_acknowledge(aid):
        cid = session["client_id"]
        get_db().execute(
            """
            INSERT INTO client_alert_views (alert_id, client_id, dismissed, acknowledged, acknowledged_at, viewed_at)
            VALUES (?, ?, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(alert_id, client_id) DO UPDATE SET 
                dismissed=1,
                acknowledged=1,
                acknowledged_at=CURRENT_TIMESTAMP,
                viewed_at=CURRENT_TIMESTAMP
            """,
            (aid, cid),
        )
        return jsonify(success=True, acknowledged=True)

    @app.route("/api/stocks/suggestions", methods=["GET"])
    def api_stock_suggestions():
        return jsonify(get_stock_suggestions(get_db()))

    @app.route("/alerts", methods=["GET"])
    @login_required
    def alerts_list():
        db = get_db()
        expire_old_alerts(db)
        alerts = db.execute(
            """
            SELECT a.*, c.name as client_name,
                   (SELECT COUNT(*) FROM client_alert_views v WHERE v.alert_id=a.id AND (v.dismissed=1 OR v.acknowledged=1)) as view_count
            FROM client_alerts a
            LEFT JOIN clients c ON c.id=a.client_id
            ORDER BY a.created_at DESC
            """
        ).fetchall()
        clients = db.execute("SELECT id, name FROM clients WHERE active=1 ORDER BY lower(name)").fetchall()

        views = db.execute(
            """
            SELECT v.alert_id, v.client_id, v.acknowledged_at, v.viewed_at, c.name as client_name
            FROM client_alert_views v
            JOIN clients c ON c.id = v.client_id
            WHERE (v.dismissed = 1 OR v.acknowledged = 1)
            ORDER BY lower(c.name)
            """
        ).fetchall()

        # Batch-fetch all active clients' non-zero holdings in ONE query to avoid N+1 queries per SELL alert
        all_holdings = db.execute(
            """
            SELECT h.client_id, c.name as client_name, h.stock
            FROM client_shares h
            JOIN clients c ON c.id = h.client_id
            WHERE c.active = 1 AND h.quantity > 0
            ORDER BY lower(c.name)
            """
        ).fetchall()

        alias_map = get_alias_mapping(db)
        stock_holders_map = {}
        for h in all_holdings:
            sym = (h["stock"] or "").strip().upper()
            canon = alias_map.get(sym, sym)
            stock_holders_map.setdefault(canon, {})[h["client_id"]] = h["client_name"]

        ack_by_alert = {}
        for v in views:
            ack_by_alert.setdefault(v["alert_id"], []).append(v)

        active_clients_list = [dict(c) for c in clients]
        active_client_map = {c["id"]: c["name"] for c in clients}

        now_utc = datetime.now(timezone.utc)
        enriched_alerts = []
        for a in alerts:
            ad = dict(a)
            created_dt = parse_utc_datetime(ad.get("created_at"))
            is_expired = False
            if created_dt and (now_utc - created_dt).total_seconds() >= 86400:
                is_expired = True

            ad["is_expired"] = is_expired
            if is_expired:
                ad["active"] = 0

            ad["created_at_ist"] = format_ist(ad.get("created_at"), "short")
            ad["created_at_full_ist"] = format_ist(ad.get("created_at"), "full")

            acks = ack_by_alert.get(ad["id"], [])
            acked_client_ids = {v["client_id"] for v in acks}
            acked_clients = [
                {
                    "id": v["client_id"],
                    "name": v["client_name"],
                    "acknowledged_at": v.get("acknowledged_at") or v.get("viewed_at"),
                    "acknowledged_at_ist": format_ist(v.get("acknowledged_at") or v.get("viewed_at"), "short"),
                    "acknowledged_at_time_ist": format_ist(v.get("acknowledged_at") or v.get("viewed_at"), "time"),
                    "acknowledged_at_chip_ist": format_ist(v.get("acknowledged_at") or v.get("viewed_at"), "chip"),
                    "acknowledged_at_full_ist": format_ist(v.get("acknowledged_at") or v.get("viewed_at"), "full"),
                }
                for v in acks
            ]

            if ad.get("client_id"):
                cid = ad["client_id"]
                cname = ad.get("client_name") or active_client_map.get(cid, f"Client #{cid}")
                target_clients = [{"id": cid, "name": cname}]
                if cid in acked_client_ids:
                    pending_clients = []
                else:
                    pending_clients = [{"id": cid, "name": cname}]
            elif ad.get("action") == "SELL":
                # Sell pop-up broadcast: only sent to clients who hold the given stock
                canon_stk = alias_map.get((ad.get("stock") or "").strip().upper(), (ad.get("stock") or "").strip().upper())
                holder_map = dict(stock_holders_map.get(canon_stk, {}))
                for v in acks:
                    if v["client_id"] not in holder_map:
                        holder_map[v["client_id"]] = v["client_name"]
                target_clients = [{"id": cid, "name": holder_map[cid]} for cid in sorted(holder_map.keys(), key=lambda i: holder_map[i].lower())]
                pending_clients = [c for c in target_clients if c["id"] not in acked_client_ids]
            else:
                target_clients = active_clients_list
                pending_clients = [c for c in active_clients_list if c["id"] not in acked_client_ids]

            ad["target_count"] = len(target_clients)
            ad["ack_count"] = len(acked_clients)
            ad["acknowledged_clients"] = acked_clients
            ad["pending_clients"] = pending_clients
            ad["all_acknowledged"] = (len(target_clients) > 0 and len(pending_clients) == 0)
            ad["is_sell_broadcast"] = (ad.get("action") == "SELL" and not ad.get("client_id"))
            enriched_alerts.append(ad)

        ensure_push_tables(db)
        try:
            push_subs_count = db.execute("SELECT COUNT(*) AS c FROM client_push_subscriptions").fetchone()["c"]
        except Exception:
            push_subs_count = 0

        return render_template("alerts.html", alerts=enriched_alerts, clients=clients, push_subs_count=push_subs_count)

    def ensure_push_tables(db):
        try:
            if db.postgres:
                db.execute("""
                CREATE TABLE IF NOT EXISTS client_push_subscriptions (
                    id BIGSERIAL PRIMARY KEY,
                    client_id BIGINT REFERENCES clients(id) ON DELETE CASCADE,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
                """)
                db.execute("CREATE INDEX IF NOT EXISTS idx_push_client ON client_push_subscriptions(client_id)")
            else:
                db.execute("""
                CREATE TABLE IF NOT EXISTS client_push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """)
                db.execute("CREATE INDEX IF NOT EXISTS idx_push_client ON client_push_subscriptions(client_id)")
        except Exception as exc:
            current_app.logger.warning("ensure_push_tables notice: %s", exc)

    def dispatch_web_push(client_id=None, title="Trade Alert", body="New trade recommendation", url="/portal", stock=None, action=None):
        db = get_db()
        ensure_push_tables(db)
        try:
            if client_id is not None:
                subs = db.execute("SELECT id, endpoint, p256dh, auth FROM client_push_subscriptions WHERE client_id=?", (client_id,)).fetchall()
            else:
                subs = db.execute("SELECT id, endpoint, p256dh, auth FROM client_push_subscriptions").fetchall()
        except Exception as exc:
            current_app.logger.warning("Push query failed: %s", exc)
            return 0

        if not subs:
            return 0

        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            current_app.logger.warning("pywebpush not installed, skipping push dispatch")
            return 0

        vapid_priv = app.config.get("VAPID_PRIVATE_KEY")
        vapid_claims = {"sub": app.config.get("VAPID_CLAIM_EMAIL", "mailto:admin@commissionbook.com")}

        payload = json.dumps({
            "title": title,
            "body": body,
            "icon": "/static/icon-192.png?v=tree",
            "badge": "/static/icon-192.png?v=tree",
            "url": url,
            "tag": f"trade-{stock or 'alert'}",
            "data": {"url": url}
        })

        sent_count = 0
        expired_ids = []
        for sub in subs:
            sub_info = {
                "endpoint": sub["endpoint"],
                "keys": {
                    "p256dh": sub["p256dh"],
                    "auth": sub["auth"],
                }
            }
            try:
                push_headers = {
                    "Urgency": "high",
                    "Topic": f"trade-{re.sub(r'[^a-zA-Z0-9_-]', '', stock or 'alert')[:30]}"
                }
                webpush(
                    subscription_info=sub_info,
                    data=payload,
                    vapid_private_key=vapid_priv,
                    vapid_claims=vapid_claims,
                    ttl=86400,
                    headers=push_headers,
                )
                sent_count += 1
            except WebPushException as ex:
                current_app.logger.warning("WebPush send failed: %s", ex)
                if hasattr(ex, "response") and ex.response is not None and ex.response.status_code in (404, 410):
                    expired_ids.append(sub["id"])
            except Exception as ex:
                current_app.logger.warning("WebPush unexpected error: %s", ex)

        if expired_ids:
            try:
                for eid in expired_ids:
                    db.execute("DELETE FROM client_push_subscriptions WHERE id=?", (eid,))
                db.commit()
            except Exception:
                pass

        return sent_count

    @app.route("/sw.js")
    def service_worker():
        response = send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    @app.route("/api/push/public-key")
    def push_public_key():
        return jsonify(publicKey=app.config.get("VAPID_PUBLIC_KEY", ""))

    @app.route("/portal/push/subscribe", methods=["POST"])
    @client_login_required
    def portal_push_subscribe():
        cid = session["client_id"]
        data = request.get_json(silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip()
        keys = data.get("keys") or {}
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
        user_agent = (request.headers.get("User-Agent") or "")[:250]

        if not endpoint or not p256dh or not auth:
            return jsonify(ok=False, error="Invalid subscription payload"), 400

        db = get_db()
        ensure_push_tables(db)
        if db.postgres:
            db.execute(
                """
                INSERT INTO client_push_subscriptions (client_id, endpoint, p256dh, auth, user_agent)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (endpoint) DO UPDATE SET
                    client_id = EXCLUDED.client_id,
                    p256dh = EXCLUDED.p256dh,
                    auth = EXCLUDED.auth,
                    user_agent = EXCLUDED.user_agent,
                    created_at = CURRENT_TIMESTAMP
                """,
                (cid, endpoint, p256dh, auth, user_agent)
            )
        else:
            db.execute(
                """
                INSERT INTO client_push_subscriptions (client_id, endpoint, p256dh, auth, user_agent)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (endpoint) DO UPDATE SET
                    client_id = excluded.client_id,
                    p256dh = excluded.p256dh,
                    auth = excluded.auth,
                    user_agent = excluded.user_agent,
                    created_at = CURRENT_TIMESTAMP
                """,
                (cid, endpoint, p256dh, auth, user_agent)
            )
        db.commit()
        return jsonify(ok=True, message="Push notifications enabled")

    @app.route("/portal/push/unsubscribe", methods=["POST"])
    @client_login_required
    def portal_push_unsubscribe():
        data = request.get_json(silent=True) or {}
        endpoint = (data.get("endpoint") or "").strip()
        if endpoint:
            db = get_db()
            ensure_push_tables(db)
            db.execute("DELETE FROM client_push_subscriptions WHERE endpoint=?", (endpoint,))
            db.commit()
        return jsonify(ok=True)

    @app.route("/admin/push/test", methods=["POST"])
    @login_required
    def admin_push_test():
        client_id_raw = (request.form.get("client_id") or "").strip()
        client_id = int(client_id_raw) if client_id_raw and client_id_raw != "all" else None
        target_name = get_client(client_id)["name"] if client_id else "All Subscribed Clients"
        sent = dispatch_web_push(
            client_id=client_id,
            title="🔔 Alder & Vane Test Alert",
            body="Push notifications are active and working on this device!",
            url="/portal"
        )
        if sent > 0:
            flash(f"Test push notification dispatched to {sent} active device(s) for {target_name}.", "success")
        else:
            flash(f"No active push subscriptions found for {target_name}. Ask the client to tap 'Enable Alerts' on their portal first.", "info")
        return redirect(safe_redirect_target(request.referrer, url_for("alerts_list")))

    @app.route("/alerts/new", methods=["POST"])
    @login_required
    def alert_create():
        action = request.form.get("action", "BUY").upper()
        if action not in ("BUY", "SELL"):
            abort(400)
        stock = request.form.get("stock", "").strip().upper()
        back = request.form.get("back")
        if not stock:
            flash("Stock symbol is required.", "error")
            return redirect(safe_redirect_target(back, url_for("alerts_list")))
        price = number(request.form.get("price"), "Price", positive=True) if request.form.get("price") else None
        target_price = number(request.form.get("target_price"), "Target Price", positive=True) if request.form.get("target_price") else None
        stop_loss = number(request.form.get("stop_loss"), "Stop Loss", positive=True) if request.form.get("stop_loss") else None
        message = request.form.get("message", "").strip()
        client_id_raw = request.form.get("client_id", "").strip()
        client_id = int(client_id_raw) if client_id_raw and client_id_raw != "all" else None
        canonical_stock = resolve_canonical_stock(get_db(), stock)

        get_db().execute(
            """
            INSERT INTO client_alerts (client_id, action, stock, price, target_price, stop_loss, message, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (client_id, action, canonical_stock, price, target_price, stop_loss, message),
        )

        send_push = request.form.get("send_push", "1") == "1"
        push_sent = 0
        if send_push:
            action_verb = "BUY" if action == "BUY" else "SELL"
            push_title = f"📢 Trade Advisory: {action_verb} {canonical_stock}"
            details = []
            if price:
                details.append(f"Price: ₹{price:,.2f}")
            if target_price:
                details.append(f"Target: ₹{target_price:,.2f}")
            if stop_loss:
                details.append(f"SL: ₹{stop_loss:,.2f}")
            push_body = " | ".join(details) if details else (message or "New trade recommendation posted.")
            if message and details:
                push_body += f"\n{message}"

            if client_id is not None:
                push_sent = dispatch_web_push(client_id=client_id, title=push_title, body=push_body, stock=canonical_stock, action=action)
            elif action == "SELL":
                holding_clients = get_clients_holding_stock(canonical_stock, get_db())
                for hc in holding_clients:
                    push_sent += dispatch_web_push(client_id=hc["id"], title=push_title, body=push_body, stock=canonical_stock, action=action)
            else:
                push_sent = dispatch_web_push(client_id=None, title=push_title, body=push_body, stock=canonical_stock, action=action)

        push_note = f" (Pushed to {push_sent} device{'s' if push_sent != 1 else ''})" if push_sent > 0 else ""
        if client_id is not None:
            recipient = get_client(client_id)["name"]
            flash(f"Pop-up trade for {canonical_stock} ({action}) sent to {recipient}{push_note}.", "success")
        elif action == "SELL":
            holding_clients = get_clients_holding_stock(canonical_stock, get_db())
            if holding_clients:
                names = ", ".join(h["name"] for h in holding_clients[:3])
                suffix = f" (+{len(holding_clients) - 3} more)" if len(holding_clients) > 3 else ""
                flash(f"Pop-up SELL advisory for {canonical_stock} sent to {len(holding_clients)} client(s) holding this stock: {names}{suffix}{push_note}.", "success")
            else:
                flash(f"Pop-up SELL advisory for {canonical_stock} recorded. Notice: No clients currently hold this stock, so no client portals will be prompted until holdings exist.", "info")
        else:
            recipient = "All clients (Broadcast)"
            flash(f"Pop-up trade for {canonical_stock} ({action}) sent to {recipient}{push_note}.", "success")
        return redirect(safe_redirect_target(back, url_for("alerts_list")))

    @app.route("/alerts/<int:aid>/toggle", methods=["POST"])
    @login_required
    def alert_toggle(aid):
        alert = get_db().execute("SELECT * FROM client_alerts WHERE id=?", (aid,)).fetchone()
        if not alert:
            abort(404)
        created_dt = parse_utc_datetime(alert.get("created_at"))
        if created_dt and (datetime.now(timezone.utc) - created_dt).total_seconds() >= 86400:
            get_db().execute("UPDATE client_alerts SET active=0 WHERE id=?", (aid,))
            flash("This alert was sent more than 1 day ago and has expired. It cannot be reactivated.", "warning")
            return redirect(url_for("alerts_list"))
        new_status = 0 if alert["active"] else 1
        get_db().execute("UPDATE client_alerts SET active=? WHERE id=?", (new_status, aid))
        flash("Alert status updated.", "success")
        return redirect(url_for("alerts_list"))

    @app.route("/alerts/<int:aid>/delete", methods=["POST"])
    @login_required
    def alert_delete(aid):
        get_db().execute("DELETE FROM client_alerts WHERE id=?", (aid,))
        flash("Alert deleted.", "success")
        return redirect(url_for("alerts_list"))

    is_serverless = bool(
        os.environ.get("VERCEL")
        or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
        or os.environ.get("LAMBDA_TASK_ROOT")
    )
    if not is_serverless and not (config and config.get("TESTING")):
        scheduler = DailyPriceScheduler(app)
        scheduler.start()
        app.extensions["price_scheduler"] = scheduler

    return app
