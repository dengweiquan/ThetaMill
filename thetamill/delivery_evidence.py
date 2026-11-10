"""Neutral stock-only deliverable evidence loader and identity matcher.

Provides schema validation, strict identity matching, and immutable caching for
explicit per-OPTION-conId stock-only delivery evidence (thetamill.deliverables.v1).
Shared by roll recommendations, ordinary sell planning, and preflight/submission.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from . import broker_state as bs

# Separate, versioned deliverable-evidence document.  It is NOT a plan, NOT a
# roll recommendation, and carries no order/executable fields.
DELIVERABLES_SCHEMA = "thetamill.deliverables.v1"


class DeliverableEvidenceError(ValueError):
    """Delivery evidence is missing, malformed, stale or contradicts identity."""


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


# --------------------------------------------------------------------------- #
# Small pure helpers (never leak raw account IDs, never coerce bad numbers)
# --------------------------------------------------------------------------- #

def _finite(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DeliverableEvidenceError(f"{label}: expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise DeliverableEvidenceError(f"{label}: expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise DeliverableEvidenceError(
            f"{label}: expected a {'positive ' if positive else ''}finite number")
    return result


def _aware(value, label: str) -> datetime:
    """Strict ISO-8601 aware timestamp (an offset is mandatory, never assumed)."""
    if not isinstance(value, str) or not value.strip():
        raise DeliverableEvidenceError(f"{label}: expected an ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise DeliverableEvidenceError(f"{label}: invalid ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DeliverableEvidenceError(f"{label}: timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _identity_for(instrument: bs.Instrument) -> dict:
    """Full, explicit identity of a qualified Instrument (independent of cli)."""
    return {
        "con_id": instrument.con_id,
        "underlying_id": instrument.underlying_id,
        "symbol": instrument.symbol,
        "currency": instrument.currency,
        "sec_type": instrument.sec_type,
        "exchange": instrument.exchange,
        "multiplier": str(_finite(instrument.multiplier, "multiplier", positive=True)),
        "right": instrument.right,
        "strike": str(_finite(instrument.strike, "strike", positive=True)),
        "expiry": instrument.expiry,
        "trading_class": instrument.trading_class,
    }


_IDENTITY_FIELDS = frozenset(_identity_for(
    bs.Instrument(1, "X", "USD", "OPT", "SMART", 1, Decimal(1), "P", Decimal(1),
                  "20260101", "X")).keys())


def _identity_matches(record: Mapping, inst: bs.Instrument) -> bool:
    """STRICT equality of every identity field, including the actual multiplier.

    ``con_id``/``underlying_id`` are compared as integers; multiplier/strike as
    exact Decimals (so ``100`` == ``100.0`` but a different multiplier never
    matches); all other fields are exact string matches.  Every known field
    must be PRESENT and agree; unknown extra fields are ignored.
    """
    expected = _identity_for(inst)
    for field in _IDENTITY_FIELDS:
        if field not in record:
            return False
        got, want = record[field], expected[field]
        if field in {"con_id", "underlying_id"}:
            if isinstance(got, bool) or not isinstance(got, int) or got != want:
                return False
        elif field in {"multiplier", "strike"}:
            try:
                if _finite(got, field, positive=True) != _finite(want, field, positive=True):
                    return False
            except DeliverableEvidenceError:
                return False
        else:
            if not isinstance(got, str) or got != want:
                return False
    return True


# --------------------------------------------------------------------------- #
# Public evidence loader (also consumed by later execution layers)
# --------------------------------------------------------------------------- #

def load_delivery_evidence(path, instruments: Iterable[bs.Instrument],
                           now: datetime | None = None
                           ) -> Mapping[int, Mapping[str, object]]:
    """Load and cross-check explicit per-OPTION-conId stock-only evidence.

    Parameters
    ----------
    path
        ``str`` / ``Path`` to a ``thetamill.deliverables.v1`` JSON document::

            {
              "schema": "thetamill.deliverables.v1",
              "records": [
                {
                  "instrument": {<full instrument_data dict, every identity field>},
                  "stock_only": true,
                  "source": "IB contract details / human review, non-empty",
                  "verified_at":  "2026-11-07T18:00:00+00:00",
                  "expires_at":  "2026-11-08T18:00:00+00:00"
                }, ...
              ]
            }

        For convenience ``instrument`` may instead be ``instrument_data``, or
        the record may carry the identity fields inline.  Every field returned
        by :func:`thetamill.roll.instrument_data` is compared against the
        *qualified* Instrument; the actual multiplier is never guessed.
    instruments
        The real qualified Instruments that will be traded (old short + every
        candidate / all prepared OPT legs).  A record confirms ONLY the exact
        OPTION ``con_id`` it names; sharing an underlying conId, tradingClass,
        symbol or multiplier grants NO authorization (an adjusted contract can
        settle into cash/basket).  STK instruments are intrinsically stock and
        are skipped (they need no per-contract evidence).
    now
        Aware datetime used for expiry checks; defaults to
        ``datetime.now(timezone.utc)``.  Naive datetimes are rejected.

    Returns
    -------
    An immutable mapping ``{option_con_id: evidence_record}`` containing ONLY
    conIds that were (a) requested in ``instruments`` and (b) fully confirmed.
    Test membership per OPTION conId with ``con_id in confirmed``; it is NEVER
    keyed by underlying conId.  Missing/partial coverage is NOT an error: the
    absent conIds are simply unconfirmed and callers MUST reject them.  Invalid
    schema / malformed JSON / a single bad record raises
    :class:`DeliverableEvidenceError` (fail closed; never a partial guess).
    A None path or missing file yields an EMPTY mapping. Malformed/empty JSON
    raises DeliverableEvidenceError; nested records are recursively immutable.
    """
    moment = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(moment, datetime) or moment.tzinfo is None \
            or moment.utcoffset() is None:
        raise DeliverableEvidenceError("now must be an aware datetime")

    requested: dict[int, bs.Instrument] = {}
    for inst in instruments or ():
        if not isinstance(inst, bs.Instrument):
            raise DeliverableEvidenceError("instruments must be broker_state.Instrument objects")
        if inst.sec_type != "OPT":
            continue  # stock itself needs no delivery evidence
        if isinstance(inst.con_id, bool) or type(inst.con_id) is not int or inst.con_id <= 0:
            raise DeliverableEvidenceError("instrument requires a positive integer conId")
        prior = requested.get(inst.con_id)
        if prior is not None and _identity_for(prior) != _identity_for(inst):
            raise DeliverableEvidenceError(f"conflicting identity for conId {inst.con_id}")
        requested[inst.con_id] = inst

    if path is None:
        return MappingProxyType({})
    p = Path(path).expanduser()
    if not p.exists():
        return MappingProxyType({})
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise DeliverableEvidenceError(
            f"cannot read delivery evidence: {type(exc).__name__}") from None
    if not isinstance(doc, dict) or doc.get("schema") != DELIVERABLES_SCHEMA:
        raise DeliverableEvidenceError(
            f"delivery evidence schema must be {DELIVERABLES_SCHEMA}")
    if any(k in doc for k in ("items", "orders", "orders_to_place", "placeOrder", "submit")):
        raise DeliverableEvidenceError("delivery evidence must not carry executable fields")
    records = doc.get("records")
    if not isinstance(records, list):
        raise DeliverableEvidenceError("delivery evidence requires a 'records' list")

    confirmed: dict[int, Mapping[str, object]] = {}
    for index, record in enumerate(records):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            raise DeliverableEvidenceError(f"{label}: expected an object")
        identity = record.get("instrument", record.get("instrument_data", record))
        if not isinstance(identity, dict):
            raise DeliverableEvidenceError(f"{label}: missing instrument identity")
        con_id = identity.get("con_id")
        if isinstance(con_id, bool) or type(con_id) is not int or con_id <= 0:
            raise DeliverableEvidenceError(f"{label}: invalid option conId")
        if record.get("stock_only") is not True:
            raise DeliverableEvidenceError(f"{label}: stock_only must be true")
        source = record.get("source")
        if not isinstance(source, str) or not source.strip():
            raise DeliverableEvidenceError(f"{label}: source must be a non-empty string")
        verified = _aware(record.get("verified_at"), f"{label}.verified_at")
        expires = _aware(record.get("expires_at"), f"{label}.expires_at")
        if expires <= verified:
            raise DeliverableEvidenceError(f"{label}: expires_at must be after verified_at")
        if verified > moment:
            raise DeliverableEvidenceError(f"{label}: verified_at is in the future")
        if expires <= moment:
            raise DeliverableEvidenceError(f"{label}: evidence has expired")

        inst = requested.get(con_id)
        if inst is None:
            continue  # evidence for a conId we are not trading is not a confirmation
        if not _identity_matches(identity, inst):
            raise DeliverableEvidenceError(
                f"{label}: identity/multiplier does not match qualified conId {con_id}")
        confirmed[con_id] = _freeze(record)
    return MappingProxyType(confirmed)


__all__ = [
    "DELIVERABLES_SCHEMA",
    "DeliverableEvidenceError",
    "load_delivery_evidence",
]
