"""Heuristic parser that turns OCR text from broker screenshots into trade rows.

No AI APIs are used: the browser runs Tesseract.js OCR, and this module applies
rule-based extraction tuned for Groww, Zerodha (Kite/Console) and generic layouts.
Only a limited set of text layouts is supported, not arbitrary screenshots.
Missing fields stay unset; parse_text() returns review warnings alongside the
editable rows. A supplied default_year is the only missing-year fallback.
"""
import math
import re
from collections import deque
from datetime import date

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

SIDE_WORD_RE = re.compile(r"\b(buy|bought|sell|sold)\b", re.I)
PNL_SIDE_RE = re.compile(r"\b(buy|sell)\s+(?:avg\.?|average|price|rate)(?![a-z])", re.I)
EXECUTED_RE = re.compile(r"\b(?:complete(?:d)?|executed|filled|traded)\b", re.I)
BAD_STATUS_RE = re.compile(
    r"\b(?:cancelled|canceled|rejected|pending|unexecuted|unfilled|failed|expired|"
    r"open|queued|awaiting|not\s+(?:executed|filled|complete(?:d)?|traded))\b", re.I)
PLACED_STATUS_RE = re.compile(r"\b(?:placed|submitted|requested|received|amo)\b", re.I)

NUM = r"(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})+,\d{3}|\d+)(?:\.\d+)?"
NUMBER_END = r"(?![\d,.])"
CURRENCY = r"(?:\u20b9|\brs\.?|\binr)(?![a-z])"
MONTH = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
         r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?!\w)")
DATE_START = r"(?<![\w/.\-])(?<!\d:)"
DATE_END = r"(?!\w|:\d|[./\-]\d)"
OPTIONAL_YEAR = (r"(?:(?:[ \t]*,[ \t]*|[ \t]+)['\u2019]?(\d{4}|\d{2})" + DATE_END
                 + r"(?![ \t]*(?:shares?|nos?)\b))?")
DATE_PATTERNS = [
    (re.compile(DATE_START + r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})" + DATE_END), "ymd"),
    (re.compile(DATE_START + r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})" + DATE_END), "dmy"),
    (re.compile(DATE_START + r"(\d{1,2})(?:st|nd|rd|th)?[ \t]+" + MONTH
                + r"\.?" + OPTIONAL_YEAR, re.I), "dMy"),
    (re.compile(DATE_START + MONTH + r"\.?[ \t]+(\d{1,2})(?:st|nd|rd|th)?(?![\d:])"
                + OPTIONAL_YEAR, re.I), "Mdy"),
]
SHORT_DATE_RE = re.compile(
    r"(?:\b(?:date|dated|executed|traded)\s*[:=\-]?\s*|"
    r"^[ \t]*(?=\d{1,2}[/-]\d{1,2}[ \t]*$))"
    r"(\d{1,2})[/-](\d{1,2})(?![\d/-])(?=[ \t]*(?:$|[A-Za-z]))", re.I | re.M)
TIME_RE = re.compile(
    r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?"
    r"[ \t]*(am|pm)?(?![\w:])", re.I)
TIME_CANDIDATE_RE = re.compile(
    r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?:[ \t]*(?:am|pm))?(?![\w:])", re.I)
NON_EXECUTION_DATE_RE = re.compile(
    r"\b(?:placed|placement|created|updated|settlement|order\s+(?:date|time))\b", re.I)

STOPWORDS = {
    "BUY", "SELL", "SOLD", "BOUGHT", "QTY", "AVG", "PRICE", "LTP", "NSE", "BSE", "EQ", "CNC", "MIS",
    "NRML", "COMPLETE", "COMPLETED", "EXECUTED", "OPEN", "CANCELLED", "REJECTED", "PENDING", "ORDER",
    "ORDERS", "DETAILS", "MARKET", "LIMIT", "SL", "AMO", "DELIVERY", "INTRADAY", "TOTAL", "INR", "RS",
    "AM", "PM", "IST", "DATE", "TIME", "STATUS", "TYPE", "EXCHANGE", "SEGMENT", "SYMBOL", "TRADE",
    "TRADES", "HOLDINGS", "POSITIONS", "P&L", "PNL", "DAY", "RETURNS", "INVESTED", "CURRENT", "VALUE",
    "STOCKS", "STOCK", "GROWW", "KITE", "ZERODHA", "CONSOLE", "QUANTITY", "AVERAGE", "REALISED",
    "UNREALISED", "CHARGES", "BROKERAGE", "NET", "ID", "NO", "VIEW", "MORE", "HELP", "REPEAT", "SHARES",
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC", "LTD", "LIMITED",
    "FILLED", "PLACED", "REGULAR", "CO", "BO", "TRIGGER", "VALIDITY", "IOC", "MTF", "SIP",
    "PORTFOLIO", "FUNDS", "BALANCE", "AVAILABLE", "MARGIN", "ACCOUNT", "OVERVIEW", "SUMMARY",
    "DASHBOARD", "SEARCH", "BACK", "CLOSE", "CANCEL", "EDIT", "EXIT", "ALL", "FILTER", "FILTERS",
    "DOWNLOAD", "EXPORT", "HISTORY", "TRANSACTIONS", "NOTIFICATIONS", "SETTINGS", "PROFILE",
    "PROFIT", "LOSS", "REALIZED", "UNREALIZED", "EQUITY", "CASH", "TODAY", "YESTERDAY", "CMP",
    "TRADING", "SCRIP", "BOOK", "EXECUTION", "CANCELED", "UNEXECUTED", "UNFILLED", "FAILED",
    "EXPIRED", "QUEUED", "AWAITING", "PARTIALLY", "PARTIAL", "SUCCESS", "SUCCESSFUL",
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "SEPT",
    "OCTOBER", "NOVEMBER", "DECEMBER", "AMOUNT", "INVESTMENT", "HOLDING", "LAST", "PLEASE",
    "REFRESH", "ADD", "WATCHLIST", "REMAINING", "PERCENT", "PERCENTAGE", "SETTLEMENT",
    "SUBMITTED", "REQUESTED", "RECEIVED", "NOT", "WAITING", "CONFIRMATION", "AUTHORISATION",
    "AUTHORIZATION", "CREATED", "UPDATED", "CLOSED", "TRANSACTION", "TRANSACTIONAL",
    "TRADEBOOK", "ORDERBOOK", "INVESTMENTS", "HOME", "LOGIN", "LOGOUT", "ISIN", "TAX",
    "APP", "OK", "DONE", "AND", "THE", "OF", "FROM", "WITH", "ON", "AT", "FOR",
}
TICKER = r"[A-Z][A-Z0-9&\-]{1,24}"
ORDER_KEYS = ("symbol", "side", "qty", "price", "date")


def to_number(s):
    try:
        value = float(s.replace(",", ""))
    except (ValueError, AttributeError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _year(y, default):
    if y is None and not re.fullmatch(r"20\d{2}|2100", str(default)):
        raise ValueError("default_year must be an explicit four-digit year")
    value = int(y if y is not None else default)
    return value + 2000 if y is not None and len(str(y)) == 2 else value


def _date_info(text, default_year=None):
    dates, issues, spans = set(), set(), []
    for rx, kind in DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(m.start() < end and m.end() > start for start, end in spans):
                continue
            spans.append(m.span())
            if kind == "ymd":
                year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
            elif kind == "dmy":
                day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            elif kind == "dMy":
                day, month, year = int(m.group(1)), MONTHS[m.group(2)[:3].lower()], m.group(3)
            else:
                month, day, year = MONTHS[m.group(1)[:3].lower()], int(m.group(2)), m.group(3)
            if year is None and re.match(
                    r"[ \t]*,?[ \t]*['\u2019]?\d{3,}(?![\d.:])", text[m.end():]):
                issues.add("invalid")
                continue
            if year is None and default_year is None:
                issues.add("missing-year")
                continue
            try:
                value = date(_year(year, default_year), month, day)
                if not 2000 <= value.year <= 2100:
                    raise ValueError("Unsupported year")
                dates.add(value.isoformat())
                if year is None:
                    issues.add("default-year")
                elif len(year) == 2:
                    issues.add("two-digit-year")
            except (TypeError, ValueError, OverflowError):
                issues.add("invalid")
    for m in SHORT_DATE_RE.finditer(text):
        if any(m.start() < end and m.end() > start for start, end in spans):
            continue
        if default_year is None:
            issues.add("missing-year")
        else:
            try:
                value = date(_year(None, default_year), int(m.group(2)), int(m.group(1)))
                if not 2000 <= value.year <= 2100:
                    raise ValueError("Unsupported year")
                dates.add(value.isoformat())
                issues.add("default-year")
            except (TypeError, ValueError, OverflowError):
                issues.add("invalid")
    if len(dates) > 1 or (dates and issues & {"missing-year", "invalid"}):
        return None, "ambiguous"
    if dates:
        return dates.pop(), next((i for i in ("default-year", "two-digit-year") if i in issues), None)
    if issues:
        return None, next(i for i in ("invalid", "missing-year") if i in issues)
    return None, "relative" if re.search(r"\b(?:today|yesterday)\b", text, re.I) else "missing"


def parse_date(text, default_year=None):
    """Parse one unambiguous date, never supplying the current year or date."""
    return _date_info(text, default_year)[0]


def _strip_dates(text):
    for rx, _ in DATE_PATTERNS:
        text = rx.sub(" ", text)
    text = SHORT_DATE_RE.sub(" ", text)
    return TIME_RE.sub(" ", text)


def _quantity_info(text):
    clean = _strip_dates(text)
    value = r"(" + NUM + r")" + NUMBER_END + r"(?:\s*/\s*(" + NUM + r")" + NUMBER_END + r")?"
    patterns = [
        (3, r"\b(?:filled(?:[ \t]+(?:qty|quantity))?|(?:executed|traded)[ \t]+(?:qty|quantity))"
            r"(?![a-z])\.?\s*[:=]?\s*" + value),
        (2, r"\b(?:buy|sell|bought|sold)\b\s*[:=\u00b7]?\s*(" + NUM
            + r")\s*/\s*(" + NUM + r")" + NUMBER_END),
        (2, r"(?<![\w/.\-])(" + NUM + r")\s*/\s*(" + NUM + r")" + NUMBER_END
            + r"\s+(?:complete(?:d)?|filled)\b"),
        (1, r"\b(?:qty|quantity|shares?)(?![a-z])\.?\s*[:=]?\s*" + value),
        (0, r"(?<![\w/.\-])(" + NUM + r")\s*(?:shares?|qty|nos?)(?![a-z0-9])"),
        (1, r"\b(?:buy|sell|bought|sold)\s+(" + NUM + r")\s*@"),
    ]
    candidates = []
    for priority, pattern in patterns:
        for m in re.finditer(pattern, clean, re.I):
            qty = to_number(m.group(1))
            total = to_number(m.group(2)) if m.lastindex == 2 and m.group(2) is not None else None
            is_ratio = m.lastindex == 2 and m.group(2) is not None
            valid = qty is not None and qty.is_integer() and 0 <= qty < 10_000_000
            if is_ratio:
                valid = valid and total is not None and total.is_integer() and 0 < total < 10_000_000 and qty <= total
            candidates.append((max(priority, 2) if is_ratio else priority, int(qty) if valid else None))
    if not candidates:
        return None, "missing"
    priority = max(p for p, _ in candidates)
    if priority < 2 and re.search(r"\bpartial(?:ly)?\s+(?:filled|executed)\b", clean, re.I):
        return None, "ambiguous"
    values = {v for p, v in candidates if p == priority}
    if None in values or len(values) != 1:
        return None, "ambiguous"
    qty = values.pop()
    return (None, "zero") if qty == 0 else (qty, None)


def find_qty(text):
    return _quantity_info(text)[0]


UNTRADED_PRICE_RE = re.compile(
    r"\b(?:ltp|cmp|current(?:\s+(?:holding|market))?|holdings?|(?:last|latest)(?:\s+traded)?|market|limit|trigger|"
    r"invested|total|order\s+value|amount|charges|brokerage|returns?|p&l|pnl)"
    r"\s*[:=]?\s*(?:(?:price|value|avg\.?(?:\s+price)?|average(?:\s+price)?|amount|rate)\s*)?"
    r"[:=@]?\s*(?:" + CURRENCY
    + r")?\s*" + NUM + NUMBER_END, re.I)


def _price_info(text, executed=False):
    # Mask the label AND its amount, including an amount on the next OCR line.
    clean = UNTRADED_PRICE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), _strip_dates(text))
    amount = r"\s*[:=@]?\s*(?:" + CURRENCY + r")?\s*(" + NUM + r")" + NUMBER_END
    patterns = [
        (2, r"\b(?:(?:executed|execution|traded|filled|buy|sell)\s+(?:price|rate)|"
            r"(?:avg\.?|average)(?:\s+(?:price|rate))?)(?![a-z])" + amount),
    ]
    if not re.search(r"\b(?:limit|trigger|sl(?:-m)?)\b", text, re.I):
        patterns.append((1, r"@" + amount))
        if executed:
            patterns.append((0, r"^[ \t]*" + CURRENCY + r"[ \t]*(" + NUM + r")" + NUMBER_END + r"[ \t]*$"))
    candidates = []
    for priority, pattern in patterns:
        for m in re.finditer(pattern, clean, re.I | re.M):
            if priority == 0:
                previous = clean[:m.start()].strip().splitlines()
                if previous and not (
                        _side(previous[-1]) or find_qty(previous[-1]) is not None
                        or re.fullmatch(CURRENCY + r"\s*" + NUM, previous[-1], re.I)):
                    continue
            number = to_number(m.group(1))
            candidates.append((priority, number if number is not None and number > 0 else None))
    if not candidates:
        return None
    priority = max(p for p, _ in candidates)
    values = {v for p, v in candidates if p == priority}
    return values.pop() if len(values) == 1 and None not in values else None


def find_price(text):
    """Find an explicitly labelled trade price or standalone currency amount."""
    return _price_info(text, executed=True)


def _ticker(token):
    if re.fullmatch(TICKER, token) and token not in STOPWORDS and not re.fullmatch(r"[A-Z]\d+", token):
        return token
    return None


def find_company_name(text):
    """Accept a whole name line, not a capitalized fragment of a UI label."""
    line = text.strip()
    if not re.fullmatch(r"[A-Za-z&.\- ]+", line):
        return None
    words = re.findall(r"[A-Za-z&.\-]+", line)
    if not 1 <= len(words) <= 6 or len(line) < 3:
        return None
    connectors = {"of", "and", "the", "&"}
    meaningful = [w for w in words if w.lower().rstrip(".") not in connectors | {"ltd", "limited"}]
    if not meaningful or any(w.upper().rstrip(".") in STOPWORDS for w in meaningful):
        return None
    if all(w[0].isupper() or w.lower() in connectors for w in words):
        return " ".join(words)
    return None


def _stock_candidate(line):
    line = line.strip()
    labelled = re.fullmatch(
        r"(?:trading\s+)?(?:symbol|scrip)(?:\s*[:=\-]\s*|\s+)("
        + TICKER + r")(?:\s+(?:NSE|BSE)(?:\s+EQ)?)?",
        line, re.I)
    exchange = re.fullmatch(
        r"(" + TICKER + r")(?:\s*[:|\u00b7\-]\s*|\s+)(?:NSE|BSE)(?:\s+EQ)?", line, re.I)
    reverse_exchange = re.fullmatch(
        r"(?:NSE|BSE)(?:\s*[:|\u00b7\-]\s*|\s+)(" + TICKER + r")(?:\s+EQ)?", line, re.I)
    for match in (labelled, exchange, reverse_exchange):
        if match and _ticker(match.group(1).upper()):
            return match.group(1).upper(), 3
    if _ticker(line):
        return line, 2
    name = find_company_name(line)
    return (name, 1) if name else None


def _inline_symbol(line):
    for pattern in (
        r"^(" + TICKER + r")\s+(?i:buy|sell|bought|sold)\b",
        r"^(?i:buy|sell|bought|sold)\s+(" + TICKER + r")\b",
    ):
        match = re.search(pattern, line)
        if match and _ticker(match.group(1)):
            return match.group(1)
    return None


def find_symbol(text):
    candidate = _stock_candidate(text)
    if candidate and candidate[1] >= 2:
        return candidate[0]
    return _inline_symbol(text)


def _stock_info(lines):
    candidates = []
    for line in lines:
        candidate = _stock_candidate(line)
        if candidate:
            candidates.append(candidate)
        inline = _inline_symbol(line)
        if inline:
            candidates.append((inline, 2))
    if not candidates:
        return ""
    priority = max(p for _, p in candidates)
    names = {name.upper(): name for name, p in candidates if p == priority}
    return next(iter(names.values())) if len(names) == 1 else ""


def _side(line):
    sides = {"buy" if m.group().lower() in ("buy", "bought") else "sell" for m in SIDE_WORD_RE.finditer(line)}
    if len(sides) != 1 or re.fullmatch(r"(?:buy|sell)\s+orders?", line, re.I):
        return None
    return sides.pop()


def _anchors(lines):
    anchors = []
    for i, line in enumerate(lines):
        side = _side(line)
        if side is None:
            continue
        if (anchors and PNL_SIDE_RE.search(line) and not PNL_SIDE_RE.search(lines[anchors[-1]])
                and side == _side(lines[anchors[-1]])):
            continue
        anchors.append(i)
    return anchors


def _bad_status(text):
    bad = BAD_STATUS_RE.search(text)
    return bad or (PLACED_STATUS_RE.search(text) if not EXECUTED_RE.search(text) else None)


def _execution_text(text):
    lines, preferred, ordinary = text.splitlines(), [], []
    skip_next = False
    for i, line in enumerate(lines):
        has_date = any(rx.search(line) for rx, _ in DATE_PATTERNS) or SHORT_DATE_RE.search(line)
        if not NON_EXECUTION_DATE_RE.search(line):
            if EXECUTED_RE.search(line) and has_date:
                preferred.append(line)
            elif i + 1 < len(lines) and re.fullmatch(
                    r"(?:execution|executed|trade|traded|filled|completed)"
                    r"(?:\s+(?:date|time|on|at))?\s*[:=\-]?", line, re.I):
                following = lines[i + 1]
                following_has_date = any(rx.search(following) for rx, _ in DATE_PATTERNS) or SHORT_DATE_RE.search(following)
                # Status/time-only labels must not hide dates elsewhere in this card.
                if following_has_date and not NON_EXECUTION_DATE_RE.search(following):
                    preferred.extend((line, following))
        if NON_EXECUTION_DATE_RE.search(line):
            skip_next = not bool(has_date or TIME_RE.search(line))
            continue
        if skip_next:
            skip_next = False
            if has_date or TIME_RE.search(line):
                continue
        ordinary.append(line)
    return "\n".join(preferred or ordinary)


def _time_info(text):
    times, invalid = set(), False
    for candidate in TIME_CANDIDATE_RE.finditer(text):
        m = TIME_RE.fullmatch(candidate.group())
        if not m:
            invalid = True
            continue
        hour = int(m.group(1))
        if m.group(4):
            if not 1 <= hour <= 12:
                invalid = True
                continue
            hour = hour % 12 + (12 if m.group(4).lower() == "pm" else 0)
        times.add(f"{hour:02d}:{m.group(2)}:{m.group(3) or '00'}")
    return (next(iter(times)) if len(times) == 1 and not invalid else None), invalid or len(times) > 1


def _parse_table_row(line, default_year):
    """Recognize the explicit symbol/date/side/quantity/price tradebook layout."""
    side = _side(line)
    d = parse_date(line, default_year)
    if not side or not d or _bad_status(line):
        return None
    if re.search(r"\b(?:ltp|cmp|current|limit|trigger)\b", line, re.I):
        return None
    side_match = SIDE_WORD_RE.search(line)
    prefix = _strip_dates(line[:side_match.start()])
    prefix = re.sub(r"\b(?:NSE|BSE|EQ)\b|[|\u00b7]", " ", prefix, flags=re.I).strip()
    sym = find_symbol(prefix)
    values = re.match(
        r"\s*[|]?\s*(" + NUM + r")" + NUMBER_END + r"(?:\s+|\s*\|\s*)"
        r"(?:" + CURRENCY + r")?\s*(" + NUM + r")" + NUMBER_END,
        line[side_match.end():], re.I)
    if not sym or not values:
        return None
    qty, price = to_number(values.group(1)), to_number(values.group(2))
    if qty is None or not qty.is_integer() or not 0 < qty < 10_000_000 or price is None or price <= 0:
        return None
    return {"symbol": sym, "side": side, "qty": int(qty), "price": price, "date": d}


def _header_followed_by_side(lines, index):
    for line in lines[index + 1:]:
        if not line or _stock_candidate(line) or line.upper() in {"NSE", "BSE", "EQ", "ORDER DETAILS", "DETAILS"}:
            continue
        return _side(line) is not None
    return False


def _card_blocks(text, default_year):
    lines = [line.strip() for line in text.replace("\f", "\n---\n").splitlines()]
    block = []
    for i, line in enumerate(lines):
        if not line:
            anchors = _anchors(block)
            if anchors and all(PNL_SIDE_RE.search(block[j]) for j in anchors):
                yield block
                block = []
            continue
        if not _anchors(block) and not _stock_info(block) and re.fullmatch(
                r"(?:(?:open|pending|executed|completed|cancelled|rejected)\s*"
                r"(?:\(\d+\)|\d+)\s*)+", line, re.I):
            continue
        if (re.fullmatch(r"[-=]{3,}", line) or re.match(r"(?i)^screenshot\s+\d+\b", line)
                or line.lower() in {"groww", "kite", "zerodha", "console", "orders", "tradebook", "p&l"}):
            if block:
                yield block
            block = []
            continue
        if _parse_table_row(line, default_year):
            if block:
                yield block
            yield [line]
            block = []
            continue
        anchors = _anchors(block)
        side = _side(line)
        candidate = _stock_candidate(line)
        new_card = False
        if anchors and side:
            own_price_field = (PNL_SIDE_RE.search(line) and not PNL_SIDE_RE.search(block[anchors[-1]])
                               and side == _side(block[anchors[-1]]))
            new_card = not own_price_field and not (PNL_SIDE_RE.search(line)
                            and all(PNL_SIDE_RE.search(block[j]) and _side(block[j]) != side for j in anchors))
        elif anchors and candidate:
            head_only = all(
                _stock_candidate(previous) or previous.upper() in {
                    "NSE", "BSE", "EQ", "ORDER DETAILS", "DETAILS", "SYMBOL", "TRADING SYMBOL",
                    "COMPLETE", "COMPLETED", "EXECUTED",
                } for previous in block[anchors[-1] + 1:])
            stocks = [c for previous in block if (c := _stock_candidate(previous))]
            existing = _stock_info(block)
            kite_header = bool(re.search(r"\b\d+\s*/\s*\d+\s+COMPLETE\b", block[anchors[-1]], re.I))
            alias = bool(stocks and (all(p == 1 for _, p in stocks) and candidate[1] == 3
                                    or existing.upper() == candidate[0].upper()))
            attach = head_only and (not existing or alias) and (
                kite_header or not _header_followed_by_side(lines, i))
            new_card = not attach
        if new_card:
            yield block
            block = []
        block.append(line)
    if block:
        yield block


def _warn(warnings, message):
    if warnings is not None and message not in warnings:
        warnings.append(message)


def _review_order(order, date_issue, warnings, executed):
    label = f"{order['side'].capitalize()} order for {order['symbol'] or 'an unidentified stock'}"
    if not order["symbol"]:
        _warn(warnings, f"{label}: stock is missing or ambiguous; left blank.")
    if order["qty"] is None:
        _warn(warnings, f"{label}: quantity is missing or ambiguous; left blank.")
    if order["price"] is None:
        _warn(warnings, f"{label}: executed price is missing or ambiguous; LTP/current/limit prices are not used.")
    if order["date"] is None:
        reason = {
            "missing-year": "date has no year",
            "relative": "relative date cannot establish a calendar date",
            "invalid": "date is invalid",
            "ambiguous": "execution date is ambiguous",
        }.get(date_issue, "complete execution date is missing")
        _warn(warnings, f"{label}: {reason}; left blank.")
    elif date_issue == "default-year":
        _warn(warnings, f"{label}: the date year came from default_year; confirm {order['date']}.")
    elif date_issue == "two-digit-year":
        _warn(warnings, f"{label}: a two-digit year was interpreted as {order['date'][:4]}; confirm it.")
    if not executed:
        _warn(warnings, f"{label}: execution status is not shown; confirm this is an executed order.")
    if order.get("_time_ambiguous"):
        _warn(warnings, f"{label}: execution time is ambiguous; same-day pairing will not be inferred.")


def _extract_records(text, default_year, warnings):
    orders = []
    for block in _card_blocks(text, default_year):
        anchors = _anchors(block)
        if not anchors:
            continue
        pnl = all(PNL_SIDE_RE.search(block[i]) for i in anchors)
        header = block[:anchors[0]]
        shared_qty, shared_issue = _quantity_info("\n".join(header)) if pnl else (None, "missing")
        for position, start in enumerate(anchors):
            end = anchors[position + 1] if position + 1 < len(anchors) else len(block)
            lines = block[start:end] if pnl else block
            content = "\n".join(lines)
            stock = _stock_info(header + lines) if pnl else _stock_info(lines)
            side = _side(block[start])
            bad_status = _bad_status("\n".join(header + lines) if pnl else content)
            if bad_status:
                _warn(warnings, f"Ignored {side} order for {stock or 'an unidentified stock'}: status is {bad_status.group().lower()}.")
                continue
            qty, qty_issue = _quantity_info(content)
            if pnl and qty_issue == "missing":
                qty, qty_issue = shared_qty, shared_issue
            if qty_issue == "zero":
                _warn(warnings, f"Ignored {side} order for {stock or 'an unidentified stock'}: explicit quantity is zero.")
                continue
            execution_text = _execution_text(content)
            execution_date, date_issue = _date_info(execution_text, default_year)
            table = _parse_table_row(content, default_year) if len(lines) == 1 else None
            executed = bool(table or pnl or EXECUTED_RE.search(content))
            order = table or {
                "symbol": stock, "side": side, "qty": qty,
                "price": _price_info(content, executed), "date": execution_date,
            }
            if not any(order[k] is not None and order[k] != "" for k in ("symbol", "qty", "price", "date")):
                _warn(warnings, f"A {side} label had no identifiable trade fields; no order was inferred.")
                continue
            order["_time"], order["_time_ambiguous"] = _time_info(execution_text)
            _review_order(order, date_issue, warnings, executed)
            orders.append(order)
    return orders


def _public_order(order):
    return {key: order[key] for key in ORDER_KEYS}


def extract_orders(text, default_year=None):
    return [_public_order(order) for order in _extract_records(text, default_year, None)]


def _known_date(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if parsed.isoformat() == value and 2000 <= parsed.year <= 2100 else None


def _pairable(order):
    qty = order["qty"]
    return (isinstance(order["symbol"], str) and bool(order["symbol"].strip()) and _known_date(order["date"]) is not None
            and isinstance(qty, (int, float)) and not isinstance(qty, bool)
            and math.isfinite(qty) and 0 < qty < 10_000_000 and int(qty) == qty)


def _same_day_compatible(buy, sell):
    if buy["date"] < sell["date"]:
        return True
    if buy["date"] > sell["date"] or buy.get("_time_ambiguous") or sell.get("_time_ambiguous"):
        return False
    bt, st = buy.get("_time"), sell.get("_time")
    return (bt is None and st is None) or (bt is not None and st is not None and bt <= st)


def pair_orders(orders, _warnings=None):
    """Chronological FIFO, using only identified, dated, explicitly sized orders."""
    trades, books = [], {}
    ordered = sorted(orders, key=lambda o: (
        _known_date(o["date"]) or "9999", o.get("_time") or "", 0 if o["side"] == "buy" else 1))
    for original in ordered:
        order = dict(original)
        is_buy = order["side"] == "buy"
        if not _pairable(order):
            trades.append(_trade(order["symbol"], order["qty"], order if is_buy else None, None if is_buy else order))
            _warn(_warnings, "Orders without a known stock, positive quantity and full date were kept as separate review rows.")
            continue
        key = " ".join(order["symbol"].upper().split())
        buys = books.setdefault(key, deque())
        if is_buy:
            if any(buy["date"] == order["date"] and (not buy.get("_time") or not order.get("_time")) for buy in buys):
                _warn(_warnings, "Same-date purchase ordering lacks comparable times; review FIFO allocations.")
            buys.append(order)
            continue
        while buys and order["qty"]:
            buy = buys[0]
            if not _same_day_compatible(buy, order):
                _warn(_warnings, "Same-day orders with incomplete or ambiguous times were not paired.")
                break
            qty = min(buy["qty"], order["qty"])
            if buy["date"] == order["date"] and buy.get("_time") is None:
                _warn(_warnings, "Same-day buy/sell ordering is not established by execution times; review the pairing.")
            trades.append(_trade(buy["symbol"], qty, buy, order))
            buy["qty"] -= qty
            order["qty"] -= qty
            if not buy["qty"]:
                buys.popleft()
        if order["qty"]:
            trades.append(_trade(order["symbol"], order["qty"], None, order))
    for buys in books.values():
        for buy in buys:
            trades.append(_trade(buy["symbol"], buy["qty"], buy, None))
    return trades


def _trade(sym, qty, b, s):
    return {
        "stock": sym or "",
        "quantity": qty or "",
        "buy_price": b["price"] if b and b["price"] else "",
        "sell_price": s["price"] if s and s["price"] else "",
        "buy_date": b["date"] if b and b["date"] else "",
        "sell_date": s["date"] if s and s["date"] else "",
    }


def detect_platform(text):
    if re.search(r"\bgroww\b", text, re.I):
        return "groww"
    if re.search(r"\b(?:kite|zerodha|console)\b", text, re.I):
        return "zerodha"
    return "generic"


def parse_text(text, default_year=None):
    """Return platform, orders, trades and deduplicated human-readable warnings.

    Unknown numeric/date order fields are None; unknown trade cells are "".
    Warnings describe omissions and assumptions, not confidence estimates.
    """
    warnings = []
    records = _extract_records(text, default_year, warnings)
    trades = pair_orders(records, warnings)
    if not records:
        _warn(warnings, "No supported executed trade rows were found; review or enter the details manually.")
    return {
        "platform": detect_platform(text), "orders": [_public_order(order) for order in records],
        "trades": trades, "warnings": warnings,
    }


if __name__ == "__main__":
    import json, sys
    print(json.dumps(parse_text(sys.stdin.read()), indent=2))
