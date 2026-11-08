"""Best-effort append-only audit trail for REAL broker submissions.

``orders.jsonl`` is a human audit trail, never an authority: the SQLite
submission ledger (``state.sqlite3``) owns quota, idempotency and recovery.
This module therefore has exactly two jobs.

1. WRITE one machine-clean record per submission attempt, so a crash or a lost
   terminal does not erase what was sent.  Values are strictly typed: anything
   that is not a JSON-safe primitive (a ``unittest.mock.MagicMock`` that leaked
   from a test into a production code path, an ``ib_async`` object, a numpy
   scalar, ...) is DROPPED field by field and reported in ``dropped_fields``.
   A line reading ``"orderId": "<MagicMock name='mock.placeOrder()...'>"`` is
   worse than no line at all: it looks like evidence and is not.  Synthetic
   reprs that arrive as plain strings are refused for the same reason.

2. READ/CLEAN records for ``thetamill audit`` and the legacy-code import, so a
   synthetic row can never be presented as a real broker order.

No broker, config, network, ledger or clock side effect beyond the file append.
The writer is best effort: it never raises into a caller that has already sent
an order.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple

AUDIT_SCHEMA = "thetamill.audit.v1"

# Reprs produced by unittest.mock / asynctest objects.  A record carrying one of
# these is a unit-test artefact, never a broker observation.
_SYNTHETIC_MARKERS = (
    "<MagicMock", "<AsyncMock", "<NonCallableMock", "<NonCallableMagicMock",
    "<Mock ", "<call ",
)

# Field names that must survive cleaning for a row to describe a submission.
_STATUS_FIELDS = ("status", "outcome", "state")

_DROP = object()


class AuditValueError(ValueError):
    """A value is not a JSON-safe primitive and must not be logged."""


def looks_synthetic(value: str) -> bool:
    """True when the text is a unit-test mock repr rather than a real value."""
    return any(marker in value for marker in _SYNTHETIC_MARKERS)


def _clean_scalar(name: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if looks_synthetic(value):
            raise AuditValueError(f"{name} is a mock repr, not a real value")
        return value
    if isinstance(value, int):  # bool was handled above
        return int(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise AuditValueError(f"{name} is not finite")
        return format(value.normalize(), "f")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AuditValueError(f"{name} is not finite")
        return value
    raise AuditValueError(f"{name} is {type(value).__name__}, not a JSON-safe value")


def clean_value(name: str, value: Any, dropped: list[str]) -> Any:
    """Return a JSON-safe copy of ``value`` or the ``_DROP`` sentinel."""
    try:
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for key, item in value.items():
                if not isinstance(key, str) or not key:
                    dropped.append(f"{name}[{type(key).__name__}]")
                    continue
                child = clean_value(f"{name}.{key}", item, dropped)
                if child is not _DROP:
                    out[key] = child
            return out
        if isinstance(value, (list, tuple, set, frozenset)):
            items = []
            for index, item in enumerate(value):
                child = clean_value(f"{name}[{index}]", item, dropped)
                if child is not _DROP:
                    items.append(child)
            return items
        return _clean_scalar(name, value)
    except AuditValueError:
        dropped.append(name)
        return _DROP


def clean_record(fields: Mapping[str, Any]) -> Tuple[dict, list[str]]:
    """Clean every field; return ``(record, dropped_field_names)``."""
    dropped: list[str] = []
    record: dict[str, Any] = {}
    for key, value in fields.items():
        if not isinstance(key, str) or not key:
            dropped.append(f"[{type(key).__name__}]")
            continue
        cleaned = clean_value(key, value, dropped)
        if cleaned is not _DROP:
            record[key] = cleaned
    return record, dropped


def is_real_record(row: Any) -> bool:
    """True only for a row that could describe a real submission.

    Rejects mock reprs anywhere in the structure and an ``orderId`` that is not
    an integer (or a digit string).  A row WITHOUT an order id or a status is
    accepted: real legacy records may predate both, and dropping real history is
    the worse failure — only positive proof of a mock hides a row.
    """
    if not isinstance(row, dict):
        return False
    if is_synthetic_record(row):
        return False
    order_id = row.get("orderId")
    if order_id is None:
        return True
    if isinstance(order_id, bool):
        return False
    if isinstance(order_id, int):
        return True
    if isinstance(order_id, str):
        return order_id.strip().isdigit()
    return False


def is_synthetic_record(row: Any) -> bool:
    """True when ANY value in the row is a unit-test mock repr.

    The narrow test used by legacy import: a genuine legacy record may lack a
    status or an order id (never drop real history), but a mock repr anywhere
    proves the row was produced by a patched broker and is not an order.
    """
    def scan(value: Any) -> bool:
        if isinstance(value, str):
            return looks_synthetic(value)
        if isinstance(value, Mapping):
            return any(scan(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return any(scan(v) for v in value)
        return False

    return scan(row)


def read_records(path: Any) -> list[dict]:
    """Parse a JSONL audit file, skipping blank/broken lines (never raising)."""
    target = Path(path)
    rows: list[dict] = []
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def append_record(path: Any, fields: Mapping[str, Any], *,
                  now: Optional[datetime] = None,
                  warn: Optional[Callable[[str], None]] = None) -> Optional[dict]:
    """Append ONE cleaned record; return it, or ``None`` when nothing was written.

    A record that loses its identifying fields (no timestamp-adjacent status and
    no order id) is NOT written: an empty line would only suggest an event that
    cannot be described.  Failures are reported through ``warn`` and swallowed —
    the order has already been sent by the time this runs.
    """
    if not isinstance(fields, Mapping):
        fields = {}
    stamp = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    record: dict[str, Any] = {
        "schema": AUDIT_SCHEMA,
        "ts": stamp.astimezone().isoformat(timespec="seconds"),
    }
    cleaned, dropped = clean_record(fields)
    record.update(cleaned)
    if dropped:
        record["dropped_fields"] = sorted(set(dropped))
    if not is_real_record(record) or not any(
            record.get(key) for key in _STATUS_FIELDS + ("orderId",)):
        # Everything identifying was dropped (or was synthetic): refuse to
        # write a row that would look like broker evidence but is not.
        if warn:
            warn("audit record carried no real submission fields"
                 + (f" (dropped: {', '.join(sorted(set(dropped)))})" if dropped else ""))
        return None
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        if warn:
            warn(str(exc))
        return None
    return record


def _identifier(name: str, value: Any) -> Any:
    """A broker id: integer when it is a clean digit string, else as supplied.

    ``orderId``/``permId`` reach this module as strings (the engine's
    ``LegOutcome`` keeps every id as text) but the trail has always recorded
    them as numbers; parsing is lossless and only for digit strings.
    """
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return value


def submission_fields(*, environment: str, port: Any, strategy: str,
                      account: str, intent_id: str, outcome: str,
                      legs: Iterable[Any] = (), decision: str = "",
                      day_key: str = "", reason: str = "") -> dict:
    """Canonical audit payload for ONE submission attempt.

    ``legs`` accepts engine ``LegOutcome`` objects (or any object/mapping with
    ``order_ref``/``leg_index``/``state``/``order_id``/``perm_id``).  Only
    primitives are emitted, so the writer's cleaning step is a second line of
    defence rather than the only one.
    """
    def pick(item: Any, name: str, default: Any = None) -> Any:
        if isinstance(item, Mapping):
            return item.get(name, default)
        return getattr(item, name, default)

    leg_rows = [{"order_ref": pick(item, "order_ref"),
                 "index": pick(item, "leg_index"),
                 "state": pick(item, "state"),
                 "orderId": _identifier("orderId", pick(item, "order_id")),
                 "permId": _identifier("permId", pick(item, "perm_id"))}
                for item in legs]
    order_ids = [row["orderId"] for row in leg_rows if row["orderId"] not in (None, "")]
    return {
        "kind": "submission",
        "env": environment,
        "port": port,
        "strategy": strategy,
        "preset": strategy,
        "account": account,
        "intent": intent_id,
        "outcome": outcome,
        "status": outcome,
        "decision": decision,
        "day_key": day_key,
        "orderId": order_ids[0] if order_ids else None,
        "legs": leg_rows,
        "reason": reason,
    }
