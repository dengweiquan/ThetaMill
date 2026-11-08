#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standard-library SQLite submission ledger for ThetaMill (stage B foundation).

This module is the authoritative *local* record of submission intents, risk
reservations, per-leg order references and broker-reconciliation results.  It
has no broker, network, order-placement or account-query dependency: the future
execution coordinator supplies all facts and calls these APIs around its own
``placeOrder`` calls.

Safety model
------------
* Every state-changing method opens its own short ``BEGIN IMMEDIATE``
  transaction with a bounded ``busy_timeout``.  Intent claiming, risk-budget
  reservation and state transitions are each committed atomically, so two
  processes on the same host cannot both claim an intent or both spend the
  same remaining budget.
* ``account_scope()`` optionally serialises the *whole* refresh / check /
  claim / pre-send sequence for one (account, environment) across processes
  with a POSIX ``fcntl`` advisory lock.  The SQL transaction is never held
  across a network call; the lock may be.
* A corrupt, unwritable, foreign or unsupported-schema database is refused
  (fail closed); it is never treated as an empty fresh ledger.
* An intent carries a fresh UUID, so identical business payload+config can be
  explicitly re-preflighted into a new intent.  The short approval code is
  bound to that intent's full canonical payload+config digest.
* Once an order may have reached the broker (``submitting`` / ``submitted`` /
  ``unknown``) the intent is consumed and is never auto-released or
  auto-retried.  Only an explicit pre-send abort releases a reservation; a
  ``submitted`` leg KEEPS its risk reservation outstanding until it is
  *positively mapped* to broker state (never merely absent from open orders).
* Attempts are counted PER LEG at the network boundary:
  ``begin_leg_attempt(order_ref)`` durably persists a ``submitting`` leg and
  increments the persistent day submission count BEFORE the caller invokes
  ``placeOrder``.  It counts "may have been sent" conservatively and is never
  refunded afterwards.  Legs that were never attempted are ``skipped`` and
  return their unspent reserved day slots.  Per-leg outcomes are recorded
  with ``record_leg_result``; ``confirm_leg`` accepts only positive broker
  statuses.
* A reservation stays outstanding while any leg is unresolved; the whole
  account can be conservatively blocked via ``has_unresolved_submissions``.
* An open-order list that does *not* contain a leg is never taken as proof
  that the order was not submitted.  Reconciliation is idempotent keyed on the
  stable ``orderRef`` / ``permId`` and stores observations only.
* Risk amounts are decimal quantities stored as strings (never floats) and
  compared with :class:`decimal.Decimal`.  Day keys come from a configured
  IANA timezone; all timestamps are UTC.
* Submission attempts are persistent: ``submitted`` / ``rejected`` /
  ``cancelled`` / ``unknown`` never refund the daily submission count.
  Unknown / submitted reservations from an earlier day are retained, not
  purged.

Explicit limitations
--------------------
This provides single-host, single-database serialisation and durable local
bookkeeping.  It does **not** claim exactly-once broker execution, does not
provide a distributed lock, does not control other machines, manual TWS use
or third-party clients, and cannot by itself prove what the broker did.
``PRAGMA quick_check`` is a sanity check, not a full forensic integrity proof.

Resource-key convention (agreed with the coordinator): keys are opaque strings
but callers should use ``cash_base``, ``shares:<underlyingConId>:<currency>``,
``close:<conId>`` and ``exposure:<underlyingConId>:<currency>``.  Resources
are scoped by (account, environment) because each account owns its own cash /
shares / obligations.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:  # POSIX only; fail closed (not silently unlocked) elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5000
_REQUIRED_TABLES = frozenset({
    "meta", "intents", "legacy_codes", "day_budget", "resources",
    "reservations", "reservation_items", "legs", "reconciliation",
})

# Reservation states that still occupy local risk.  ``submitted`` is NOT a
# release: the order has been sent but has not yet been *positively mapped*
# to broker state, so the risk must remain reserved.  Only ``confirmed``
# (explicit positive snapshot handoff), ``rejected``, ``released``,
# ``aborted`` free risk.  ``unknown`` is retained forever until an explicit
# operator decision.
_RISK_OUTSTANDING_STATES = frozenset({
    "reserved", "submitting", "submitted", "unknown",
})
# States that mean at least one leg may have reached the broker: the whole
# account is conservatively blocked from new risk until they are resolved.
_UNRESOLVED_STATES = frozenset({"submitting", "submitted", "unknown"})
# Terminal per-leg states (a leg cannot be attempted twice).
_LEG_TERMINAL_STATES = frozenset({
    "submitted", "rejected", "unknown", "confirmed", "skipped",
})
# Broker statuses that prove the broker actually saw the order.  An absent
# open-order is deliberately NOT in this set (it is not proof of visibility).
POSITIVE_BROKER_STATES = frozenset({
    "Submitted", "PreSubmitted", "Filled", "PartiallyFilled",
    "Cancelled", "ApiCancelled", "Inactive",
})
# A durable pre-send attempt is never refunded from the day budget.
_LEG_ATTEMPT_STATES = frozenset({"submitted", "rejected", "unknown"})


class StateError(Exception):
    """Base class for ledger errors (always fail closed on these)."""


class SchemaError(StateError):
    """Corrupt, unwritable, foreign or unsupported-schema database."""


class ClaimError(StateError):
    """An intent could not be atomically claimed (replay, expiry, mismatch)."""


class BudgetError(StateError):
    """A daily submission budget or risk resource could not be reserved."""


class LockUnavailableError(StateError):
    """The optional cross-process account lock is unsupported or unavailable."""


class MigrationFreeze(StateError):
    """Conservative first-day freeze: a fresh/empty ledger cannot prove that
    zero orders were already submitted today.  New risk stays blocked until
    the next configured natural day or an explicitly imported, *verified*
    local submission count.  ``explanation`` is shown to the operator.
    """

    def __init__(self, explanation: str) -> None:
        super().__init__(explanation)
        self.explanation = explanation


@dataclass(frozen=True)
class RiskReservationSpec:
    """One ``(currency, resource_key, amount)`` leg of a reservation.

    ``amount`` is a decimal amount passed as a string (never a float).  Use
    ``cash_base`` for base-currency budget checks, or
    ``shares:<underlyingConId>:<currency>``, ``close:<conId>`` and
    ``exposure:<underlyingConId>:<currency>`` for delivery / exposure limits.
    """

    currency: str
    resource_key: str
    amount: str

    def key(self) -> Tuple[str, str]:
        return (self.currency, self.resource_key)


@dataclass(frozen=True)
class ReservationView:
    """Outstanding reservation as exposed to the future risk coordinator."""

    reservation_id: str
    intent_id: str
    scope: str
    day_key: str
    kind: str
    state: str
    created_at: str
    day_submission_slots: int
    items: Tuple[Tuple[str, str, str], ...]  # (currency, resource_key, amount_text)


@dataclass(frozen=True)
class LegView:
    """A per-leg order record and its submission state.

    ``reservation_id`` anchors the leg to exactly one reservation: an aborted
    reservation's refs can never be reused as the anchor for a later one.
    """

    intent_id: str
    reservation_id: str
    order_ref: str
    leg_index: int
    contract_id: str
    action: str
    quantity: str
    state: str
    order_id: Optional[str]
    perm_id: Optional[str]
    created_at: str
    updated_at: str
    submitted_at: Optional[str] = None


@dataclass(frozen=True)
class ReconciliationView:
    """A broker observation recorded against a leg (never auto-retried)."""

    observation_id: str
    intent_id: str
    order_ref: str
    perm_id: Optional[str]
    order_id: Optional[str]
    status: str
    broker_status: Optional[str]
    note: str
    observed_at: str


# -- pure helpers ---------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _aware(dt: Optional[datetime], label: str) -> datetime:
    if dt is None:
        return _utc_now()
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise StateError(f"{label} must be an aware datetime")
    return dt


def day_key_for(day_timezone: str, now: Optional[datetime] = None) -> str:
    """Return ``YYYY-MM-DD`` for ``now`` in the configured IANA timezone.

    ``now`` must be an aware datetime; naive values are rejected rather than
    silently interpreted in the host's local timezone.  An unknown IANA name
    raises :class:`StateError` (never silently falls back to UTC).
    """
    try:
        tz = ZoneInfo(day_timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise StateError(f"unknown IANA day_timezone: {day_timezone!r}") from exc
    return _aware(now, "now").astimezone(tz).date().isoformat()


def _canonical_bytes(value: Any) -> bytes:
    # default=str keeps digests stable for non-JSON-native scalars (Decimal,
    # etc.) in exactly the same way the stored payload is serialised.
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def config_digest(config: Mapping[str, Any]) -> str:
    """Canonical sha256 of the risk/execution config (change invalidates intents)."""
    if not isinstance(config, Mapping):
        raise StateError("config must be a mapping")
    return _sha256(config)


def compute_full_digest(*, intent_id: str, account: str, environment: str,
                       payload: Mapping[str, Any],
                       config: Mapping[str, Any]) -> str:
    """Recompute an intent's full canonical digest for config-change checks.

    The coordinator should call this at confirmation time with the *current*
    config and compare against the stored ``full_digest``; a mismatch means the
    config changed and the old review is no longer valid.  The UUID nonce is
    part of the object, so re-preflighting identical business terms yields a
    fresh intent instead of being blocked forever by a stable content hash.
    """
    cfg_digest = config_digest(config)
    return _sha256({"schema": SCHEMA_VERSION, "intent_id": intent_id,
                    "account": account, "environment": environment,
                    "payload": payload, "config": config,
                    "config_digest": cfg_digest})


def approval_code(full_digest: str, key_id: str) -> str:
    """Derive a short, human-enterable 8-char approval code.

    Bound to the *full* canonical payload+config digest and to ``key_id``
    (pass the account/environment scope).  A guessed code must still be
    presented with its full digest: :meth:`SubmissionLedger.claim_intent`
    recomputes and verifies the binding.  ``key_id`` is not a secret.
    """
    return _sha256({"key_id": key_id, "digest": full_digest})[:8].upper()


def _to_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def _decimal_text(value: Any, label: str) -> str:
    """Validate a decimal amount given as str / int / Decimal (never float)."""
    if isinstance(value, bool) or isinstance(value, float):
        raise StateError(f"{label} must be a decimal string/int/Decimal, not {type(value).__name__}")
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise StateError(f"invalid decimal amount for {label}: {value!r}") from exc
    if not d.is_finite():
        raise StateError(f"{label} must be finite, got {value!r}")
    return str(d)


def _validate_scope(account: str, environment: str) -> None:
    if not isinstance(account, str) or not account.strip():
        raise StateError("account must be a non-empty string")
    if not isinstance(environment, str) or not environment.strip():
        raise StateError("environment must be a non-empty string")


def _scope(account: str, environment: str) -> str:
    return f"{account}\x1f{environment}"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class AccountLock:
    """Optional account-scoped cross-process advisory lock (POSIX only).

    Serialises the refresh / final-check / claim / reserve / pre-send sequence
    for one (account, environment) without holding a SQLite transaction across
    any network call.  On platforms without ``fcntl`` it raises
    :class:`LockUnavailableError` (fail closed) rather than silently proceeding
    without mutual exclusion.  Advisory locks are per-host only.
    """

    def __init__(self, directory: os.PathLike | str, account: str,
                 environment: str) -> None:
        _validate_scope(account, environment)
        self.directory = Path(directory)
        self.account = account
        self.environment = environment
        self._fd: Optional[int] = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LockUnavailableError(f"cannot create lock directory: {exc}") from exc
        name = "lock-" + _sha256({"account": account,
                                  "environment": environment})[:24] + ".lock"
        self.path = self.directory / name

    def __enter__(self) -> "AccountLock":
        if fcntl is None or not hasattr(fcntl, "flock"):
            raise LockUnavailableError("fcntl.flock unavailable on this platform")
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise LockUnavailableError(f"cannot open lock file: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # blocking; single-host serialisation
        except OSError as exc:
            os.close(fd)
            raise LockUnavailableError(f"cannot acquire account lock: {exc}") from exc
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


@contextmanager
def account_scope(directory: os.PathLike | str, account: str, environment: str):
    """Context manager yielding an :class:`AccountLock` for (account, env).

    Hold this around the coordinator's full pre-send sequence; the ledger's own
    SQL transactions remain independent and short-lived.
    """
    with AccountLock(directory, account, environment) as lock:
        yield lock


# -- schema ---------------------------------------------------------------

_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE intents (
        intent_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        account TEXT NOT NULL,
        environment TEXT NOT NULL,
        full_digest TEXT NOT NULL UNIQUE,
        approval_code TEXT NOT NULL,
        payload TEXT NOT NULL,
        config TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        claimed_at TEXT,
        terminal_at TEXT,
        consumed INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE legacy_codes (
        code TEXT NOT NULL,
        scope TEXT NOT NULL,
        source TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        PRIMARY KEY (code, scope)
    )""",
    """CREATE TABLE day_budget (
        scope TEXT NOT NULL,
        day_key TEXT NOT NULL,
        limit_count INTEGER NOT NULL,
        reserved_count INTEGER NOT NULL DEFAULT 0,
        submitted_count INTEGER NOT NULL DEFAULT 0,
        migration_frozen INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (scope, day_key)
    )""",
    """CREATE TABLE resources (
        scope TEXT NOT NULL,
        currency TEXT NOT NULL,
        resource_key TEXT NOT NULL,
        amount TEXT NOT NULL,
        PRIMARY KEY (scope, currency, resource_key)
    )""",
    """CREATE TABLE reservations (
        reservation_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        day_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        day_submission_slots INTEGER NOT NULL DEFAULT 0,
        UNIQUE (reservation_id, scope)
    )""",
    """CREATE TABLE reservation_items (
        reservation_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        currency TEXT NOT NULL,
        resource_key TEXT NOT NULL,
        amount TEXT NOT NULL,
        PRIMARY KEY (reservation_id, scope, currency, resource_key)
    )""",
    """CREATE TABLE legs (
        intent_id TEXT NOT NULL,
        reservation_id TEXT NOT NULL,
        order_ref TEXT NOT NULL UNIQUE,
        leg_index INTEGER NOT NULL,
        contract_id TEXT NOT NULL,
        action TEXT NOT NULL,
        quantity TEXT NOT NULL,
        state TEXT NOT NULL,
        order_id TEXT,
        perm_id TEXT,
        created_at TEXT NOT NULL,
        submitted_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE (intent_id, reservation_id, order_ref),
        UNIQUE (reservation_id, leg_index)
    )""",
    """CREATE TABLE reconciliation (
        observation_id TEXT PRIMARY KEY,
        intent_id TEXT NOT NULL,
        order_ref TEXT NOT NULL,
        perm_id TEXT,
        order_id TEXT,
        status TEXT NOT NULL,
        broker_status TEXT,
        note TEXT NOT NULL DEFAULT '',
        observed_at TEXT NOT NULL
    )""",
    "CREATE INDEX idx_recon_ref ON reconciliation(order_ref)",
    "CREATE INDEX idx_legs_intent ON legs(intent_id)",
)


def _reservation_view(row: sqlite3.Row,
                      items: Sequence[sqlite3.Row]) -> ReservationView:
    return ReservationView(
        reservation_id=row["reservation_id"], intent_id=row["intent_id"],
        scope=row["scope"], day_key=row["day_key"], kind=row["kind"],
        state=row["state"], created_at=row["created_at"],
        day_submission_slots=row["day_submission_slots"],
        items=tuple((i["currency"], i["resource_key"], i["amount"]) for i in items),
    )


def _leg_view(row: sqlite3.Row) -> LegView:
    return LegView(
        intent_id=row["intent_id"], reservation_id=row["reservation_id"],
        order_ref=row["order_ref"],
        leg_index=row["leg_index"], contract_id=row["contract_id"],
        action=row["action"], quantity=row["quantity"], state=row["state"],
        order_id=row["order_id"], perm_id=row["perm_id"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        submitted_at=row["submitted_at"],
    )


def _recon_view(row: sqlite3.Row) -> ReconciliationView:
    return ReconciliationView(
        observation_id=row["observation_id"], intent_id=row["intent_id"],
        order_ref=row["order_ref"], perm_id=row["perm_id"],
        order_id=row["order_id"], status=row["status"],
        broker_status=row["broker_status"], note=row["note"],
        observed_at=row["observed_at"],
    )


class SubmissionLedger:
    """Authoritative, versioned SQLite submission ledger (no broker dependency).

    ``path`` is the database file.  ``day_timezone`` is a required IANA name
    used to compute natural-day keys for submission budgets.  All public
    state-changing methods are atomic and safe across processes on this host;
    all read methods are snapshot reads.  Constructing against a corrupt /
    unwritable / foreign / unsupported-schema file raises :class:`SchemaError`
    instead of silently initialising a fresh database.
    """

    def __init__(self, path: os.PathLike | str, day_timezone: str, *,
                 busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
                 read_only: bool = False) -> None:
        self.path = str(path)
        self.day_timezone = day_timezone
        self.read_only = bool(read_only)
        try:
            ZoneInfo(day_timezone)  # fail fast on unknown / non-IANA names
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise StateError(f"unknown IANA day_timezone: {day_timezone!r}") from exc
        if isinstance(busy_timeout_ms, bool) or not isinstance(busy_timeout_ms, int) \
                or busy_timeout_ms <= 0:
            raise StateError("busy_timeout_ms must be a positive integer")
        self.busy_timeout_ms = busy_timeout_ms
        if self.read_only:
            # Query-only: NEVER create schema, migrate, or touch journal/WAL
            # files.  Opening with SQLite mode=ro makes this a hard guarantee,
            # and _assert_schema (integrity + tables + version) still fails
            # closed on an empty / foreign / corrupt / unsupported DB.
            self._validate_existing()
        else:
            self._initialize()

    def _validate_existing(self) -> None:
        """Validate an EXISTING ledger for read-only use, without any writes."""
        if not Path(self.path).is_file():
            raise SchemaError("read-only ledger: database file does not exist")
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise SchemaError(f"cannot open read-only ledger: {exc}") from exc
        try:
            self._assert_schema(conn, initialize=False, write_probe=False)
        finally:
            conn.close()

    # -- connection / transactions ----------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            # mode=ro: no file creation, no journal/WAL changes, no writes.
            from urllib.parse import quote
            uri = "file:" + quote(str(Path(self.path).resolve())) + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True,
                                   timeout=self.busy_timeout_ms / 1000.0,
                                   isolation_level=None)
        else:
            conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000.0,
                                   isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        return conn

    @contextmanager
    def _transaction(self, *, initialize: bool = False):
        """Short ``BEGIN IMMEDIATE`` transaction; always rolls back on error."""
        if self.read_only and not initialize:
            raise SchemaError("read-only ledger: state-changing operations are refused")
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise SchemaError(f"cannot open ledger database: {exc}") from exc
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise SchemaError(f"cannot begin ledger transaction: {exc}") from exc
            try:
                self._assert_schema(conn, initialize=initialize)
                yield conn
                conn.execute("COMMIT")
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        finally:
            conn.close()

    @contextmanager
    def _read(self):
        """Snapshot read that also verifies the ledger schema is intact.

        In WRITABLE mode the write probe runs inside a rolled-back
        ``BEGIN IMMEDIATE`` transaction, so a read-only *file* (which passes
        ``quick_check``) is still rejected instead of looking healthy.  BEGIN
        IMMEDIATE acquires the RESERVED lock up front so the probe never fails
        with a transient cross-process lock-upgrade error.  In READ-ONLY mode
        (``mode=ro``) no write probe is needed or possible: the OS/SQLite
        guarantee writes cannot happen, and the schema/integrity check still
        fails closed on an empty / foreign / corrupt / unsupported DB.
        """
        try:
            conn = self._connect()
        except sqlite3.Error as exc:
            raise SchemaError(f"cannot open ledger database: {exc}") from exc
        try:
            try:
                if self.read_only:
                    conn.execute("BEGIN")
                    probe = False
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    probe = True
            except sqlite3.Error as exc:
                # A corrupt file can fail at BEGIN time (before quick_check).
                raise SchemaError(f"ledger database is corrupt: {exc}") from exc
            try:
                self._assert_schema(conn, initialize=False, write_probe=probe)
                yield conn
            finally:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
        finally:
            conn.close()

    def _assert_schema(self, conn: sqlite3.Connection, *, initialize: bool,
                       write_probe: bool = True) -> None:
        """Validate the DB is ours, intact and writable. Never treat bad state as empty."""
        try:
            result = conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise SchemaError(f"ledger database is corrupt: {exc}") from exc
        if not result or result[0][0] != "ok":
            raise SchemaError("ledger database failed integrity check")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if initialize and not tables:
            return  # brand-new empty file: caller will create the schema
        if not _REQUIRED_TABLES.issubset(tables):
            raise SchemaError("ledger database is not a ThetaMill submission ledger "
                              "(missing required tables)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            raise SchemaError("ledger database has no schema_version")
        try:
            version = int(row["value"])
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"invalid schema_version: {row['value']!r}") from exc
        if version != SCHEMA_VERSION:
            raise SchemaError(f"unsupported ledger schema version {version} "
                              f"(expected {SCHEMA_VERSION})")
        if write_probe:
            # quick_check alone passes on a read-only file; only a real write
            # detects an unwritable ledger. Probe with a no-op upsert+delete.
            try:
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('__writable_probe__','1') "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
                conn.execute("DELETE FROM meta WHERE key='__writable_probe__'")
            except sqlite3.Error as exc:
                raise SchemaError(f"ledger database is not writable: {exc}") from exc

    def _initialize(self) -> None:
        parent = Path(self.path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SchemaError(f"cannot create ledger directory: {exc}") from exc
        with self._transaction(initialize=True) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                for stmt in _DDL:
                    conn.execute(stmt)
                conn.execute("INSERT INTO meta(key,value) VALUES('schema_version',?)",
                             (str(SCHEMA_VERSION),))
                conn.execute("INSERT INTO meta(key,value) VALUES('day_timezone',?)",
                             (self.day_timezone,))

    # -- intents -----------------------------------------------------------

    def create_intent(self, *, account: str, environment: str,
                      payload: Mapping[str, Any], config: Mapping[str, Any],
                      ttl_seconds: int,
                      now: Optional[datetime] = None) -> Dict[str, Any]:
        """Register a preflight intent with a canonical full payload+config digest.

        Returns a fresh UUID ``intent_id``, ``full_digest``, ``config_digest``,
        a short ``approval_code`` bound to the full digest, UTC ``expires_at``
        and the scope.  Each call is a distinct intent even for identical
        business terms, so re-preflight legitimately creates a new approval.
        """
        _validate_scope(account, environment)
        if not isinstance(payload, Mapping) or not isinstance(config, Mapping):
            raise StateError("payload and config must be mappings")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) \
                or not math.isfinite(float(ttl_seconds)) or ttl_seconds <= 0:
            raise StateError("ttl_seconds must be a positive finite number")
        now = _aware(now, "now")
        scope = _scope(account, environment)
        intent_id = _new_id("int")
        full_digest = compute_full_digest(
            intent_id=intent_id, account=account, environment=environment,
            payload=payload, config=config)
        code = approval_code(full_digest, scope)
        created_at = _iso(now)
        expires_at = _iso(now + timedelta(seconds=float(ttl_seconds)))
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM legacy_codes WHERE code=? AND scope=?",
                            (code, scope)).fetchone() is not None:
                raise ClaimError("approval code matches a consumed legacy code")
            conn.execute(
                "INSERT INTO intents(intent_id,scope,account,environment,full_digest,"
                "approval_code,payload,config,config_digest,state,created_at,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (intent_id, scope, account, environment, full_digest, code,
                 _to_text(payload), _to_text(config), config_digest(config),
                 "pending", created_at, expires_at))
        return {"intent_id": intent_id, "full_digest": full_digest,
                "config_digest": config_digest(config), "approval_code": code,
                "expires_at": expires_at, "scope": scope}

    def get_intent(self, intent_id: str) -> Optional[Dict[str, Any]]:
        """Read an intent row by id (coordinator verifies config_digest here)."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM intents WHERE intent_id=?",
                               (intent_id,)).fetchone()
            return dict(row) if row is not None else None

    def get_intent_by_digest(self, full_digest: str) -> Optional[Dict[str, Any]]:
        """Look up an intent by its full canonical digest (read-only)."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM intents WHERE full_digest=?",
                               (full_digest,)).fetchone()
            return dict(row) if row is not None else None

    def claim_intent(self, *, account: str, environment: str, intent_id: str,
                     full_digest: str, code: str,
                     now: Optional[datetime] = None) -> str:
        """Atomically claim a pending intent; exactly one caller can succeed.

        Verifies account/environment scoping, the short approval code (bound
        to the full digest), expiry, stored payload/config binding, legacy
        consumed codes and that the intent is still ``pending``.  On success
        the intent becomes ``claimed`` and consumed forever.  ``BEGIN
        IMMEDIATE`` serialises concurrent claimants across processes.
        """
        _validate_scope(account, environment)
        now = _aware(now, "now")
        stamp = _iso(now)
        scope = _scope(account, environment)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM intents WHERE intent_id=?",
                               (intent_id,)).fetchone()
            if row is None:
                raise ClaimError("unknown intent")
            if row["scope"] != scope:
                raise ClaimError("intent belongs to a different account/environment")
            if row["full_digest"] != full_digest:
                raise ClaimError("full payload/config digest mismatch (config may have changed)")
            if row["approval_code"] != code or approval_code(full_digest, scope) != code:
                raise ClaimError("approval code is not bound to this full digest")
            if row["expires_at"] <= stamp:
                raise ClaimError("intent has expired")
            if row["consumed"]:
                raise ClaimError("intent already consumed")
            if row["state"] != "pending":
                raise ClaimError(f"intent is not pending (state={row['state']})")
            if conn.execute("SELECT 1 FROM legacy_codes WHERE code=? AND scope=?",
                            (code, scope)).fetchone() is not None:
                raise ClaimError("approval code is a consumed legacy code")
            # Recompute the full digest from stored canonical payload + config
            # so a tampered/partially-written row cannot be claimed.
            recomputed = compute_full_digest(
                intent_id=row["intent_id"], account=row["account"],
                environment=row["environment"],
                payload=json.loads(row["payload"]),
                config=json.loads(row["config"]))
            if recomputed != row["full_digest"]:
                raise ClaimError("stored payload/config binding is inconsistent")
            changed = conn.execute(
                "UPDATE intents SET state='claimed', claimed_at=?, consumed=1 "
                "WHERE intent_id=? AND state='pending' AND consumed=0",
                (stamp, intent_id)).rowcount
            if changed != 1:
                raise ClaimError("intent was claimed concurrently")
        return intent_id

    # -- risk resources & day budgets -------------------------------------

    def set_resource(self, *, account: str, environment: str, currency: str,
                     resource_key: str, amount: Any) -> None:
        """Set the available amount for a scoped risk resource.

        ``amount`` is a decimal string/int/Decimal (never a float).  A missing
        resource row means "unavailable" and makes ``reserve`` fail closed.
        Resources are account/environment scoped: each account owns its own
        cash / shares / obligations.
        """
        _validate_scope(account, environment)
        if not isinstance(currency, str) or not currency.strip():
            raise StateError("currency must be a non-empty string")
        if not isinstance(resource_key, str) or not resource_key.strip():
            raise StateError("resource_key must be a non-empty string")
        text = _decimal_text(amount, "resource amount")
        scope = _scope(account, environment)
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO resources(scope,currency,resource_key,amount) VALUES(?,?,?,?) "
                "ON CONFLICT(scope,currency,resource_key) DO UPDATE SET amount=excluded.amount",
                (scope, currency, resource_key, text))

    def get_resource(self, *, account: str, environment: str,
                     currency: str, resource_key: str) -> Optional[str]:
        """Return the stored decimal amount text, or ``None`` if unset."""
        scope = _scope(account, environment)
        with self._read() as conn:
            row = conn.execute(
                "SELECT amount FROM resources WHERE scope=? AND currency=? AND resource_key=?",
                (scope, currency, resource_key)).fetchone()
            return row["amount"] if row is not None else None

    def list_resources(self, *, account: str, environment: str
                       ) -> Dict[Tuple[str, str], str]:
        """Bulk snapshot of scoped resources as ``{(currency, key): amount}``."""
        scope = _scope(account, environment)
        with self._read() as conn:
            return {(r["currency"], r["resource_key"]): r["amount"] for r in
                    conn.execute("SELECT * FROM resources WHERE scope=?", (scope,))}

    def set_daily_limit(self, *, account: str, environment: str, day_key: str,
                        limit_count: int) -> None:
        """Configure max new-order submissions for a scoped natural day.

        Rejects lowering the limit below slots already consumed (reserved plus
        persistent submitted) on that day.  Use a fresh day key from
        ``day_key_for`` rather than resetting an existing day.
        """
        _validate_scope(account, environment)
        if isinstance(limit_count, bool) or not isinstance(limit_count, int) \
                or limit_count < 0:
            raise StateError("limit_count must be a non-negative integer")
        scope = _scope(account, environment)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM day_budget WHERE scope=? AND day_key=?",
                               (scope, day_key)).fetchone()
            consumed = ((row["reserved_count"] + row["submitted_count"]) if row else 0)
            if limit_count < consumed:
                raise BudgetError(
                    f"cannot lower daily limit below {consumed} already consumed slots")
            conn.execute(
                "INSERT INTO day_budget(scope,day_key,limit_count,reserved_count,"
                "submitted_count,migration_frozen) VALUES(?,?,?,?,?,0) "
                "ON CONFLICT(scope,day_key) DO UPDATE SET "
                "limit_count=excluded.limit_count",
                (scope, day_key, limit_count, row["reserved_count"] if row else 0,
                 row["submitted_count"] if row else 0))

    def get_day_budget(self, *, account: str, environment: str,
                       day_key: str) -> Dict[str, Any]:
        """Return limit / reserved / submitted / remaining counts for a day.

        ``exists`` distinguishes a real persisted row from a MISSING one.  For a
        missing row the counters are reported as ``None`` (unknown), NOT zero:
        a read-only report must never assert a verified zero historic count from
        an empty ledger.  ``remaining`` = max(0, limit - reserved - submitted);
        persistent submitted attempts are never refunded by cancellation or
        rejection.
        """
        scope = _scope(account, environment)
        with self._read() as conn:
            row = conn.execute("SELECT * FROM day_budget WHERE scope=? AND day_key=?",
                               (scope, day_key)).fetchone()
            if row is None:
                return {"exists": False, "limit_count": None, "reserved_count": None,
                        "submitted_count": None, "remaining": None,
                        "migration_frozen": None}
            limit, reserved, submitted = (row["limit_count"],
                                          row["reserved_count"], row["submitted_count"])
            frozen = bool(row["migration_frozen"])
            return {"exists": True, "limit_count": limit, "reserved_count": reserved,
                    "submitted_count": submitted,
                    "remaining": 0 if frozen else max(0, limit - reserved - submitted),
                    "migration_frozen": frozen}

    def prepare_day_budget(self, *, account: str, environment: str,
                           day_key: str, limit_count: int,
                           verified_prior_submissions: Optional[int] = None
                           ) -> Dict[str, Any]:
        """Conservative day-budget initialisation for risk-enabled execution.

        A brand-new / empty ledger row CANNOT be interpreted as "zero historic
        submissions today": earlier tool versions or other processes may have
        already sent orders that this ledger never recorded.  Enabled P3 with
        no *explicitly verified* prior submission count therefore FREEZES new
        risk on the FIRST day this (account, environment) scope is ever seen
        (``limit_count`` is stored but ``migration_frozen`` remains).

        Once THIS SAME scope is genuinely established (a persisted day_budget
        row for this scope) and the clock crosses to a STRICTLY LATER configured
        natural day, that new day may start at a controlled zero: we have been
        tracking the scope for a full day, so zero local submissions today is
        now verifiable.  This is NEVER inferred from a row belonging to another
        scope, a FUTURE/rolled-back day, or by clearing older unresolved risk /
        reservations (those live in their own rows and stay outstanding).

        Pass ``verified_prior_submissions=<operator-verified count>`` to unlock
        a day immediately; it is persisted as a conservative ``submitted_count``
        and never refunded.

        Returns a dict with ``migration_frozen``, ``explanation`` and the
        budget snapshot.  Raises :class:`MigrationFreeze` when frozen, so the
        execution engine can surface the reason and block without an implicit
        user limit.  Disabled P3 callers should instead use ``set_daily_limit``
        with a generous *internal* budget (never a user-facing limit).
        """
        _validate_scope(account, environment)
        if isinstance(limit_count, bool) or not isinstance(limit_count, int) \
                or limit_count < 0:
            raise StateError("limit_count must be a non-negative integer")
        if verified_prior_submissions is not None:
            if isinstance(verified_prior_submissions, bool) \
                    or not isinstance(verified_prior_submissions, int) \
                    or verified_prior_submissions < 0:
                raise StateError("verified_prior_submissions must be a non-negative int")
        scope = _scope(account, environment)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM day_budget WHERE scope=? AND day_key=?",
                               (scope, day_key)).fetchone()
            if row is None:
                # A brand-new (scope, day).  We may grant a CONTROLLED zero
                # start ONLY when THIS SAME scope was already genuinely
                # established on a STRICTLY EARLIER configured natural day.
                # Evidence = a real persisted day_budget row for this scope
                # whose day_key < the requested day_key.  A row for another
                # scope, or a FUTURE/rolled-back day, is NOT proof.
                established = conn.execute(
                    "SELECT day_key FROM day_budget WHERE scope=? AND day_key<? "
                    "ORDER BY day_key DESC LIMIT 1", (scope, day_key)).fetchone()
                controlled_zero = established is not None
                imported = verified_prior_submissions or 0
                has_verified = verified_prior_submissions is not None
                if has_verified or controlled_zero:
                    # Explicit verified import, OR a genuinely later natural day
                    # of an already-established scope: a fresh day legitimately
                    # starts at zero local submissions.  Old unresolved risk /
                    # reservations are NEVER cleared (they live in their own
                    # rows and remain outstanding).
                    conn.execute(
                        "INSERT INTO day_budget(scope,day_key,limit_count,reserved_count,"
                        "submitted_count,migration_frozen) VALUES(?,?,?,0,?,0)",
                        (scope, day_key, limit_count, imported))
                    frozen = False
                else:
                    # FIRST day this scope has ever been seen: cannot prove zero
                    # historic submissions.  Store the limit but FREEZE until a
                    # later natural day or an explicit verified import.
                    conn.execute(
                        "INSERT INTO day_budget(scope,day_key,limit_count,reserved_count,"
                        "submitted_count,migration_frozen) VALUES(?,?,?,0,?,1)",
                        (scope, day_key, limit_count, imported))
                    frozen = True
            else:
                frozen = False  # an established day with real ledger history
                if verified_prior_submissions is not None:
                    # Conservatively keep the LARGER of stored / imported counts,
                    # and un-freeze only with an explicit verified import.
                    conn.execute(
                        "UPDATE day_budget SET limit_count=?, submitted_count=MAX(submitted_count,?), "
                        "migration_frozen=0 WHERE scope=? AND day_key=?",
                        (limit_count, verified_prior_submissions, scope, day_key))
                elif row["migration_frozen"]:
                    # A frozen day stays frozen until the next configured
                    # natural day (a different day_key) or a verified import.
                    conn.execute(
                        "UPDATE day_budget SET limit_count=? WHERE scope=? AND day_key=?",
                        (limit_count, scope, day_key))
                    frozen = True
                else:
                    conn.execute(
                        "UPDATE day_budget SET limit_count=? WHERE scope=? AND day_key=?",
                        (limit_count, scope, day_key))
        budget = self.get_day_budget(account=account, environment=environment,
                                     day_key=day_key)
        explanation = (
            "Conservative migration freeze: a fresh ledger cannot prove zero "
            "historic submissions for this configured natural day. New risk is "
            "blocked until the next configured natural day or until an explicit "
            "operator-verified local submission count is imported."
            if frozen else
            "Day budget established with ledger history / verified prior count."
        )
        result = {"migration_frozen": frozen, "explanation": explanation,
                  "budget": budget}
        if frozen:
            raise MigrationFreeze(explanation)
        return result

    def reserve(self, *, account: str, environment: str, intent_id: str,
                day_key: str, items: Sequence[RiskReservationSpec],
                kind: str = "new_risk", day_submission_slots: int = 0,
                verification_evidence: Optional[Mapping[str, Any]] = None,
                now: Optional[datetime] = None) -> str:
        """Atomically reserve risk resources AND daily submission slots.

        Recomputes total outstanding reserved amounts from all active
        reservations (including cross-day ``unknown`` ones) plus this request
        and refuses if any requested amount exceeds the stored available
        resource, using exact Decimal arithmetic.  ``day_submission_slots``
        must cover every leg to be sent (e.g. 2 for a covered call).  Returns
        the new ``reservation_id``.

        Trust Boundary Note:
        Direct callers of this method claiming ``kind="risk_reduction"`` must
        reference an intent approved for risk reduction (or candidate risk reduction)
        and, when provided, valid verification evidence. In-process Python objects
        do not form a security boundary against arbitrary local code running in
        the same process address space; this API enforces state invariants and
        schema integrity for bounded callers.
        """
        _validate_scope(account, environment)
        if not isinstance(intent_id, str) or not intent_id.strip():
            raise StateError("intent_id must be non-empty")
        if not isinstance(day_key, str) or not day_key.strip():
            raise StateError("day_key must be non-empty")
        if isinstance(day_submission_slots, bool) or not isinstance(day_submission_slots, int) \
                or day_submission_slots < 0:
            raise StateError("day_submission_slots must be a non-negative integer")
        if not isinstance(kind, str) or not kind.strip():
            raise StateError("kind must be a non-empty string")
        now = _aware(now, "now")
        scope = _scope(account, environment)
        reservation_id = _new_id("res")
        created_at = _iso(now)
        normalised: Dict[Tuple[str, str], Decimal] = {}
        for item in items:
            if not isinstance(item, RiskReservationSpec):
                raise StateError("items must be RiskReservationSpec values")
            key = item.key()
            amount = Decimal(_decimal_text(item.amount, "reservation amount"))
            if amount < 0:
                raise StateError("reservation amounts must be non-negative")
            normalised[key] = normalised.get(key, Decimal(0)) + amount
        with self._transaction() as conn:
            intent = conn.execute(
                "SELECT state, payload FROM intents WHERE intent_id=? AND scope=?",
                (intent_id, scope)).fetchone()
            if intent is None:
                raise ClaimError("cannot reserve for an intent outside this scope")
            if intent["state"] not in ("claimed", "reserved", "submitting"):
                raise ClaimError(f"intent state {intent['state']} is not reservable")
            if conn.execute(
                    "SELECT 1 FROM reservations WHERE intent_id=? AND scope=? "
                    "AND state IN ('reserved','submitting','unknown')",
                    (intent_id, scope)).fetchone() is not None:
                raise BudgetError("intent already has an outstanding reservation")

            # Check approved audited risk-reduction exception
            is_risk_reduction = (kind == "risk_reduction")
            if is_risk_reduction:
                try:
                    payload = json.loads(intent["payload"])
                    approved_kind = payload.get("kind")
                except (ValueError, TypeError, KeyError):
                    approved_kind = None
                if approved_kind not in ("risk_reduction", "candidate_risk_reduction"):
                    raise ClaimError("cannot claim risk-reduction quota exception for an intent not approved for risk_reduction")
                if verification_evidence is not None:
                    if not isinstance(verification_evidence, Mapping) or not verification_evidence.get("verified_reduction", False):
                        raise ClaimError("cannot claim risk-reduction quota exception without validated verification evidence")

            # Day-budget reservation (atomic across processes).
            budget = conn.execute("SELECT * FROM day_budget WHERE scope=? AND day_key=?",
                                  (scope, day_key)).fetchone()
            limit = budget["limit_count"] if budget else 0
            consumed = ((budget["reserved_count"] + budget["submitted_count"])
                        if budget else 0)

            if not is_risk_reduction:
                if budget is not None:
                    if budget["migration_frozen"]:
                        raise MigrationFreeze(
                            "Conservative migration freeze: this configured natural day "
                            "has no verified prior submission count; new risk is blocked "
                            "until the next configured natural day or an explicit "
                            "operator-verified local submission count is imported.")
                else:
                    established = conn.execute(
                        "SELECT day_key FROM day_budget WHERE scope=? AND day_key<? "
                        "ORDER BY day_key DESC LIMIT 1", (scope, day_key)).fetchone()
                    if established is None:
                        raise MigrationFreeze(
                            "Conservative migration freeze: a fresh ledger cannot prove zero "
                            "historic submissions for this configured natural day. New risk is "
                            "blocked until the next configured natural day or until an explicit "
                            "operator-verified local submission count is imported.")
                if day_submission_slots > max(0, limit - consumed):
                    raise BudgetError("daily submission budget exhausted")

            if budget is None:
                established = conn.execute(
                    "SELECT day_key FROM day_budget WHERE scope=? AND day_key<? "
                    "ORDER BY day_key DESC LIMIT 1", (scope, day_key)).fetchone()
                controlled_zero = established is not None
                conn.execute(
                    "INSERT INTO day_budget(scope,day_key,limit_count,reserved_count,submitted_count,migration_frozen) "
                    "VALUES(?,?,?,?,0,?)",
                    (scope, day_key, limit, day_submission_slots, 0 if controlled_zero else 1))
            else:
                conn.execute(
                    "UPDATE day_budget SET reserved_count=reserved_count+? "
                    "WHERE scope=? AND day_key=?",
                    (day_submission_slots, scope, day_key))
            # Risk-resource reservation: remaining = available - sum(outstanding).
            placeholders = ",".join("?" for _ in _RISK_OUTSTANDING_STATES)
            for (currency, resource_key), requested in normalised.items():
                row = conn.execute(
                    "SELECT amount FROM resources WHERE scope=? AND currency=? AND resource_key=?",
                    (scope, currency, resource_key)).fetchone()
                if row is None:
                    raise BudgetError(
                        f"risk resource {currency}:{resource_key} is not configured")
                available = Decimal(row["amount"])
                active = Decimal(0)
                for active_row in conn.execute(
                        "SELECT ri.amount FROM reservation_items ri "
                        "JOIN reservations r ON r.reservation_id=ri.reservation_id "
                        "AND r.scope=ri.scope WHERE ri.scope=? AND ri.currency=? "
                        f"AND ri.resource_key=? AND r.state IN ({placeholders})",
                        (scope, currency, resource_key, *_RISK_OUTSTANDING_STATES)):
                    active += Decimal(active_row["amount"])
                if available < active + requested:
                    raise BudgetError(
                        f"insufficient {currency}:{resource_key}: available {available}, "
                        f"outstanding {active}, requested {requested}")
            conn.execute(
                "INSERT INTO reservations(reservation_id,intent_id,scope,day_key,kind,"
                "state,created_at,day_submission_slots) VALUES(?,?,?,?,?,?,?,?)",
                (reservation_id, intent_id, scope, day_key, kind, "reserved",
                 created_at, day_submission_slots))
            for (currency, resource_key), amount in normalised.items():
                conn.execute(
                    "INSERT INTO reservation_items(reservation_id,scope,currency,"
                    "resource_key,amount) VALUES(?,?,?,?,?)",
                    (reservation_id, scope, currency, resource_key, str(amount)))
            conn.execute("UPDATE intents SET state='reserved' WHERE intent_id=? AND state='claimed'",
                         (intent_id,))
        return reservation_id

    def outstanding_reservations(self, *, account: str, environment: str,
                                 states: Optional[Iterable[str]] = None
                                 ) -> List[ReservationView]:
        """Expose outstanding reservations for risk-coordinator integration.

        Defaults to ``reserved`` / ``submitting`` / ``unknown`` (all still
        occupying local risk).  Pass an explicit state list to inspect other
        states.  Account/environment scoped and ordered by creation time.
        """
        _validate_scope(account, environment)
        wanted = tuple(states) if states is not None else tuple(sorted(_RISK_OUTSTANDING_STATES))
        if not wanted:
            return []
        scope = _scope(account, environment)
        placeholders = ",".join("?" for _ in wanted)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM reservations WHERE scope=? AND state IN ({placeholders}) "
                "ORDER BY created_at", (scope, *wanted)).fetchall()
            views = []
            for row in rows:
                items = conn.execute(
                    "SELECT * FROM reservation_items WHERE reservation_id=? AND scope=? "
                    "ORDER BY currency,resource_key",
                    (row["reservation_id"], scope)).fetchall()
                views.append(_reservation_view(row, items))
            return views

    def get_reservation(self, reservation_id: str) -> Optional[ReservationView]:
        """Return a single reservation view by id, or ``None``."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (reservation_id,)).fetchone()
            if row is None:
                return None
            items = conn.execute(
                "SELECT * FROM reservation_items WHERE reservation_id=? AND scope=?",
                (reservation_id, row["scope"])).fetchall()
            return _reservation_view(row, items)

    def _reservation_state(self, conn: sqlite3.Connection,
                           reservation_id: str,
                           leg_states: Sequence[str]) -> str:
        """Derive the reservation state from its per-leg states.

        A reservation stays outstanding (``submitting`` / ``submitted`` /
        ``unknown``) while ANY leg is unresolved.  Only when every leg is
        terminal does it become ``confirmed`` (all positively mapped),
        ``rejected`` (all rejected/skipped), ``released`` or ``aborted``.
        ``unknown`` dominates: it is never downgraded automatically.
        """
        states = set(leg_states)
        if states & {"unknown"}:
            return "unknown"
        if states & {"submitting", "submitted"}:
            return "submitted" if "submitted" in states else "submitting"
        if states and states <= {"confirmed"}:
            return "confirmed"
        if states and states <= {"rejected", "skipped", "confirmed"}:
            # rejected / skipped legs (or a mix with confirmed) mean this
            # reservation no longer occupies risk.
            return "rejected"
        return "reserved"

    def _refresh_reservation(self, conn: sqlite3.Connection,
                             reservation_id: str, stamp: str,
                             terminal_at: bool = False) -> str:
        """Recompute reservation + intent state after a per-leg transition."""
        rows = conn.execute(
            "SELECT state FROM legs WHERE reservation_id=? ORDER BY leg_index",
            (reservation_id,)).fetchall()
        leg_states = [r["state"] for r in rows]
        row = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                           (reservation_id,)).fetchone()
        derived = self._reservation_state(conn, reservation_id, leg_states)
        conn.execute("UPDATE reservations SET state=? WHERE reservation_id=?",
                     (derived, reservation_id))
        # The intent is consumed as soon as the first leg is attempted; its
        # terminal state mirrors the derived reservation state.
        if leg_states:
            conn.execute("UPDATE intents SET state=?, consumed=1 WHERE intent_id=?",
                         (derived, row["intent_id"]))
        if terminal_at and derived not in _UNRESOLVED_STATES:
            conn.execute("UPDATE intents SET terminal_at=? WHERE intent_id=?",
                         (stamp, row["intent_id"]))
        return derived

    def begin_leg_attempt(self, order_ref: str, *,
                          now: Optional[datetime] = None) -> Dict[str, Any]:
        """Durable pre-send attempt for ONE leg — call IMMEDIATELY BEFORE
        ``placeOrder`` for that leg.

        Atomically: marks the leg ``submitting``, converts ONE reserved day
        slot into the persistent ``submitted_count`` (a may-have-sent attempt
        is never refunded), marks the reservation/intent unresolved, and
        returns the leg's ``intent_id`` / ``reservation_id`` / ``leg_index``.
        Re-attempting an already-attempted leg raises (no double count, no
        replay).  Only a leg still ``reserved`` can be attempted.
        """
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            leg = conn.execute("SELECT * FROM legs WHERE order_ref=?",
                               (order_ref,)).fetchone()
            if leg is None:
                raise StateError("unknown orderRef")
            if leg["state"] != "reserved":
                raise StateError(f"leg {order_ref!r} already attempted (state="
                                 f"{leg['state']})")
            res = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (leg["reservation_id"],)).fetchone()
            # A multi-leg sequential send legitimately has a reservation that
            # is already "submitted" (leg 0 positively mapped to a callback,
            # leg 1 still "reserved").  The per-leg state check above is the
            # real replay guard; here we only refuse TERMINAL / unresolved
            # reservation states (aborted, rejected, confirmed, released,
            # unknown).  "submitted" stays outstanding and sendable for the
            # remaining never-attempted legs.
            if res is None or res["state"] not in (
                    "reserved", "submitting", "submitted"):
                raise StateError("reservation is not in a sendable state")
            conn.execute(
                "UPDATE legs SET state='submitting', submitted_at=?, updated_at=? "
                "WHERE order_ref=?", (stamp, stamp, order_ref))
            # ONE attempt consumes ONE reserved slot into the persistent count.
            conn.execute(
                "UPDATE day_budget SET reserved_count=MAX(0,reserved_count-1), "
                "submitted_count=submitted_count+1 WHERE scope=? AND day_key=?",
                (res["scope"], res["day_key"]))
            self._refresh_reservation(conn, leg["reservation_id"], stamp)
            return {"order_ref": order_ref, "intent_id": leg["intent_id"],
                    "reservation_id": leg["reservation_id"],
                    "leg_index": leg["leg_index"], "submitted_at": stamp}

    def record_leg_result(self, order_ref: str, *, state: str,
                          now: Optional[datetime] = None) -> str:
        """Record ONE leg's post-send outcome (``submitted`` / ``rejected`` /
        ``unknown``).  Only a leg currently ``submitting`` may be finalised.
        ``unknown`` is terminal and keeps retaining risk; it is never
        auto-released or auto-retried.  Returns the derived reservation state.
        """
        if state not in ("submitted", "rejected", "unknown"):
            raise StateError(f"invalid leg result state: {state!r}")
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            leg = conn.execute("SELECT * FROM legs WHERE order_ref=?",
                               (order_ref,)).fetchone()
            if leg is None:
                raise StateError("unknown orderRef")
            if leg["state"] != "submitting":
                raise StateError(f"leg {order_ref!r} is not submitting (state="
                                 f"{leg['state']})")
            conn.execute("UPDATE legs SET state=?, updated_at=? WHERE order_ref=?",
                         (state, stamp, order_ref))
            return self._refresh_reservation(conn, leg["reservation_id"], stamp,
                                             terminal_at=True)

    def confirm_leg(self, order_ref: str, *, broker_status: str,
                    perm_id: Any = None, order_id: Any = None,
                    now: Optional[datetime] = None) -> str:
        """Positively map ONE leg to broker state and release its risk.

        Requires an explicit POSITIVE broker status (e.g. ``Filled``,
        ``Cancelled``, ``Submitted``); an ``absent_open_order`` or anything in
        :data:`POSITIVE_BROKER_STATES` complement is rejected — absence from
        open orders is never proof that the broker saw the order.  Only a leg
        in ``submitted``/``submitting``/``unknown`` may be confirmed.  Returns
        the derived reservation state.
        """
        if not isinstance(broker_status, str) or broker_status not in POSITIVE_BROKER_STATES:
            raise StateError(
                f"confirm_leg requires a positive broker status, got {broker_status!r}")
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            leg = conn.execute("SELECT * FROM legs WHERE order_ref=?",
                               (order_ref,)).fetchone()
            if leg is None:
                raise StateError("unknown orderRef")
            if leg["state"] not in ("submitted", "submitting", "unknown"):
                raise StateError(f"leg {order_ref!r} cannot be confirmed (state="
                                 f"{leg['state']})")
            self._bind_ids(conn, order_ref, order_id, perm_id, stamp)
            conn.execute("UPDATE legs SET state='confirmed', updated_at=? "
                         "WHERE order_ref=?", (stamp, order_ref))
            # Record WHICH broker status justified the release.  The leg state
            # alone ("confirmed") loses that: a later audit/report cannot tell
            # a Submitted handoff from a Filled one, and #133/#140 were exactly
            # that case.  Same idempotency rule as :meth:`reconcile`.
            ref = leg["order_ref"]
            perm_text = None if perm_id is None else str(perm_id)
            order_text = None if order_id is None else str(order_id)
            note = "confirm_leg positive broker handoff"
            existing = conn.execute(
                "SELECT observation_id FROM reconciliation WHERE order_ref=? AND "
                "COALESCE(perm_id,'')=COALESCE(?,'') AND COALESCE(order_id,'')=COALESCE(?,'') "
                "AND status='confirmed' AND COALESCE(broker_status,'')=COALESCE(?,'') "
                "AND note=?", (ref, perm_text, order_text, broker_status, note)).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO reconciliation(observation_id,intent_id,order_ref,"
                    "perm_id,order_id,status,broker_status,note,observed_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (_new_id("obs"), leg["intent_id"], ref, perm_text, order_text,
                     "confirmed", broker_status, note, stamp))
            return self._refresh_reservation(conn, leg["reservation_id"], stamp,
                                             terminal_at=True)

    def skip_unsent_legs(self, reservation_id: str, *,
                         now: Optional[datetime] = None) -> int:
        """Mark every never-attempted (``reserved``) leg as ``skipped`` and
        refund their unspent reserved day slots.  Call after a partial send
        (e.g. leg 0 succeeded but the process stopped before leg 1).  Legs
        that were already attempted are never touched.  Returns the number of
        skipped legs.
        """
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            res = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (reservation_id,)).fetchone()
            if res is None:
                raise StateError("unknown reservation")
            pending = conn.execute(
                "SELECT order_ref FROM legs WHERE reservation_id=? AND state='reserved'",
                (reservation_id,)).fetchall()
            for row in pending:
                conn.execute("UPDATE legs SET state='skipped', updated_at=? "
                             "WHERE order_ref=?", (stamp, row["order_ref"]))
            if pending:
                conn.execute(
                    "UPDATE day_budget SET reserved_count=MAX(0,reserved_count-?) "
                    "WHERE scope=? AND day_key=?",
                    (len(pending), res["scope"], res["day_key"]))
                self._refresh_reservation(conn, reservation_id, stamp,
                                          terminal_at=True)
            return len(pending)

    def abort_reservation(self, reservation_id: str, *,
                          now: Optional[datetime] = None) -> str:
        """Explicitly release a *pre-send* reservation (aborted before ANY
        ``placeOrder``).  Refuses if any leg has already been attempted,
        refunds all reserved slots + risk, marks legs ``skipped`` and restores
        the intent to ``claimed``.  Never call after a send may have occurred.
        """
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            res = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (reservation_id,)).fetchone()
            if res is None:
                raise StateError("unknown reservation")
            if res["state"] != "reserved":
                raise StateError("abort_reservation only allowed on a reserved reservation")
            if conn.execute(
                    "SELECT 1 FROM legs WHERE reservation_id=? AND state NOT IN "
                    "('reserved','skipped') LIMIT 1",
                    (reservation_id,)).fetchone() is not None:
                raise StateError("cannot abort: at least one leg was attempted")
            pending = conn.execute(
                "SELECT COUNT(*) FROM legs WHERE reservation_id=? AND state='reserved'",
                (reservation_id,)).fetchone()[0]
            # Refund exactly the still-reserved slots.  Normally this equals
            # len(pending legs); when no legs were ever recorded the whole
            # pre-reserved day_submission_slots must still be refunded.
            refund = pending if pending else res["day_submission_slots"]
            conn.execute("UPDATE legs SET state='skipped', updated_at=? "
                         "WHERE reservation_id=? AND state='reserved'",
                         (stamp, reservation_id))
            if refund:
                conn.execute(
                    "UPDATE day_budget SET reserved_count=MAX(0,reserved_count-?) "
                    "WHERE scope=? AND day_key=?",
                    (refund, res["scope"], res["day_key"]))
            conn.execute("UPDATE reservations SET state='aborted' WHERE reservation_id=?",
                         (reservation_id,))
            conn.execute("UPDATE intents SET state='claimed', terminal_at=? "
                         "WHERE intent_id=?", (stamp, res["intent_id"]))
        return "aborted"

    def resolve_unknown(self, reservation_id: str, *, state: str,
                        now: Optional[datetime] = None) -> str:
        """Explicit, operator-driven resolution of unresolved legs.

        ``state`` is ``confirmed`` (positive snapshot handoff: all unresolved
        legs are confirmed and risk released) or ``released`` (proved the
        orders never reached the broker).  Never called automatically; the
        persistent day submission count is retained.  This is the only path
        that can release an ``unknown`` risk reservation.
        """
        if state not in ("confirmed", "released"):
            raise StateError("resolve_unknown state must be 'confirmed' or 'released'")
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            res = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (reservation_id,)).fetchone()
            if res is None:
                raise StateError("unknown reservation")
            if res["state"] not in _UNRESOLVED_STATES:
                raise StateError("reservation has no unresolved submissions")
            conn.execute(
                "UPDATE legs SET state=?, updated_at=? WHERE reservation_id=? "
                "AND state IN ('submitted','submitting','unknown')",
                (state, stamp, reservation_id))
            conn.execute("UPDATE reservations SET state=? WHERE reservation_id=?",
                         (state, reservation_id))
            conn.execute("UPDATE intents SET state=?, terminal_at=? WHERE intent_id=?",
                         (state, stamp, res["intent_id"]))
        return state

    def has_unresolved_submissions(self, *, account: str,
                                   environment: str) -> bool:
        """True if ANY reservation for this scope is unresolved (submitting /
        submitted / unknown).  The execution engine can use this to
        conservatively block the WHOLE account from new risk instead of
        speculatively releasing risk that may still be live at the broker.
        """
        scope = _scope(account, environment)
        with self._read() as conn:
            ph = ",".join("?" for _ in _UNRESOLVED_STATES)
            return conn.execute(
                f"SELECT 1 FROM reservations WHERE scope=? AND state IN ({ph}) "
                "LIMIT 1", (scope, *_UNRESOLVED_STATES)).fetchone() is not None

    # -- backward-compatible bulk API (single-leg only) --------------------

    def begin_submission(self, reservation_id: str, *,
                         now: Optional[datetime] = None) -> str:
        """Legacy bulk begin: ONLY for reservations with exactly one leg.

        Multi-leg callers MUST use :meth:`begin_leg_attempt` per leg at the
        network boundary so that actual (not total-upfront) attempts are
        counted.  This wrapper routes to the identical per-leg semantics.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT order_ref FROM legs WHERE reservation_id=?",
                (reservation_id,)).fetchall()
        if len(rows) != 1 or rows[0]["order_ref"] is None:
            raise StateError("begin_submission requires exactly one recorded leg; "
                             "use begin_leg_attempt per leg")
        self.begin_leg_attempt(rows[0]["order_ref"], now=now)
        return "submitting"

    def record_submission(self, reservation_id: str, *, state: str,
                          now: Optional[datetime] = None) -> str:
        """Legacy bulk result: ONLY for reservations with exactly one leg.

        ``submitted`` here does NOT release risk (use :meth:`confirm_leg` with
        a positive broker status, or :meth:`resolve_unknown`).  Routes to the
        identical per-leg semantics.
        """
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT order_ref FROM legs WHERE reservation_id=?",
                (reservation_id,)).fetchall()
        if len(rows) != 1:
            raise StateError("record_submission requires exactly one recorded leg; "
                             "use record_leg_result per leg")
        return self.record_leg_result(rows[0]["order_ref"], state=state, now=now)

    # -- per-leg order references -----------------------------------------

    def record_legs(self, *, intent_id: str, reservation_id: str,
                    legs: Sequence[Mapping[str, Any]],
                    now: Optional[datetime] = None) -> List[str]:
        """Persist a stable ``orderRef`` for every leg BEFORE any network call.

        Each leg supplies ``contract_id``, ``action``, ``quantity`` and
        optionally ``order_ref`` (generated when omitted).  The number of legs
        MUST equal the reservation's ``day_submission_slots`` (no 0-slot/1-leg
        bypass).  Legs are bound immutably to this ``reservation_id`` with
        ``UNIQUE(reservation_id, leg_index)``, so an aborted reservation's
        refs cannot be reused as the anchor for a later reservation.  Returns
        the order refs in leg order.
        """
        now = _aware(now, "now")
        stamp = _iso(now)
        if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
            raise StateError("legs must be a sequence of mappings")
        with self._transaction() as conn:
            res = conn.execute("SELECT * FROM reservations WHERE reservation_id=?",
                               (reservation_id,)).fetchone()
            if res is None or res["intent_id"] != intent_id:
                raise StateError("reservation/intent mismatch")
            if res["state"] != "reserved":
                raise StateError("legs can only be recorded on a reserved reservation")
            if len(legs) != res["day_submission_slots"]:
                raise StateError(
                    f"leg count {len(legs)} must exactly match reserved "
                    f"day_submission_slots {res['day_submission_slots']}")
            if conn.execute("SELECT 1 FROM legs WHERE reservation_id=? LIMIT 1",
                            (reservation_id,)).fetchone() is not None:
                raise StateError("legs already recorded for this reservation")
            refs: List[str] = []
            for index, leg in enumerate(legs):
                if not isinstance(leg, Mapping):
                    raise StateError("each leg must be a mapping")
                ref = leg.get("order_ref") or _new_id("ref")
                if not isinstance(ref, str) or not ref.strip():
                    raise StateError("order_ref must be a non-empty string")
                qty = _decimal_text(leg.get("quantity", "0"), "leg quantity")
                try:
                    conn.execute(
                        "INSERT INTO legs(intent_id,reservation_id,order_ref,leg_index,"
                        "contract_id,action,quantity,state,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (intent_id, reservation_id, ref, index,
                         str(leg.get("contract_id", "")),
                         str(leg.get("action", "")), qty, "reserved", stamp, stamp))
                except sqlite3.IntegrityError as exc:
                    raise StateError(f"duplicate orderRef / leg index: {ref!r}") from exc
                refs.append(ref)
            return refs

    def get_leg(self, order_ref: str, *, account: str,
                environment: str) -> Optional[LegView]:
        """Read ONE leg by stable orderRef, STRICTLY account/environment scoped.

        Used by the execution engine's positive-handoff path so a ref belonging
        to a DIFFERENT account/environment can never be used to release this
        scope's reserved risk.  Returns ``None`` for a foreign / unknown ref.
        """
        _validate_scope(account, environment)
        scope = _scope(account, environment)
        with self._read() as conn:
            row = conn.execute(
                "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                "WHERE l.order_ref=? AND i.scope=?", (order_ref, scope)).fetchone()
            return _leg_view(row) if row is not None else None

    def list_scopes(self) -> List[Tuple[str, str]]:
        """Every ``(account, environment)`` scope that owns a leg, sorted.

        Read-only discovery for reporting an audit trail whose account was not
        pre-selected; it derives the scope ONLY from recorded intents, so an
        empty ledger reports nothing rather than a fabricated zero account.
        """
        with self._read() as conn:
            rows = conn.execute(
                "SELECT DISTINCT i.account, i.environment FROM legs l "
                "JOIN intents i ON i.intent_id=l.intent_id "
                "ORDER BY i.account, i.environment").fetchall()
            return [(str(r[0]), str(r[1])) for r in rows]

    def list_legs(self, *, account: str, environment: str,
                  intent_id: Optional[str] = None) -> List[LegView]:
        """List per-leg records (optionally for one intent), account-scoped."""
        scope = _scope(account, environment)
        with self._read() as conn:
            if intent_id is None:
                rows = conn.execute(
                    "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                    "WHERE i.scope=? ORDER BY l.created_at,l.leg_index", (scope,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                    "WHERE i.scope=? AND l.intent_id=? ORDER BY l.leg_index",
                    (scope, intent_id)).fetchall()
            return [_leg_view(r) for r in rows]

    @staticmethod
    def _bind_ids(conn: sqlite3.Connection, order_ref: str,
                  order_id: Any, perm_id: Any, stamp: str) -> None:
        """Attach broker IDs to a leg.  Existing non-empty IDs are IMMUTABLE:
        supplying a conflicting value raises instead of overwriting (a stable
        orderRef/permId anchor is the anti-replay / reconciliation key).
        Re-binding the SAME value is idempotent.
        """
        leg = conn.execute("SELECT * FROM legs WHERE order_ref=?",
                           (order_ref,)).fetchone()
        if leg is None:
            raise StateError("unknown orderRef")
        new_order = None if order_id is None else str(order_id)
        new_perm = None if perm_id is None else str(perm_id)
        if new_order is not None and leg["order_id"] not in (None, "") \
                and leg["order_id"] != new_order:
            raise StateError(f"conflicting orderId for {order_ref!r}: "
                             f"{leg['order_id']} vs {new_order}")
        if new_perm is not None and leg["perm_id"] not in (None, "") \
                and leg["perm_id"] != new_perm:
            raise StateError(f"conflicting permId for {order_ref!r}: "
                             f"{leg['perm_id']} vs {new_perm}")
        conn.execute(
            "UPDATE legs SET order_id=COALESCE(?,order_id), "
            "perm_id=COALESCE(?,perm_id), updated_at=? WHERE order_ref=?",
            (new_order, new_perm, stamp, order_ref))

    def bind_broker_ids(self, order_ref: str, *, order_id: Any = None,
                        perm_id: Any = None, now: Optional[datetime] = None) -> None:
        """Attach known broker ``orderId`` / ``permId`` to a leg (idempotent,
        conflict-rejecting).  Never overwrites an existing non-empty ID.
        """
        now = _aware(now, "now")
        stamp = _iso(now)
        with self._transaction() as conn:
            self._bind_ids(conn, order_ref, order_id, perm_id, stamp)

    # -- legacy codes (read-only import) ----------------------------------

    def import_legacy_codes(self, *, account: str, environment: str,
                            codes: Iterable[str],
                            source: str = "consumed_codes.json",
                            now: Optional[datetime] = None) -> int:
        """Read-only import of consumed codes from legacy JSON/JSONL data.

        Existing records are preserved (``INSERT OR IGNORE``): nothing is
        cleared, rewritten or deleted, so backups/migrations remain friendly.
        Returns the number of *newly* imported codes.  Imported codes make a
        future claim with a matching short code fail closed.
        """
        return self.import_legacy_records(
            account=account, environment=environment,
            records=[{"code": c} for c in codes], source=source, now=now)

    def import_legacy_records(self, *, account: str, environment: str,
                              records: Iterable[Any],
                              source: str = "consumed_codes.json",
                              now: Optional[datetime] = None) -> int:
        """Import already-consumed codes from parsed legacy data.

        ``records`` may be code strings, a dict (keys are codes), mappings with
        a ``code`` key, or raw JSONL lines.  Performs no file I/O and never
        clears history; callers read legacy files themselves and hand parsed
        data in.
        """
        _validate_scope(account, environment)
        now = _aware(now, "now")
        stamp = _iso(now)
        scope = _scope(account, environment)
        codes: List[str] = []
        for record in records:
            if isinstance(record, str):
                raw = record.strip()
                if not raw:
                    continue
                if raw.startswith("{"):
                    try:
                        raw = str(json.loads(raw).get("code", "") or "")
                    except (ValueError, TypeError):
                        continue
                if raw:
                    codes.append(raw)
            elif isinstance(record, Mapping):
                raw = str(record.get("code", "") or "").strip()
                if raw:
                    codes.append(raw)
            elif isinstance(record, (tuple, list, set, frozenset)):
                codes.extend(str(x) for x in record if str(x))
        with self._transaction() as conn:
            before = conn.execute("SELECT COUNT(*) FROM legacy_codes WHERE scope=?",
                                  (scope,)).fetchone()[0]
            for code in codes:
                conn.execute(
                    "INSERT OR IGNORE INTO legacy_codes(code,scope,source,imported_at) "
                    "VALUES(?,?,?,?)", (code, scope, source, stamp))
            after = conn.execute("SELECT COUNT(*) FROM legacy_codes WHERE scope=?",
                                 (scope,)).fetchone()[0]
            return after - before

    def legacy_codes(self, *, account: str, environment: str) -> List[str]:
        """Read-only view of imported legacy consumed codes for a scope."""
        scope = _scope(account, environment)
        with self._read() as conn:
            return [r["code"] for r in conn.execute(
                "SELECT code FROM legacy_codes WHERE scope=? ORDER BY code", (scope,))]

    # -- broker reconciliation (idempotent) -------------------------------

    def reconcile(self, *, account: str, environment: str,
                  order_ref: Optional[str] = None, perm_id: Any = None,
                  order_id: Any = None, status: str = "confirmed",
                  broker_status: Optional[str] = None, note: str = "",
                  now: Optional[datetime] = None) -> Dict[str, Any]:
        """Record an idempotent broker observation against a stable leg key.

        ``status`` is the coordinator's conclusion (e.g. ``confirmed``,
        ``absent_open_order``, ``filled``, ``cancelled``).  An
        ``absent_open_order`` observation is stored but deliberately NOT
        treated as proof that no submission happened: it never releases or
        retries an ``unknown`` leg.  Repeated identical observations (same
        ``orderRef``/``permId``/``orderId`` + status + broker_status + note)
        return ``duplicate=True`` without inserting again.  This method never
        places, cancels or retries an order.
        """
        _validate_scope(account, environment)
        now = _aware(now, "now")
        stamp = _iso(now)
        scope = _scope(account, environment)
        perm_text = None if perm_id is None else str(perm_id)
        order_text = None if order_id is None else str(order_id)
        with self._transaction() as conn:
            leg = None
            if order_ref is not None:
                leg = conn.execute(
                    "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                    "WHERE l.order_ref=? AND i.scope=?", (order_ref, scope)).fetchone()
                if leg is None:
                    raise StateError("unknown orderRef for this scope")
            elif perm_text is not None:
                leg = conn.execute(
                    "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                    "WHERE l.perm_id=? AND i.scope=?", (perm_text, scope)).fetchone()
            elif order_text is not None:
                leg = conn.execute(
                    "SELECT l.* FROM legs l JOIN intents i ON i.intent_id=l.intent_id "
                    "WHERE l.order_id=? AND i.scope=?", (order_text, scope)).fetchone()
            if leg is None:
                raise StateError("no stable orderRef/permId/orderId matched a leg")
            ref, intent_id = leg["order_ref"], leg["intent_id"]
            # A supplied permId/orderId that conflicts with the leg's bound
            # IDs is rejected: never silently rewrite a reconciliation anchor.
            if perm_text is not None and leg["perm_id"] not in (None, "") \
                    and leg["perm_id"] != perm_text:
                raise StateError(f"conflicting permId supplied for {ref!r}: "
                                 f"{leg['perm_id']} vs {perm_text}")
            if order_text is not None and leg["order_id"] not in (None, "") \
                    and leg["order_id"] != order_text:
                raise StateError(f"conflicting orderId supplied for {ref!r}: "
                                 f"{leg['order_id']} vs {order_text}")
            self._bind_ids(conn, ref, order_text, perm_text, stamp)
            existing = conn.execute(
                "SELECT observation_id FROM reconciliation WHERE order_ref=? AND "
                "COALESCE(perm_id,'')=COALESCE(?,'') AND COALESCE(order_id,'')=COALESCE(?,'') "
                "AND status=? AND COALESCE(broker_status,'')=COALESCE(?,'') AND note=?",
                (ref, perm_text, order_text, status, broker_status, note)).fetchone()
            if existing is not None:
                return {"observation_id": existing["observation_id"], "duplicate": True,
                        "intent_id": intent_id, "order_ref": ref}
            observation_id = _new_id("obs")
            conn.execute(
                "INSERT INTO reconciliation(observation_id,intent_id,order_ref,perm_id,"
                "order_id,status,broker_status,note,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (observation_id, intent_id, ref, perm_text, order_text, status,
                 broker_status, note, stamp))
            return {"observation_id": observation_id, "duplicate": False,
                    "intent_id": intent_id, "order_ref": ref}

    def list_reconciliations(self, *, account: str, environment: str,
                             intent_id: Optional[str] = None
                             ) -> List[ReconciliationView]:
        """Read-only reconciliation history (idempotent observations only)."""
        scope = _scope(account, environment)
        with self._read() as conn:
            sql = ("SELECT r.* FROM reconciliation r JOIN intents i "
                   "ON i.intent_id=r.intent_id WHERE i.scope=?")
            params: List[Any] = [scope]
            if intent_id is not None:
                sql += " AND r.intent_id=?"
                params.append(intent_id)
            sql += " ORDER BY r.observed_at"
            return [_recon_view(r) for r in conn.execute(sql, params).fetchall()]


def open_readonly(path: os.PathLike | str, day_timezone: str, *,
                  busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
                  ) -> "SubmissionLedger":
    """Open an EXISTING ledger for query-only / report use.

    Never creates schema, migrates, or touches journal/WAL files: a touched
    empty file stays empty and is rejected as a foreign/unsupported DB.  All
    state-changing methods raise :class:`SchemaError`.  Use this for the CLI
    read-only report; ``reconcile`` (writing observations) requires a normal
    writable :class:`SubmissionLedger` after the schema has been validated.
    """
    return SubmissionLedger(path, day_timezone,
                            busy_timeout_ms=busy_timeout_ms, read_only=True)


__all__ = [
    "SCHEMA_VERSION", "StateError", "SchemaError", "ClaimError", "BudgetError",
    "LockUnavailableError", "MigrationFreeze",
    "RiskReservationSpec", "ReservationView", "LegView",
    "ReconciliationView", "AccountLock", "account_scope", "SubmissionLedger",
    "open_readonly",
    "approval_code", "config_digest", "compute_full_digest", "day_key_for",
    "POSITIVE_BROKER_STATES",
]
