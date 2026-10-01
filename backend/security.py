import hashlib
import hmac
import ipaddress
import os
import secrets
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit

from flask import abort, current_app, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash


def configure_app(app, overrides=None):
    root = Path(app.root_path)
    production = os.environ.get("VERCEL") == "1" or os.environ.get("COMMISSION_ENV") == "production"
    database_url = os.environ.get("DATABASE_URL", "").strip()
    cloud = bool(database_url) or production
    secret = os.environ.get("SECRET_KEY", "")
    admin_password = os.environ.get("ADMIN_PASSWORD", "")
    admin_hash = os.environ.get("ADMIN_PASSWORD_HASH", "")
    config = {
        "PRODUCTION": production or cloud,
        "DEPLOYED": os.environ.get("VERCEL") == "1" and os.environ.get("VERCEL_ENV") != "development",
        "BACKEND_MODE": "supabase" if cloud else "local",
        "DATABASE": os.environ.get("COMMISSION_DB", str(root / "commission.db")),
        "DATABASE_URL": database_url,
        "UPLOAD_DIR": os.environ.get("COMMISSION_UPLOAD_DIR", str(root / "uploads")),
        "SECRET_KEY": secret,
        "ADMIN_PASSWORD": admin_password,
        "ADMIN_PASSWORD_HASH": admin_hash,
        "SUPABASE_URL": os.environ.get("SUPABASE_URL", "").rstrip("/"),
        "SUPABASE_SERVICE_ROLE_KEY": os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        "SUPABASE_STORAGE_BUCKET": os.environ.get("SUPABASE_STORAGE_BUCKET", "commission-evidence"),
        "PUBLIC_BASE_URL": os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
        "MAX_CONTENT_LENGTH": 4 * 1024 * 1024,
        "MAX_FORM_MEMORY_SIZE": 256 * 1024,
        "MAX_FORM_PARTS": 1000,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": production or cloud,
        "PERMANENT_SESSION_LIFETIME": timedelta(hours=12),
        "SESSION_REFRESH_EACH_REQUEST": False,
    }
    if overrides:
        config.update(overrides)
    app.config.update(config)
    if app.config["PRODUCTION"]:
        required = (
            "SECRET_KEY", "DATABASE_URL", "SUPABASE_URL",
            "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_STORAGE_BUCKET",
        )
        missing = [key for key in required if not app.config.get(key)]
        if not app.config["ADMIN_PASSWORD"] and not app.config["ADMIN_PASSWORD_HASH"]:
            missing.append("ADMIN_PASSWORD or ADMIN_PASSWORD_HASH")
        if missing:
            raise RuntimeError("Production configuration is incomplete: " + ", ".join(missing))
        if len(app.config["SECRET_KEY"]) < 32:
            raise RuntimeError("Production SECRET_KEY must contain at least 32 characters.")
    if app.config["ADMIN_PASSWORD"] and app.config["ADMIN_PASSWORD_HASH"]:
        raise RuntimeError("Set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH, not both.")
    auth_material = None
    if app.config["ADMIN_PASSWORD"]:
        if not 10 <= len(app.config["ADMIN_PASSWORD"]) <= 1024:
            raise RuntimeError("ADMIN_PASSWORD must contain between 10 and 1024 characters.")
        auth_material = "password:" + app.config["ADMIN_PASSWORD"]
        app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash(app.config["ADMIN_PASSWORD"])
        app.config["ADMIN_PASSWORD"] = ""
    elif app.config["ADMIN_PASSWORD_HASH"]:
        password_hash = app.config["ADMIN_PASSWORD_HASH"]
        if password_hash.count("$") != 2 or not password_hash.startswith(("scrypt:", "pbkdf2:sha256:")):
            raise RuntimeError("ADMIN_PASSWORD_HASH must be a Werkzeug scrypt or PBKDF2-SHA256 hash.")
        try:
            check_password_hash(password_hash, "configuration-validation")
        except (ValueError, TypeError) as exc:
            raise RuntimeError("ADMIN_PASSWORD_HASH is invalid.") from exc
        auth_material = "hash:" + password_hash
    if not app.secret_key:
        path = root / ".secret_key"
        try:
            app.secret_key = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            candidate = secrets.token_hex(32)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                app.secret_key = path.read_text(encoding="utf-8").strip()
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(candidate)
                app.secret_key = candidate
        if not app.secret_key:
            raise RuntimeError("The local .secret_key file is empty; set SECRET_KEY explicitly.")
    if auth_material is not None:
        app.config["ADMIN_AUTH_VERSION"] = credential_version(app.secret_key, auth_material)
    if app.config["BACKEND_MODE"] == "supabase":
        parsed = urlsplit(app.config["SUPABASE_URL"])
        if parsed.scheme != "https" or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise RuntimeError("SUPABASE_URL must be an HTTPS project origin without a path.")
        if parsed.username or parsed.password:
            raise RuntimeError("SUPABASE_URL must not contain credentials.")
        if not app.config["DATABASE_URL"].startswith(("postgres://", "postgresql://")):
            raise RuntimeError("DATABASE_URL must be a PostgreSQL connection URL.")
        bucket = app.config["SUPABASE_STORAGE_BUCKET"]
        if not bucket or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for char in bucket):
            raise RuntimeError("SUPABASE_STORAGE_BUCKET must use lowercase letters, numbers, '-' or '_'.")
    base_url = app.config["PUBLIC_BASE_URL"]
    if base_url:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise RuntimeError("PUBLIC_BASE_URL must be an origin such as https://books.example.com.")
        if app.config["PRODUCTION"] and parsed.scheme != "https":
            raise RuntimeError("Production PUBLIC_BASE_URL must use HTTPS.")
    if os.environ.get("VERCEL") == "1":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
    trusted = [value.strip() for value in os.environ.get("TRUSTED_HOSTS", "").split(",") if value.strip()]
    if base_url:
        trusted.append(urlsplit(base_url).hostname)
    for key in ("VERCEL_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
        if os.environ.get(key):
            trusted.append(os.environ[key].split(":")[0])
    if trusted and app.config["PRODUCTION"]:
        app.config["TRUSTED_HOSTS"] = list(dict.fromkeys(trusted))


def credential_version(secret, credential):
    key = secret.encode() if isinstance(secret, str) else secret
    return hmac.new(key, credential.encode(), hashlib.sha256).hexdigest()


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def require_csrf():
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    provided = request.headers.get("X-CSRF-Token")
    if not provided and request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        provided = request.form.get("csrf_token")
    expected = session.get("_csrf")
    if not expected or not provided or not hmac.compare_digest(expected.encode(), provided.encode()):
        abort(400, description="This form expired or is missing its security token. Reload the page and try again.")
    origin = request.headers.get("Origin")
    if origin:
        allowed = {request.host_url.rstrip("/"), current_app.config["PUBLIC_BASE_URL"]}
        null_origin_from_local_browser = (
            origin == "null"
            and not current_app.config["PRODUCTION"]
            and current_app.config["BACKEND_MODE"] == "local"
            and request.headers.get("Sec-Fetch-Site") == "same-origin"
            and not any(request.headers.get(header) for header in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host"))
            and _request_is_loopback()
        )
        if origin not in allowed and not null_origin_from_local_browser:
            abort(403, description="Cross-origin form submissions are not allowed.")
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        abort(403, description="Cross-site form submissions are not allowed.")


def _request_is_loopback():
    try:
        address = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return False
    return address.is_loopback and urlsplit(request.host_url).hostname in ("localhost", "127.0.0.1", "::1")


def local_setup_allowed():
    if current_app.config["PRODUCTION"] or current_app.config["BACKEND_MODE"] != "local":
        return False
    if any(request.headers.get(header) for header in ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host")):
        return False
    return _request_is_loopback()


def safe_redirect_target(value, default):
    if not isinstance(value, str) or not value:
        return default
    if any(ord(char) < 32 for char in value) or "\\" in unquote(value):
        return default
    try:
        parsed = urlsplit(value)
    except ValueError:
        return default
    if parsed.netloc:
        if parsed.netloc != request.host or parsed.scheme not in ("http", "https"):
            return default
    elif parsed.scheme:
        return default
    path = parsed.path
    if not path.startswith("/") or unquote(path).startswith("//"):
        return default
    return path + ("?" + parsed.query if parsed.query else "")


def secure_response(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store"
    if current_app.config["PRODUCTION"]:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response
