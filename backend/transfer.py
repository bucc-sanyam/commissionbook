"""Explicit, preflighted migration from a read-only SQLite source to an empty cloud ledger."""

import hashlib
import io
import sqlite3
import uuid
from pathlib import Path

import psycopg
from PIL import Image, UnidentifiedImageError

from .database import DEFAULTS, get_db
from .storage import IMAGE_TYPES, MAX_CLOUD_IMAGE, StorageUnavailable, get_storage, validate_image
from .validation import ValidationError, client_name, identifier, number, rule_values, trade_values, valid_date


def inspect_local(database_path, upload_directory):
    database_path = Path(database_path).resolve(strict=True)
    upload_directory = Path(upload_directory).resolve()
    connection = sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        source = {
            table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("clients", "uploads", "trades", "payments")
        }
        keys = tuple(DEFAULTS)
        settings = {
            row["key"]: row["value"] for row in connection.execute(
                f"SELECT key,value FROM settings WHERE key IN ({','.join('?' for _ in keys)})", keys
            )
        }
    finally:
        connection.close()
    rule_values(
        settings.get("default_commission_type", DEFAULTS["default_commission_type"]),
        settings.get("default_commission_rate", DEFAULTS["default_commission_rate"]),
    )
    client_ids, names = set(), {}
    for client in source["clients"]:
        cid = identifier(client["id"])
        client_ids.add(cid)
        normalized = client_name(client["name"])
        if normalized.lower() in names:
            raise ValidationError("Client names conflict ignoring case. Merge them locally before migration.")
        names[normalized.lower()] = cid
        client["name"] = normalized
        client["commission_type"], client["commission_rate"] = rule_values(
            client["commission_type"], client["commission_rate"], allow_default=True
        )
    upload_ids = {upload["id"] for upload in source["uploads"]}
    for trade in source["trades"]:
        try:
            values = trade_values(trade)
            if trade["client_id"] not in client_ids:
                raise ValidationError("The referenced user is missing.")
            if trade["upload_id"] is not None and trade["upload_id"] not in upload_ids:
                raise ValidationError("The referenced submission is missing.")
            snapshot_type, snapshot_rate = rule_values(
                trade.get("commission_type_snapshot"), trade.get("commission_rate_snapshot"), allow_default=True
            )
        except ValidationError as exc:
            raise ValidationError(f"Trade {trade['id']}: {exc} Correct the source entry before migrating.") from exc
        trade.update(values, commission_type_snapshot=snapshot_type, commission_rate_snapshot=snapshot_rate)
    for payment in source["payments"]:
        try:
            if payment["client_id"] not in client_ids:
                raise ValidationError("The referenced user is missing.")
            payment["amount"] = number(payment["amount"], "Payment amount", required=True, positive=True, places=2)
            payment["paid_on"] = valid_date(payment["paid_on"], "Payment date", required=True)
        except ValidationError as exc:
            raise ValidationError(f"Payment {payment['id']}: {exc}") from exc
    assets = []
    seen_files = set()
    for upload in source["uploads"]:
        if "client_id" not in upload:
            upload["client_id"] = names.get(upload["client_name"].lower())
        if upload["client_id"] is not None and upload["client_id"] not in client_ids:
            raise ValidationError(f"Submission {upload['id']} references a missing user.")
        paths = []
        for filename in (upload["filename"] or "").split(","):
            if not filename:
                continue
            original = (upload_directory / filename).resolve()
            if not original.is_relative_to(upload_directory) or original in seen_files:
                raise ValidationError(f"Submission {upload['id']} contains an unsafe or duplicated evidence path.")
            seen_files.add(original)
            try:
                with original.open("rb") as handle:
                    content = handle.read(MAX_CLOUD_IMAGE + 1)
                if len(content) > MAX_CLOUD_IMAGE:
                    raise ValidationError("An evidence image exceeds the 10 MB cloud limit.")
                with Image.open(io.BytesIO(content)) as image:
                    mime = next((mime for mime, (format_name, _) in IMAGE_TYPES.items() if format_name == image.format), None)
                if mime is None:
                    raise ValidationError("Legacy evidence must be PNG, JPEG, or WebP before migration.")
                validate_image(content, mime)
            except (OSError, UnidentifiedImageError) as exc:
                raise ValidationError(f"Submission {upload['id']} has missing or unreadable evidence.") from exc
            path = "evidence/" + uuid.uuid4().hex + IMAGE_TYPES[mime][1]
            paths.append(path)
            assets.append({
                "path": path, "original": original, "content_type": mime, "byte_size": len(content),
                "digest": hashlib.sha256(content).digest(), "upload_id": upload["id"],
            })
        upload["filename"] = ",".join(paths)
        for key in ("submission_id", "submission_hash", "submission_owner"):
            upload.setdefault(key, None)
    return {**source, "settings": settings, "assets": assets}


COLUMNS = {
    "clients": ("id", "name", "phone", "email", "commission_type", "commission_rate", "notes", "active", "created_at"),
    "uploads": ("id", "client_name", "client_id", "filename", "platform", "ocr_text", "trade_count",
                "submission_id", "submission_hash", "submission_owner", "created_at"),
    "trades": ("id", "client_id", "stock", "quantity", "buy_price", "sell_price", "buy_date", "sell_date",
               "commission_override", "commission_type_snapshot", "commission_rate_snapshot", "source",
               "upload_id", "notes", "created_at"),
    "payments": ("id", "client_id", "amount", "paid_on", "mode", "notes", "created_at"),
}


def migrate_local(snapshot):
    database, storage = get_db(), get_storage()
    if not database.postgres:
        raise ValidationError("Configure Supabase before migration. Local preflight works with --dry-run.")
    storage.check_bucket()
    attempted_paths = []
    try:
        database.execute("LOCK TABLE clients,uploads,trades,payments,upload_assets IN ACCESS EXCLUSIVE MODE")
        for table in (*COLUMNS, "upload_assets"):
            if database.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                raise ValidationError("The cloud ledger must be empty. Migration never overwrites an existing ledger.")
        for table, columns in COLUMNS.items():
            sql = f"INSERT INTO {table}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})"
            for row in snapshot[table]:
                database.execute(sql, [row[key] for key in columns])
        for key, value in snapshot["settings"].items():
            database.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        for asset in snapshot["assets"]:
            with asset["original"].open("rb") as handle:
                content = handle.read(MAX_CLOUD_IMAGE + 1)
            if hashlib.sha256(content).digest() != asset["digest"]:
                raise ValidationError("Source evidence changed during migration. Stop local edits and retry.")
            attempted_paths.append(asset["path"])
            storage.upload_bytes(asset["path"], content, asset["content_type"])
            database.execute(
                """INSERT INTO upload_assets(path,owner_hash,scope_hash,content_type,byte_size,expires_at,upload_id)
                VALUES (?, 'migration', 'migration', ?, ?, 0, ?)""",
                (asset["path"], asset["content_type"], asset["byte_size"], asset["upload_id"]),
            )
        for table in COLUMNS:
            database.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}','id'),COALESCE(MAX(id),1),MAX(id) IS NOT NULL) FROM {table}"
            )
    except (psycopg.Error, StorageUnavailable, ValidationError, OSError):
        database.rollback()
        storage.remove(attempted_paths)
        raise
    # Never remove evidence on an ambiguous commit failure: PostgreSQL may have committed successfully.
    database.commit()
