import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal

import psycopg
from flask import current_app, g
from psycopg.rows import dict_row


DEFAULTS = {
    "default_commission_type": "profit_pct",
    "default_commission_rate": "10",
    "upload_code": "",
    "currency": "\u20b9",
    "business_name": "",
}

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE,
    phone TEXT UNIQUE, email TEXT, commission_type TEXT, commission_rate NUMERIC,
    notes TEXT, active INTEGER NOT NULL DEFAULT 1,
    portfolio_amount NUMERIC DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_name TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    filename TEXT, platform TEXT, ocr_text TEXT,
    trade_count INTEGER NOT NULL DEFAULT 0,
    submission_id TEXT UNIQUE, submission_hash TEXT, submission_owner TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    stock TEXT NOT NULL, quantity NUMERIC NOT NULL DEFAULT 1,
    buy_price NUMERIC, sell_price NUMERIC, buy_date TEXT, sell_date TEXT,
    commission_override NUMERIC,
    commission_type_snapshot TEXT, commission_rate_snapshot NUMERIC,
    source TEXT NOT NULL DEFAULT 'manual',
    upload_id INTEGER REFERENCES uploads(id) ON DELETE SET NULL,
    notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    amount NUMERIC NOT NULL, paid_on TEXT NOT NULL,
    mode TEXT, notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS funds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    amount NUMERIC NOT NULL, added_on TEXT NOT NULL,
    notes TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS upload_assets (
    path TEXT PRIMARY KEY,
    owner_hash TEXT NOT NULL, scope_hash TEXT NOT NULL,
    content_type TEXT NOT NULL, byte_size INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    upload_id INTEGER REFERENCES uploads(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_trades_client ON trades(client_id);
CREATE INDEX IF NOT EXISTS idx_payments_client ON payments(client_id);
CREATE INDEX IF NOT EXISTS idx_trades_dates ON trades(sell_date, buy_date);
CREATE INDEX IF NOT EXISTS idx_payments_date ON payments(paid_on);
CREATE INDEX IF NOT EXISTS idx_assets_upload ON upload_assets(upload_id);
CREATE INDEX IF NOT EXISTS idx_assets_owner ON upload_assets(owner_hash, expires_at);
"""


def _params(parameters):
    return tuple(str(p) if isinstance(p, Decimal) else p for p in parameters)


def _row(row):
    if row is None:
        return None
    result = dict(row)
    for key, value in result.items():
        if isinstance(value, (date, datetime)):
            result[key] = value.isoformat()
    return result


class Result:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def fetchone(self):
        return _row(self.cursor.fetchone())

    def fetchall(self):
        return [_row(row) for row in self.cursor.fetchall()]


class Database:
    def __init__(self, connection, postgres=False):
        self.connection = connection
        self.postgres = postgres

    def execute(self, sql, parameters=()):
        # All application SQL uses positional placeholders and no literal question marks.
        if self.postgres:
            return Result(self.connection.execute(sql.replace("?", "%s"), tuple(parameters)))
        return Result(self.connection.execute(sql, _params(parameters)))

    def commit(self):
        self.connection.commit()

    def rollback(self):
        # PostgreSQL has already aborted any open transaction when its connection is gone.
        if self.postgres and self.connection.closed:
            return
        self.connection.rollback()

    def close(self):
        self.connection.close()


def init_local(connection):
    connection.executescript(SQLITE_SCHEMA)
    connection.execute("BEGIN IMMEDIATE")
    backfill_upload_clients = False
    additions = {
        "uploads": {
            "client_id": "INTEGER REFERENCES clients(id) ON DELETE SET NULL",
            "submission_id": "TEXT",
            "submission_hash": "TEXT",
            "submission_owner": "TEXT",
        },
        "trades": {
            "commission_type_snapshot": "TEXT",
            "commission_rate_snapshot": "NUMERIC",
        },
        "clients": {
            "portfolio_amount": "NUMERIC DEFAULT 0",
        },
    }
    for table, columns in additions.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                if table == "uploads" and name == "client_id":
                    backfill_upload_clients = True
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_submission ON uploads(submission_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_uploads_client ON uploads(client_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_clients_name_ci ON clients(lower(name))")
    if backfill_upload_clients:
        connection.execute(
            """UPDATE uploads SET client_id=(
                SELECT c.id FROM clients c WHERE lower(c.name)=lower(uploads.client_name)
            ) WHERE client_id IS NULL"""
        )
    for key, value in DEFAULTS.items():
        connection.execute(
            "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
            (key, value),
        )
    connection.commit()


def get_db():
    if "db" not in g:
        config = current_app.config
        if config["BACKEND_MODE"] == "supabase":
            conn_kwargs = {
                "prepare_threshold": None,
                "connect_timeout": 10,
                "row_factory": dict_row,
            }
            if "sslmode" not in config["DATABASE_URL"]:
                conn_kwargs["sslmode"] = "require"
            connection = psycopg.connect(config["DATABASE_URL"], **conn_kwargs)
            g.db = Database(connection, postgres=True)
        else:
            connection = sqlite3.connect(config["DATABASE"], timeout=15)
            g.db = Database(connection)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 15000")
            state = current_app.extensions["commission_database"]
            with state["lock"]:
                if not state["initialized"]:
                    init_local(connection)
                    state["initialized"] = True
    return g.db


def complete_request(response):
    database = g.get("db")
    if database is not None:
        if response.status_code < 400:
            database.commit()
        else:
            database.rollback()
    return response


def close_db(_error=None):
    database = g.pop("db", None)
    if database is not None:
        try:
            database.rollback()
        finally:
            database.close()


def install_database(app):
    app.extensions["commission_database"] = {"lock": threading.Lock(), "initialized": False}
    app.after_request(complete_request)
    app.teardown_appcontext(close_db)
