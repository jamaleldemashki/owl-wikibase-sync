"""
Centralized OWL-value -> Wikibase-datavalue conversion layer.

This is pure transformation logic: nothing in this module makes a network
call. The Wikibase datatype for a given OWL predicate always comes from the
``PROPERTY_MAP`` configuration (see ``src/config.py``) rather than being
guessed from the shape of the value, so conversion behavior is stable across
runs. Incompatible values are reported as failed :class:`ConversionResult`
objects rather than silently coerced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

SUPPORTED_WIKIBASE_DATATYPES = frozenset(
    {
        "wikibase-item",
        "string",
        "external-id",
        "url",
        "monolingualtext",
        "time",
        "quantity",
        "commonsMedia",
    }
)

DEFAULT_CALENDAR_MODEL = "http://www.wikidata.org/entity/Q1985727"  # proleptic Gregorian
"""Default calendar model IRI for `time` values. Wikibase Cloud/private
instances usually mirror this Wikidata entity, but verify it exists on the
target instance before relying on `time` statements in production."""

_DATE_YEAR_RE = re.compile(r"^[+-]?\d{1,9}$")
_DATE_YEAR_MONTH_RE = re.compile(r"^([+-]?\d{1,9})-(\d{2})$")
_DATE_FULL_RE = re.compile(r"^([+-]?\d{1,9})-(\d{2})-(\d{2})(?:T.*)?$")

_QUANTITY_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*(.*)$")


@dataclass
class ConversionResult:
    """Outcome of converting one OWL value into a Wikibase datavalue."""

    success: bool
    datavalue: Optional[dict] = None
    comparable_value: Optional[str] = None
    error: Optional[str] = None


def normalize_whitespace(text: str) -> str:
    return " ".join(str(text).split())


def normalize_uri_for_comparison(uri: str) -> str:
    """Normalize a URI for equality comparison (not for display).

    Lower-cases scheme and host, strips a single trailing slash (except for
    the root path), and collapses surrounding whitespace. Path/query/fragment
    case is preserved since many identifier schemes are case-sensitive there.
    """
    text = normalize_whitespace(uri)
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme:
        return text
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, parts.fragment))
    return normalized


def build_wikibase_datavalue(
    wikibase_datatype: str,
    value: str,
    language: Optional[str] = None,
    default_language: str = "en",
    calendar_model: str = DEFAULT_CALENDAR_MODEL,
) -> ConversionResult:
    """Build a Wikibase-API-shaped datavalue for an already-resolved value.

    ``value`` must already be the *final* value to write: a QID for
    ``wikibase-item``, a literal string otherwise. Resource resolution (OWL
    object -> Wikibase QID) happens before this function is called -- see
    ``src/sync_planner.py``.
    """
    if wikibase_datatype not in SUPPORTED_WIKIBASE_DATATYPES:
        return ConversionResult(success=False, error=f"unsupported_wikibase_datatype:{wikibase_datatype}")

    if wikibase_datatype == "wikibase-item":
        qid = normalize_whitespace(value)
        if not re.fullmatch(r"Q\d+", qid):
            return ConversionResult(success=False, error=f"invalid_qid:{qid!r}")
        return ConversionResult(
            success=True,
            datavalue={"value": {"entity-type": "item", "id": qid}, "type": "wikibase-entityid"},
            comparable_value=qid,
        )

    if wikibase_datatype in ("string", "external-id", "commonsMedia"):
        normalized = normalize_whitespace(value)
        if not normalized:
            return ConversionResult(success=False, error="empty_string_value")
        return ConversionResult(
            success=True,
            datavalue={"value": normalized, "type": "string"},
            comparable_value=normalized,
        )

    if wikibase_datatype == "url":
        normalized = normalize_whitespace(value)
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", normalized):
            return ConversionResult(success=False, error=f"not_a_uri:{normalized!r}")
        return ConversionResult(
            success=True,
            datavalue={"value": normalized, "type": "string"},
            comparable_value=normalize_uri_for_comparison(normalized),
        )

    if wikibase_datatype == "monolingualtext":
        text = normalize_whitespace(value)
        lang = language or default_language
        if not text:
            return ConversionResult(success=False, error="empty_monolingualtext_value")
        return ConversionResult(
            success=True,
            datavalue={"value": {"text": text, "language": lang}, "type": "monolingualtext"},
            comparable_value=f"{lang}:{text}",
        )

    if wikibase_datatype == "time":
        return _build_time_datavalue(value, calendar_model)

    if wikibase_datatype == "quantity":
        return _build_quantity_datavalue(value)

    return ConversionResult(success=False, error=f"unhandled_wikibase_datatype:{wikibase_datatype}")


def _build_time_datavalue(value: str, calendar_model: str) -> ConversionResult:
    text = normalize_whitespace(value)

    match = _DATE_FULL_RE.match(text)
    if match:
        year, month, day = match.groups()
        precision = 11
    else:
        match = _DATE_YEAR_MONTH_RE.match(text)
        if match:
            year, month = match.groups()
            day = "00"
            precision = 10
        elif _DATE_YEAR_RE.match(text):
            year, month, day = text, "00", "00"
            precision = 9
        else:
            return ConversionResult(success=False, error=f"unparseable_time_value:{text!r}")

    sign = "+" if not year.startswith("-") else ""
    padded_year = year.zfill(4) if not year.startswith("-") else "-" + year[1:].zfill(4)
    time_string = f"{sign}{padded_year}-{month or '00'}-{day or '00'}T00:00:00Z"

    return ConversionResult(
        success=True,
        datavalue={
            "value": {
                "time": time_string,
                "timezone": 0,
                "before": 0,
                "after": 0,
                "precision": precision,
                "calendarmodel": calendar_model,
            },
            "type": "time",
        },
        comparable_value=time_string,
    )


def _build_quantity_datavalue(value: str) -> ConversionResult:
    text = normalize_whitespace(value)
    match = _QUANTITY_RE.match(text)
    if not match:
        return ConversionResult(success=False, error=f"unparseable_quantity_value:{text!r}")
    amount, unit_text = match.groups()
    signed_amount = amount if amount.startswith(("+", "-")) else f"+{amount}"
    unit = "1" if not unit_text.strip() else unit_text.strip()
    return ConversionResult(
        success=True,
        datavalue={"value": {"amount": signed_amount, "unit": unit}, "type": "quantity"},
        comparable_value=f"{signed_amount}|{unit}",
    )


def convert_owl_value_to_wikibase(
    value_kind: str,
    raw_value: str,
    wikibase_datatype: str,
    literal_datatype: Optional[str] = None,
    language: Optional[str] = None,
    default_language: str = "en",
    resolved_qid: Optional[str] = None,
) -> ConversionResult:
    """Convert one OWL statement value into a Wikibase datavalue.

    For ``wikibase-item`` targets, ``value_kind`` must be ``"resource"`` and
    ``resolved_qid`` must already be known (resolved by the planner before
    calling this function). All other datatypes convert the raw literal
    value directly.
    """
    if wikibase_datatype == "wikibase-item":
        if value_kind != "resource":
            return ConversionResult(
                success=False,
                error=f"datatype_mismatch: wikibase-item property received value_kind={value_kind!r}",
            )
        if not resolved_qid:
            return ConversionResult(success=False, error="unresolved_target_qid")
        return build_wikibase_datavalue("wikibase-item", resolved_qid)

    if value_kind == "resource" and wikibase_datatype not in ("url", "string", "external-id"):
        return ConversionResult(
            success=False,
            error=f"datatype_mismatch: resource value cannot populate {wikibase_datatype!r}",
        )

    return build_wikibase_datavalue(
        wikibase_datatype,
        raw_value,
        language=language,
        default_language=default_language,
    )


def normalize_value_for_comparison(wikibase_datatype: str, value: str) -> str:
    """Normalize a value for deduplication/equality comparison against existing claims."""
    if wikibase_datatype == "wikibase-item":
        return normalize_whitespace(value)
    if wikibase_datatype == "url":
        return normalize_uri_for_comparison(value)
    if wikibase_datatype in ("string", "external-id", "commonsMedia"):
        return normalize_whitespace(value)
    if wikibase_datatype in ("monolingualtext", "time", "quantity"):
        return normalize_whitespace(value)
    return normalize_whitespace(value)
