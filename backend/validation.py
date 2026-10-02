import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID


COMMISSION_TYPES = {
    "profit_pct": "% of profit",
    "turnover_pct": "% of turnover",
    "flat": "Flat per trade",
}
MAX_NUMBER = Decimal("999999999999")
MAX_ROWS = 500
MAX_TEXT = 50000
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    pass


def text(value, label, *, limit=2000, required=False, multiline=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text.")
    if len(value) > limit:
        raise ValidationError(f"{label} must be at most {limit:,} characters.")
    value = value.strip()
    if CONTROL_CHARACTERS.search(value) or (not multiline and ("\n" in value or "\r" in value)):
        raise ValidationError(f"{label} contains unsupported control characters.")
    if required and not value:
        raise ValidationError(f"{label} is required.")
    return value


def client_name(value):
    return " ".join(text(value, "Name", limit=120, required=True).split())


def number(value, label, *, required=False, positive=False, places=6):
    if value is None or value == "":
        if required:
            raise ValidationError(f"{label} is required.")
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValidationError(f"{label} must be a number.")
    cleaned = str(value).strip()
    if cleaned.startswith("\u20b9"):
        cleaned = cleaned[1:].strip()
    if not cleaned:
        if required:
            raise ValidationError(f"{label} is required.")
        return None
    if len(cleaned) > 80:
        raise ValidationError(f"{label} is out of range.")
    if "," in cleaned:
        grouped = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?"
        if not re.fullmatch(grouped, cleaned):
            raise ValidationError(f"{label} has invalid digit grouping.")
        cleaned = cleaned.replace(",", "")
    try:
        result = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValidationError(f"{label} must be a valid number.") from exc
    if not result.is_finite():
        raise ValidationError(f"{label} must be finite.")
    if result < 0 or (positive and result == 0):
        qualifier = "greater than zero" if positive else "zero or greater"
        raise ValidationError(f"{label} must be {qualifier}.")
    if result > MAX_NUMBER:
        raise ValidationError(f"{label} is out of range.")
    quantum = Decimal(1).scaleb(-places)
    if result != result.quantize(quantum):
        raise ValidationError(f"{label} must have at most {places} decimal places.")
    return result


def valid_date(value, label, *, required=False):
    if value is None or value == "":
        if required:
            raise ValidationError(f"{label} is required.")
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a valid date.")
    value = value.strip()
    for fmt in (
        "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y",
        "%d %b %Y", "%d-%b-%Y", "%b %d, %Y",
    ):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValidationError(f"{label} must be a valid date (YYYY-MM-DD).")


def identifier(value, label="User"):
    if isinstance(value, bool):
        raise ValidationError(f"{label} must be a valid ID.")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        result = int(value)
    else:
        raise ValidationError(f"{label} must be a valid ID.")
    if result <= 0 or result > 9223372036854775807:
        raise ValidationError(f"{label} must be a valid ID.")
    return result


def submission_uuid(value):
    if not isinstance(value, str):
        raise ValidationError("submission_id must be a UUID.")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValidationError("submission_id must be a UUID.") from exc
    if parsed.version != 4:
        raise ValidationError("submission_id must be a random UUID (version 4).")
    return str(parsed)


def trade_values(values, *, public=False):
    if not hasattr(values, "get") or (public and not isinstance(values, dict)):
        raise ValidationError("Each trade row must be an object.")
    if public and any(
        key in values for key in
        ("commission", "commission_override", "commission_type", "commission_rate")
    ):
        raise ValidationError("Commission fields cannot be submitted through the upload portal.")
    result = {
        "stock": text(values.get("stock"), "Stock", limit=120, required=True).upper(),
        "quantity": number(values.get("quantity"), "Quantity", required=True, positive=True),
        "buy_price": number(values.get("buy_price"), "Buy price"),
        "sell_price": number(values.get("sell_price"), "Sell price"),
        "buy_date": valid_date(values.get("buy_date"), "Buy date"),
        "sell_date": valid_date(values.get("sell_date"), "Sell date"),
        "commission_override": None if public else number(
            values.get("commission_override"), "Commission", places=2
        ),
        "notes": text(values.get("notes"), "Notes", multiline=True) or None,
    }
    if result["buy_price"] is None and result["sell_price"] is None:
        raise ValidationError("Enter at least one buy or sell price.")
    if result["buy_date"] and result["sell_date"] and result["sell_date"] < result["buy_date"]:
        raise ValidationError("Sell date cannot be before buy date.")
    return result


def rule_values(ctype, rate, *, allow_default=False):
    if not ctype and allow_default:
        if rate not in (None, ""):
            raise ValidationError("Choose a commission rule before entering a rate.")
        return None, None
    if ctype not in COMMISSION_TYPES:
        raise ValidationError("Choose a valid commission rule.")
    return ctype, number(rate, "Commission rate", required=True)
