import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import time
import urllib.request
import urllib.parse
import uuid
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
from functools import wraps
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import click
import psycopg
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
from .security import (
    configure_app, credential_version, csrf_token, local_setup_allowed, require_csrf,
    safe_redirect_target, secure_response,
)
from .storage import (
    DOWNLOAD_TTL, IMAGE_TYPES, MAX_CLOUD_IMAGE, MAX_LOCAL_IMAGE, UPLOAD_TTL,
    StorageUnavailable, get_storage, safe_path,
)
from .validation import (
    COMMISSION_TYPES, MAX_ROWS, MAX_TEXT, ValidationError, client_name,
    identifier, number, rule_values, submission_uuid, text, trade_values, valid_date,
)


ZERO = Decimal("0")
CENT = Decimal("0.01")
TRADE_SELECT = """
SELECT t.*, c.name AS client_name, c.commission_type, c.commission_rate
FROM trades t JOIN clients c ON c.id=t.client_id
"""
EXPORT_COLS = [
    ("Username", "client_name"), ("Stock", "stock"), ("Quantity", "quantity"),
    ("Buy Price", "buy_price"), ("Sell Price", "sell_price"), ("Buy Date", "buy_date"),
    ("Sell Date", "sell_date"), ("Status", "status"), ("Invested", "invested"),
    ("P&L", "pnl"), ("P&L %", "pnl_pct"), ("Commission", "commission"),
    ("Holding Days", "holding_days"), ("Source", "source"), ("Notes", "notes"),
]


def settings_values():
    if "settings_values" not in g:
        g.settings_values = {
            row["key"]: row["value"] for row in get_db().execute("SELECT key,value FROM settings").fetchall()
        }
    return g.settings_values


def setting(key, default=None):
    return settings_values().get(key, default)


def set_setting(key, value):
    get_db().execute(
        "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    settings_values()[key] = value


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
            if request.path.startswith("/api/"):
                abort(401, description="Administrator sign-in is required.")
            return redirect(url_for("login", next=request.path))
        return function(*args, **kwargs)
    return wrapper


def dec(value):
    return Decimal(str(value)) if value is not None else ZERO


def effective_rule(client):
    ctype = client["commission_type"] or setting("default_commission_type", "profit_pct")
    rate = client["commission_rate"]
    return ctype, dec(rate if rate is not None else setting("default_commission_rate", "10"))


def get_client(cid):
    client = get_db().execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
    if not client:
        abort(404, description="That user no longer exists.")
    return client


def get_or_create_client(name, phone=None):
    name = client_name(name)
    database = get_db()
    if phone:
        phone = text(phone, "Phone", limit=80)
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
    ctype, rate = effective_rule(get_client(cid))
    parameters = []
    for trade in trades:
        parameters.extend((
            cid, trade["stock"], trade["quantity"], trade["buy_price"], trade["sell_price"],
            trade["buy_date"], trade["sell_date"], trade.get("commission_override"),
            ctype, rate, source, upload_id, trade.get("notes"),
        ))
    rows = get_db().execute(
        """INSERT INTO trades(
            client_id,stock,quantity,buy_price,sell_price,buy_date,sell_date,commission_override,
            commission_type_snapshot,commission_rate_snapshot,source,upload_id,notes
        ) VALUES """ + ",".join("(?,?,?,?,?,?,?,?,?,?,?,?,?)" for _ in trades) + " RETURNING id",
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
    if result.get("commission_override") is not None:
        commission = result["commission_override"]
    elif not closed:
        commission = ZERO
    elif ctype == "turnover_pct":
        commission = (result["buy_price"] + result["sell_price"]) * quantity * rate / 100
    elif ctype == "flat":
        commission = rate
    else:
        commission = max(result["pnl"], ZERO) * rate / 100
    result["commission"] = commission.quantize(CENT, rounding=ROUND_HALF_UP)
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


def rule_label(ctype, rate):
    if not ctype or rate is None:
        return "Default"
    r = f"{dec(rate):.1f}".rstrip('0').rstrip('.')
    if ctype == "flat":
        return f"{r} flat/trade"
    return f"{r}{COMMISSION_TYPES[ctype]}"


def client_values(values, name):
    ctype, rate = rule_values(values.get("commission_type"), values.get("commission_rate"), allow_default=True)
    phone = text(values.get("phone"), "Phone", limit=80)
    if not phone:
        raise ValidationError("Phone number is required.")
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

    @app.before_request
    def authorize_request():
        g.decimal_context = localcontext()
        g.decimal_context.__enter__().prec = 60
        if session.get("admin"):
            _, version = admin_credentials()
            if not version or session.get("auth_version") != version:
                session.clear()
        require_csrf()

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

    @app.context_processor
    def inject_globals():
        error = g.get("error_response", False)
        return {
            "COMMISSION_TYPES": COMMISSION_TYPES, "logged_in": bool(session.get("admin")),
            "today": date.today().isoformat(), "app_name": "Commission Book",
            "business_name": "" if error else setting("business_name", ""),
            "currency": "\u20b9" if error else setting("currency", "\u20b9"),
            "backend_mode": app.config["BACKEND_MODE"],
            "deployed": app.config["DEPLOYED"],
            "password_managed": bool(app.config["ADMIN_PASSWORD_HASH"]),
            "admin_password_managed": bool(app.config["ADMIN_PASSWORD_HASH"]),
            "commission_policy": "snapshot",
            "upload_url": portal_url() if session.get("admin") and not error else None,
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
    @app.errorhandler(psycopg.IntegrityError)
    def database_conflict(error):
        app.logger.warning("A database constraint rejected a request (%s).", type(error).__name__)
        return error_response(409, "Conflicting entry", "This entry conflicts with existing data. Reload and try again.")

    @app.errorhandler(sqlite3.OperationalError)
    @app.errorhandler(psycopg.OperationalError)
    def unavailable_database(error):
        app.logger.error("Database operation failed (%s).", type(error).__name__)
        return error_response(503, "Ledger unavailable", "The database is unavailable. Try again shortly or check its configuration.")

    @app.errorhandler(500)
    def server_error(_error):
        return error_response(500, "Something went wrong", "Your request could not be completed. Please try again.")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        password_hash, version = admin_credentials()
        first_run = not password_hash
        if first_run and not local_setup_allowed():
            abort(503, description="An administrator must configure the password on the server before this site can be used.")
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) > 1024:
                raise ValidationError("Password is too long.")
            if first_run:
                if len(password) < 10 or password != request.form.get("confirm"):
                    flash("Password must be at least 10 characters and match confirmation.", "error")
                    return render_template("login.html", first_run=True), 400
                password_hash = generate_password_hash(password)
                set_setting("admin_password", password_hash)
                version = admin_credentials()[1]
            elif not check_password_hash(password_hash, password):
                flash("Incorrect password.", "error")
                return render_template("login.html", first_run=False), 401
            session.clear()
            session["admin"], session["auth_version"] = True, version
            session.permanent = True
            return redirect(safe_redirect_target(request.args.get("next"), url_for("dashboard")))
        return render_template("login.html", first_run=first_run, public_token=portal_token())

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        args = {key: request.args.get(key, "") for key in ("from", "to")}
        trades = query_trades(args)
        total, paid = summarize(trades), payments_by_client()
        clients = get_db().execute("SELECT * FROM clients ORDER BY lower(name),id").fetchall()
        all_trades = query_trades({}) if any(args.values()) else trades
        all_earned = {}
        for trade in all_trades:
            all_earned[trade["client_id"]] = all_earned.get(trade["client_id"], ZERO) + trade["commission"]
        per_client = []
        for client in clients:
            summary = summarize([trade for trade in trades if trade["client_id"] == client["id"]])
            summary.update(
                id=client["id"], name=client["name"], paid=paid.get(client["id"], ZERO),
                outstanding=all_earned.get(client["id"], ZERO) - paid.get(client["id"], ZERO),
                rule=rule_label(client["commission_type"], client["commission_rate"]),
            )
            per_client.append(summary)
        per_client.sort(key=lambda value: value["commission"], reverse=True)
        total["paid"] = sum(paid.values(), ZERO)
        total["outstanding"] = sum((client["outstanding"] for client in per_client), ZERO)
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
        return render_template(
            "dashboard.html", total=total, per_client=per_client, chart=chart,
            top_stocks=sorted(stocks.values(), key=lambda stock: stock["commission"], reverse=True)[:10],
            recent=trades[:8], args=args,
        )

    @app.route("/trades")
    @login_required
    def trades_list():
        args = {key: request.args.get(key, "") for key in ("client_id", "stock", "from", "to", "status")}
        trades = query_trades(args)
        clients = get_db().execute("SELECT id,name FROM clients ORDER BY lower(name)").fetchall()
        return render_template("trades.html", trades=trades, total=summarize(trades), clients=clients, args=args)

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
                    buy_date=?,sell_date=?,commission_override=?,notes=? WHERE id=?""",
                    (cid, values["stock"], values["quantity"], values["buy_price"], values["sell_price"],
                     values["buy_date"], values["sell_date"], values["commission_override"], values["notes"], tid),
                )
                flash("Trade updated.", "success")
            else:
                insert_trade(cid, values)
                flash("Trade added.", "success")
            if request.form.get("add_another"):
                return redirect(url_for("trade_form", client=name))
            return redirect(safe_redirect_target(request.form.get("back"), url_for("trades_list")))
        clients = get_db().execute("SELECT name FROM clients ORDER BY lower(name)").fetchall()
        return render_template(
            "trade_form.html", trade=trade, clients=clients,
            preset_client=request.args.get("client", ""),
            back=safe_redirect_target(request.referrer, url_for("trades_list")),
        )

    @app.route("/trades/<int:tid>/delete", methods=["POST"])
    @login_required
    def trade_delete(tid):
        if get_db().execute("DELETE FROM trades WHERE id=?", (tid,)).rowcount != 1:
            abort(404)
        flash("Trade deleted.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("trades_list")))

    @app.route("/trades/<int:tid>/close", methods=["POST"])
    @login_required
    def trade_close(tid):
        trade = get_db().execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        if not trade:
            abort(404)
        if trade["buy_price"] is None:
            raise ValidationError("Add the opening buy price and date before closing this position.")
        if trade["sell_price"] is not None:
            abort(409, description="This position is already closed. Use Edit to correct it.")
        trade.update(
            sell_price=number(request.form.get("sell_price"), "Sell price", required=True),
            sell_date=valid_date(request.form.get("sell_date"), "Sell date", required=True),
        )
        values = trade_values(trade)
        get_db().execute(
            "UPDATE trades SET sell_price=?,sell_date=? WHERE id=?",
            (values["sell_price"], values["sell_date"], tid),
        )
        flash("Position closed.", "success")
        return redirect(safe_redirect_target(request.referrer, url_for("trades_list")))

    @app.route("/trades/bulk-delete", methods=["POST"])
    @login_required
    def trades_bulk_delete():
        ids = list(dict.fromkeys(identifier(value, "Trade") for value in request.form.getlist("ids")))
        if not ids:
            raise ValidationError("Choose at least one trade to delete.")
        if len(ids) > MAX_ROWS:
            raise ValidationError(f"Delete at most {MAX_ROWS} trades at once.")
        count = get_db().execute(
            f"DELETE FROM trades WHERE id IN ({','.join('?' for _ in ids)})", ids
        ).rowcount
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
            insert_trades(get_or_create_client(name), values, source="import")
        flash(f"Imported {len(validated)} trade(s).", "success")
        return redirect(url_for("trades_list"))

    @app.route("/clients", methods=["GET", "POST"])
    @login_required
    def clients_list():
        if request.method == "POST":
            name = client_name(request.form.get("name"))
            values = client_values(request.form, name)
            if get_db().execute("SELECT id FROM clients WHERE lower(name)=lower(?)", (name,)).fetchone():
                raise ValidationError("A user with that name already exists.")
            get_db().execute(
                "INSERT INTO clients(name,phone,email,commission_type,commission_rate,notes,portfolio_amount) VALUES (?,?,?,?,?,?,?)",
                values,
            )
            flash(f"Added {name}.", "success")
            return redirect(url_for("clients_list"))
        trades, paid, rows = query_trades({}), payments_by_client(), []
        for client in get_db().execute("SELECT * FROM clients ORDER BY lower(name)").fetchall():
            summary = summarize([trade for trade in trades if trade["client_id"] == client["id"]])
            received = paid.get(client["id"], ZERO)
            rows.append({
                **client, **summary, "paid": received, "outstanding": summary["commission"] - received,
                "rule": rule_label(client["commission_type"], client["commission_rate"]),
            })
        return render_template("clients.html", clients=rows)

    @app.route("/clients/<int:cid>", methods=["GET", "POST"])
    @login_required
    def client_detail(cid):
        client = get_client(cid)
        if request.method == "POST":
            name = client_name(request.form.get("name"))
            values = client_values(request.form, name)
            if get_db().execute("SELECT id FROM clients WHERE lower(name)=lower(?) AND id<>?", (name, cid)).fetchone():
                raise ValidationError("Another user already has that name.")
            get_db().execute(
                "UPDATE clients SET name=?,phone=?,email=?,commission_type=?,commission_rate=?,notes=?,portfolio_amount=? WHERE id=?",
                (*values, cid),
            )
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
        
        cash_from_sales = sum(((trade["sell_price"] or ZERO) * trade["quantity"] for trade in trades), ZERO)
        cash_spent_on_buys = sum(((trade["buy_price"] or ZERO) * trade["quantity"] for trade in trades), ZERO)
        summary["money_in_bank"] = total_funds + cash_from_sales - cash_spent_on_buys

        return render_template(
            "client_detail.html", client=client, trades=trades, s=summary, payments=payments,
            funds=funds, total_funds=total_funds,
            uploads=upload_records(cid), rule=rule_label(client["commission_type"], client["commission_rate"]),
            merge_clients=get_db().execute("SELECT id,name FROM clients WHERE id<>? ORDER BY lower(name)", (cid,)).fetchall(),
            payment_token=payment_signer().dumps(cid),
        )

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
    @login_required
    def upload_file(name):
        if app.config["BACKEND_MODE"] == "supabase":
            asset = get_db().execute(
                "SELECT path FROM upload_assets WHERE path=? AND upload_id IS NOT NULL", (safe_path(name),)
            ).fetchone()
            if not asset:
                abort(404)
            return redirect(get_storage().sign_download(name), code=302)
        return send_from_directory(app.config["UPLOAD_DIR"], name, as_attachment=True)

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
        short_url = setting("upload_short_url") or go_url
        return render_template(
            "settings.html", s={key: setting(key, "") for key in keys}, upload_url=portal_url(),
            short_url=short_url, legacy_commission_count=legacy_count,
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
            og_title="Commission Book – Send Your Trades",
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
        
        if request.method == "POST":
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
            
            flash("Thank you, payment marked as done. The admin has been notified.", "success")
            return redirect(url_for("client_pay", token=token))
            
        return render_template("pay.html", client=client, outstanding=outstanding)

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
        phone = data.get("phone")
        if not phone:
            raise ValidationError("Phone number is required.")
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
        insert_trades(cid, validated, source="upload", upload_id=upload_id)
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
        except psycopg.Error as exc:
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
        except psycopg.Error as exc:
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

    return app
