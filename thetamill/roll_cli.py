"""Read-only CLI adapter for Stage-E roll recommendations (recommendation ONLY).

This module is the *only* impure layer around the pure :mod:`thetamill.roll`
engine.  It:

* connects to the broker in **readonly** mode and builds an account-scoped
  immutable :class:`~thetamill.broker_state.Snapshot` (via cli._risk_snapshot);
* qualifies each explicitly supplied option conId through
  ``reqContractDetails(Contract(conId=id))`` and turns the *qualified* contract
  into a real :class:`~thetamill.broker_state.Instrument` using
  :func:`thetamill.broker_state._instrument` (never guesses a chain, symbol,
  multiplier or tradingClass);
* fetches timestamped two-sided quotes with ``reqTickers`` and uses the
  ticker's OWN ``time`` as the quote timestamp (it never stamps ``now`` onto a
  possibly stale bid/ask; a missing/unusable/crossed/future timestamp yields
  ``None`` with a warning, it never crashes the JSON document);
* loads explicit, per-OPTION-conId stock-only delivery evidence (a separate
  versioned ``thetamill.deliverables.v1`` document) via the public
  :func:`load_delivery_evidence` helper, which later execution layers reuse;
* runs the conservative new-open risk gate through :func:`thetamill.risk.assess`
  on the ORIGINAL snapshot with local aggregate reservations from the
  submission ledger, if one exists (no ledger -> no new open, fail closed);
* honors the optional universe whitelist, renders via
  :func:`thetamill.roll.render_roll_recommendation`, and writes with create-only
  :func:`thetamill.report.write_report`.

Nothing here places, cancels or modifies orders, never runs automatically, and
cannot be consumed by ``run`` (the output is ``thetamill.roll.v1``).  All errors
fail closed: a degraded plan is still emitted (with unknown/rejected fields),
never fabricated data or a raw account identifier. Broker stdout/stderr
diagnostics are suppressed so ``--format json`` stays clean without privacy leaks.
"""
from __future__ import annotations

import inspect
import io
import json
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from . import broker_state as bs
from . import roll
from . import universe as uni

# Separate, versioned deliverable-evidence document.  It is NOT a plan, NOT a
# roll recommendation, and carries no order/executable fields.
DELIVERABLES_SCHEMA = "thetamill.deliverables.v1"

# Upper bound (seconds) on one snapshot request.  ib_async's own default of 0
# means "never time out", which turns an unanswered request into a hang.
SNAPSHOT_TIMEOUT_SECONDS = 20.0


class DeliverableEvidenceError(ValueError):
    """Delivery evidence is missing, malformed, stale or contradicts identity."""


@contextmanager
def _private_diagnostics():
    """Never forward arbitrary broker/library output, even on error paths."""
    with io.StringIO() as sink, redirect_stdout(sink), redirect_stderr(sink):
        yield


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
            f"cannot read delivery evidence: {type(exc).__name__}") from exc
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


# --------------------------------------------------------------------------- #
# Broker qualification + timestamped quotes
# --------------------------------------------------------------------------- #

def _qualify_options(ib, con_ids: Sequence[int], notes: list[str]):
    """Return (instruments, contracts_by_id). Only genuinely qualified identities.

    NO chain/symbol guessing: each id is looked up via
    ``reqContractDetails(Contract(conId=id))`` and converted with
    ``broker_state._instrument`` (which validates underConId uniqueness,
    multiplier fidelity and stock-only underlying).

    The contracts returned for use in market-data requests are the
    DETAILS-DERIVED ones, never the ``Contract(conId=...)`` lookup stubs.  IB
    refuses a stub that carries no exchange (error 321, "Please enter
    exchange") and answers with nothing at all, so a snapshot request built
    from stubs never completes and the caller blocks forever.
    """
    from . import cli as _cli  # lazy: avoid a module-level circular import

    instruments: list[bs.Instrument] = []
    contracts: dict[int, object] = {}
    for cid in con_ids:
        if isinstance(cid, bool) or type(cid) is not int or cid <= 0:
            notes.append(f"candidate conId {cid!r} is invalid; not qualified")
            continue
        try:
            contract = _cli.Contract(conId=cid)
            with _private_diagnostics():
                details = ib.reqContractDetails(contract)
        except Exception as exc:
            notes.append(f"contract details unavailable for conId {cid} "
                         f"({type(exc).__name__}); not qualified")
            continue
        if not isinstance(details, (list, tuple)) or not details:
            notes.append(f"no contract details for conId {cid}; not qualified")
            continue
        matching = [d for d in details
                    if getattr(getattr(d, "contract", None), "conId", None) == cid]
        if not matching:
            notes.append(f"contract details did not match conId {cid}; not qualified")
            continue
        qualified = matching[0].contract
        if getattr(qualified, "secType", "") != "OPT":
            notes.append(f"conId {cid} is not an option; not qualified")
            continue
        try:
            inst = bs._instrument(qualified, ib, {cid: matching})
        except Exception as exc:  # fail closed on any defect
            notes.append(f"qualified identity for conId {cid} rejected "
                         f"({type(exc).__name__}); not qualified")
            continue
        if any(x.con_id == cid for x in instruments):
            notes.append(f"duplicate candidate conId {cid}; ignored")
            continue
        # Route the market-data request with the DETAILS contract.  The
        # conId-only stub above has no exchange, which IB answers with error
        # 321 and no reply at all -- a snapshot built from stubs never
        # completes.  A details contract that still lacks a venue is not
        # routable either, so refuse it rather than issue a request that can
        # only block.
        if not getattr(qualified, "exchange", ""):
            notes.append(f"qualified conId {cid} has no exchange; quote request "
                         "not possible; not qualified")
            continue
        instruments.append(inst)
        contracts[cid] = qualified
    return instruments, contracts


def _fetch_tickers(ib, contracts: Mapping[int, object], notes: list[str]):
    """One reqTickers call; returns a raw ``{con_id: ticker}`` map.

    The request is additionally TIGHTENED to ``SNAPSHOT_TIMEOUT_SECONDS``.
    ``cli.connect`` already bounds ``RequestTimeout`` globally (the library
    default of 0 means "wait forever"), but a snapshot is a short, optional
    read: making the caller wait the full general timeout for a quote it can
    live without is the wrong trade.  The bound is only ever lowered, never
    raised, so a caller-configured stricter policy is preserved.
    """
    raw: dict[int, object] = {}
    if not contracts:
        return raw
    previous = getattr(ib, "RequestTimeout", None)
    try:
        if not isinstance(previous, (int, float)) or previous <= 0 \
                or previous > SNAPSHOT_TIMEOUT_SECONDS:
            ib.RequestTimeout = SNAPSHOT_TIMEOUT_SECONDS
    except Exception:      # a strict/frozen stand-in: run with its own policy
        pass
    try:
        with _private_diagnostics():
            tickers = ib.reqTickers(*contracts.values())
    except Exception as exc:
        notes.append(f"market data request failed ({type(exc).__name__}); "
                     "all quotes treated as missing")
        return raw
    finally:
        try:
            ib.RequestTimeout = previous
        except Exception:
            pass
    for t in (tickers or ()):
        cid = getattr(getattr(t, "contract", None), "conId", None)
        if type(cid) is int and cid > 0 and cid not in raw:
            raw[cid] = t
    return raw


def _build_quotes(raw: Mapping[int, object], instruments: Sequence[bs.Instrument],
                  now: datetime, notes: list[str]) -> dict[int, roll.Quote | None]:
    """Build Quote objects using the ticker's OWN time; degrade, never crash."""
    quotes: dict[int, roll.Quote | None] = {}
    for inst in instruments:
        cid = inst.con_id
        t = raw.get(cid)
        if t is None:
            notes.append(f"no timestamped ticker for conId {cid}; quote missing")
            quotes[cid] = None
            continue
        # A usable quote (one that can feed a cash estimate / cost floor) MUST
        # come from the LIVE feed.  ticker.time is only the local reception
        # time and does NOT prove a delayed/frozen price is fresh; types 2/3/4
        # are clearly labelled and excluded rather than passed off as current.
        data_type = getattr(t, "marketDataType", None)
        if data_type != 1:
            notes.append(f"ticker for conId {cid} is DELAYED/FROZEN "
                         f"(marketDataType={data_type!r}); not usable as a "
                         "current cash-estimate quote")
            quotes[cid] = None
            continue
        stamp = getattr(t, "time", None)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None \
                or stamp.utcoffset() is None:
            # NEVER stamp a stale/undated bid/ask with the current wall clock.
            notes.append(f"ticker for conId {cid} has no source timestamp; "
                         "quote missing (not re-stamped)")
            quotes[cid] = None
            continue
        if stamp.astimezone(timezone.utc) > now + roll._CLOCK_SKEW:
            notes.append(f"ticker for conId {cid} is in the future; quote unavailable")
            quotes[cid] = None
            continue

        def price(value, label):
            if value is None:
                return None
            if isinstance(value, bool):
                notes.append(f"{label} for conId {cid} is not a number; missing")
                return None
            try:
                result = Decimal(str(value))
            except (InvalidOperation, ValueError, TypeError):
                notes.append(f"{label} for conId {cid} is not a number; missing")
                return None
            if not result.is_finite() or result < 0:
                notes.append(f"{label} for conId {cid} is unusable; missing")
                return None
            return result

        bid = price(getattr(t, "bid", None), "bid")
        ask = price(getattr(t, "ask", None), "ask")
        if bid is None and ask is None:
            notes.append(f"ticker for conId {cid} has no bid/ask; quote missing")
            quotes[cid] = None
            continue
        if bid is not None and ask is not None and bid > ask:
            # A crossed quote is unusable: an unavailable warning, never a crash.
            notes.append(f"ticker for conId {cid} is crossed (bid {bid} > ask {ask}); "
                         "quote unavailable")
            quotes[cid] = None
            continue
        try:
            quotes[cid] = roll.Quote(cid, bid, ask, stamp, source="reqTickers")
        except Exception as exc:
            # The roll engine may add further quote validation; degrade per-leg.
            notes.append(f"quote for conId {cid} rejected by roll engine "
                         f"({type(exc).__name__}); quote unavailable")
            quotes[cid] = None
    return quotes


# --------------------------------------------------------------------------- #
# Constraints / universe / risk callback
# --------------------------------------------------------------------------- #

def _constraints(args, cfg, symbol: str, doc, universe_requested: bool,
                 notes: list[str]) -> roll.RollConstraints:
    """Build RollConstraints; a whitelist is ONLY ever a subtraction."""
    cost_floor = getattr(args, "cost_floor", None)
    max_entry = getattr(args, "max_entry", None)
    allowed_symbols: frozenset = frozenset()
    blocked = False

    if universe_requested:
        if doc is None:
            notes.append("universe whitelist could not be loaded; all candidates blocked")
            blocked = True
        else:
            allowed = uni.universe_allowed(doc)
            # preset is deliberately not "cash-put": the per-candidate strike
            # ceiling is applied below via max_entry, not against one strike.
            ok, reason = uni.universe_gate(symbol, "", 0.0, allowed)
            if not ok or not allowed:
                notes.append(f"universe whitelist blocked {symbol}: "
                             f"{reason or 'empty eligible set'}")
                blocked = True
            else:
                allowed_symbols = frozenset(allowed)
                item = allowed.get(str(symbol).upper()) or {}
                uni_max = item.get("max_entry")
                if uni_max is not None:
                    try:
                        u = Decimal(str(uni_max))
                        cur = Decimal(str(max_entry)) if max_entry is not None else None
                        max_entry = str(u if cur is None else min(cur, u))
                    except (InvalidOperation, ValueError, TypeError):
                        pass  # RollConstraints validates the raw value below
    if blocked:
        # A non-empty sentinel that no real symbol can match -> every candidate
        # is rejected by the engine with an explicit whitelist reason.
        allowed_symbols = frozenset({"\x00universe-blocked"})

    # Snapshot freshness cap comes from the VALIDATED risk config, when present.
    max_snapshot_age = None
    try:
        from . import risk
        limits = risk.validate_risk_config(cfg)
        if limits.max_snapshot_age_seconds is not None:
            max_snapshot_age = timedelta(seconds=limits.max_snapshot_age_seconds)
    except Exception as exc:
        notes.append(f"risk config invalid ({type(exc).__name__}); "
                     "snapshot freshness cap unavailable")

    kwargs = dict(min_dte=getattr(args, "dte_min", None),
                  max_dte=getattr(args, "dte_max", None),
                  cost_floor=cost_floor, max_entry=max_entry,
                  allowed_symbols=allowed_symbols)
    if max_snapshot_age is not None:
        if "max_snapshot_age" in inspect.signature(roll.RollConstraints).parameters:
            kwargs["max_snapshot_age"] = max_snapshot_age
        else:
            notes.append("roll engine lacks max_snapshot_age support; "
                         "snapshot freshness not enforced by the engine")
    try:
        return roll.RollConstraints(**kwargs)
    except roll.RollError as exc:
        raise ValueError(f"invalid roll constraints: {exc}") from exc


def _risk_callback(snapshot, cfg, args, environment, now, notes):
    """Return the assess(snapshot, cand, qty) closure for the pure roll engine.

    It evaluates the NEW OPEN (sell-to-open) on the ORIGINAL snapshot with the
    local ledger's aggregate reservations.  Without a ledger the new open is
    refused (history is unknown; fail closed).  All exceptions fail closed.
    """
    try:
        from . import risk
        limits = risk.validate_risk_config(cfg)
    except Exception as exc:
        notes.append(f"risk config invalid ({type(exc).__name__}); new opens blocked")
        return lambda snap, cand, qty: (False, "risk config invalid; new open blocked")

    reservations = None
    try:
        from . import cli as _cli
        path = _cli._data_dir() / "state.sqlite3"
        if not path.exists():
            notes.append("Local intent ledger does not exist; submission history unknown; "
                         "new opens blocked")
            return lambda snap, cand, qty: (
                False, "no local submission ledger: reservations/history unknown; "
                       "new open blocked")
        from .state import SubmissionLedger
        from .execution import aggregate_reservations
        ledger = SubmissionLedger(path, limits.day_timezone or "UTC", read_only=True)
        reservations = aggregate_reservations(ledger, snapshot.account, environment)
    except Exception as exc:
        notes.append(f"local reservations unavailable ({type(exc).__name__}); "
                     "new opens blocked")
        return lambda snap, cand, qty: (
            False, "local reservations unavailable; new open blocked")

    def assess(snap, cand, qty):
        try:
            from .planning import validate_snapshot_evidence
            evidence_reasons = validate_snapshot_evidence(
                snap, limits, now, needed_currencies=(cand.currency,))
            if evidence_reasons:
                # Reasons may contain adapter-supplied identifiers. Preserve
                # fail-closed semantics without forwarding arbitrary details.
                return False, "snapshot valuation evidence unconfirmed; new open blocked"
            request = risk.OrderRequest(
                instrument=cand, action="SELL", quantity=Decimal(int(qty)),
                order_type="LMT", limit_price=None, tif="DAY")
            decision = risk.assess(snap, request, limits, reservations, now=now)
            if decision.allowed:
                return True, ""
            return False, "; ".join(decision.reasons)
        except Exception as exc:
            return False, f"risk assessment error ({type(exc).__name__}); blocked"

    return assess


# --------------------------------------------------------------------------- #
# Rendering + privacy-safe diagnostics
# --------------------------------------------------------------------------- #

def _skeleton(now: datetime, con_id) -> dict:
    return {
        "schema": roll.SCHEMA,
        "kind": "roll_recommendation",
        "recommendation_only": True,
        "executable": False,
        "disclaimer": "仅供人工复核的展期建议；非投资建议。本文件不可执行：不会下单/撤单。",
        "account": "***",
        "environment": "unknown",
        "base_currency": "unknown",
        "snapshot_captured_at": None,
        "created_at": now.isoformat(),
        "expires_at": (now + roll.RollConstraints().valid_for).isoformat(),
        "short_con_id": con_id,
        "close_leg": {"action": "BUY", "quantity": None,
                      "instrument": None, "multiplier": None},
        "open_action": "SELL",
        "new_open_quantity": None,
        "risk_assessment": {"basis": "original_snapshot",
                            "old_obligations_released": False},
        "constraints": {},
        "confirmed_deliverables": [],
        "confirmed_deliverables_scope": "option_conIds_per_contract",
        "deliverable_policy": ("stock-only delivery confirmed per OPTION conId; "
                               "never inferred from underlying conId/tradingClass/multiplier"),
        "close_quote": None,
        "fees": None,
        "fees_note": "手续费/佣金未知；净额估算未扣除任何费用。",
        "candidates": [],
        "recommended_count": 0,
        "rejected_count": 0,
        "issues": [],
        "assumptions": [],
    }


def _failure_document(args, notes: list[str], now: datetime) -> dict:
    """A degraded, explicit recommendation document when the engine cannot run."""
    doc = _skeleton(now, getattr(args, "con_id", None))
    doc["issues"] = list(dict.fromkeys(notes)) or ["roll recommendation unavailable"]
    return doc


def _rejected_document(args, old_inst, candidates, notes: list[str], now: datetime,
                       reason: str) -> dict:
    """No per-contract evidence: every candidate is explicitly rejected."""
    doc = _skeleton(now, getattr(args, "con_id", None))
    qty = str(getattr(args, "qty", 0) or 0)
    doc["new_open_quantity"] = qty
    doc["close_leg"]["quantity"] = qty
    if old_inst is not None:
        try:
            doc["close_leg"]["instrument"] = roll.instrument_data(old_inst)
            doc["close_leg"]["multiplier"] = str(old_inst.multiplier)
        except Exception:
            pass
    rows = []
    for cand in candidates:
        try:
            inst_data = roll.instrument_data(cand)
        except Exception:
            inst_data = None
        rows.append({
            "con_id": cand.con_id, "symbol": cand.symbol, "expiry": cand.expiry,
            "status": "rejected", "reasons": [reason],
            "instrument": inst_data, "quantity": qty,
            "multiplier": str(cand.multiplier) if inst_data else None,
            "days_to_expiry": None, "quote": None, "net_estimate": None,
        })
    doc["candidates"] = rows
    doc["rejected_count"] = len(rows)
    doc["issues"] = list(dict.fromkeys(notes + [reason]))
    return doc


def _render(doc: Mapping, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(dict(doc), ensure_ascii=False, indent=2,
                          allow_nan=False, default=str) + "\n"
    try:
        return roll.render_roll_recommendation(doc, fmt)
    except Exception:
        return json.dumps(dict(doc), ensure_ascii=False, indent=2,
                          allow_nan=False, default=str) + "\n"


# --------------------------------------------------------------------------- #
# Main entry point (called by cli.cmd_roll_plan)
# --------------------------------------------------------------------------- #

def cmd_roll_plan(cfg, args, now: datetime | None = None) -> int:
    """Build a read-only, recommendation-only roll plan.  Never executes orders.

    ``now`` is an optional injectable aware UTC clock (used by deterministic
    tests); production callers pass only ``(cfg, args)`` and the wall clock is
    used.  A snapshot captured at ``now`` with a real quote timestamp slightly
    in the past passes the engine's freshness/quote-age validation; an injected
    clock is never used to fabricate a quote timestamp.
    """
    from . import cli as _cli  # lazy import avoids a circular import with cli.py

    fmt = getattr(args, "format", "terminal") or "terminal"
    if fmt not in {"terminal", "json", "markdown"}:
        fmt = "terminal"
    injected_now = now
    if now is None or not isinstance(now, datetime) or now.tzinfo is None \
            or now.utcoffset() is None:
        injected_now = None
        now = datetime.now(timezone.utc)
    notes: list[str] = []
    ib = None
    snapshot = None
    doc = None
    environment = "paper"

    try:
        port, _ = _cli.resolve_port(cfg, args)
        environment = "live" if _cli._is_live_env(cfg, args, port) else "paper"
        with _private_diagnostics():
            ib = _cli.connect(cfg, port, readonly=True, args=args)
            # A supported roll must keep the held short's currency, already
            # collected from its position by _risk_snapshot. Cross-currency
            # candidates are rejected by the pure engine before this risk gate;
            # qualifying candidates does not require a second account refresh.
            snapshot = _cli._risk_snapshot(ib, cfg, args, environment)
        if snapshot is None:
            raise ValueError("account snapshot unavailable")

        con_id = getattr(args, "con_id", None)
        qty = getattr(args, "qty", None)
        if isinstance(con_id, bool) or type(con_id) is not int or con_id <= 0:
            raise ValueError("con_id must be a positive integer")
        if isinstance(qty, bool) or type(qty) is not int or qty <= 0:
            raise ValueError("qty must be a positive integer")
        candidate_ids = list(getattr(args, "candidate_con_id", None) or [])
        all_ids = [con_id] + candidate_ids

        # Old short + every candidate must be genuinely qualified; no guessing.
        qualified, contracts = _qualify_options(ib, all_ids, notes)
        raw_tickers = _fetch_tickers(ib, contracts, notes)
        if injected_now is None:
            now = datetime.now(timezone.utc)  # AFTER broker calls: no negative age
        quotes = _build_quotes(raw_tickers, qualified, now, notes)
        by_con_id = {inst.con_id: inst for inst in qualified}
        old_inst = by_con_id.get(con_id)

        universe_requested = bool(getattr(args, "universe", None))
        universe_doc = None
        if universe_requested:
            try:
                with _private_diagnostics():
                    universe_doc = uni.load_universe(args.universe)
            except (SystemExit, Exception) as exc:
                notes.append(f"universe whitelist rejected ({type(exc).__name__}); "
                             "no whitelist applied")

        # Explicit per-OPTION-conId evidence. Missing/unreadable -> empty set.
        evidence_path = getattr(args, "delivery_evidence", None)
        try:
            confirmed = (load_delivery_evidence(evidence_path, qualified, now)
                         if evidence_path else MappingProxyType({}))
        except DeliverableEvidenceError as exc:
            notes.append(f"delivery evidence rejected: {exc}; no contract authorized")
            confirmed = MappingProxyType({})
        if not confirmed:
            notes.append("No stock-only delivery evidence supplied; "
                         "per-OPTION conId confirmations are empty")
        confirmed_ids = frozenset(confirmed)

        candidates = [by_con_id[cid] for cid in dict.fromkeys(candidate_ids)
                      if cid in by_con_id and cid != con_id]

        if old_inst is None:
            raise ValueError(f"existing short conId {con_id} could not be qualified")
        # Evidence authorizes the QUALIFIED identity, not every snapshot row
        # bearing its conId. Validate the economic terms actually being closed
        # before reducing evidence to a set of IDs for the pure engine.
        for row in (*snapshot.positions, *snapshot.orders):
            observed = row.instrument
            if observed.con_id != con_id:
                continue
            expected = _identity_for(old_inst)
            actual = _identity_for(observed)
            expected.pop("exchange")  # routing is not an economic field
            actual.pop("exchange")
            if any((_finite(actual[key], key) != _finite(value, key)
                    if key in {"multiplier", "strike"} else actual[key] != value)
                   for key, value in expected.items()):
                raise ValueError("qualified old identity conflicts with snapshot")
        constraints = _constraints(args, cfg, old_inst.symbol, universe_doc,
                                   universe_requested, notes)
        assess = _risk_callback(snapshot, cfg, args, environment, now, notes)

        if con_id not in confirmed_ids:
            # The pure engine raises when the close leg lacks per-contract
            # evidence.  Produce an explicit all-rejected document instead of a
            # top-level failure: candidates are rejected WITH a reason.
            reason = ("deliverable not confirmed for this option conId: stock-only "
                      "per-CONTRACT evidence required (never inferred from underlying "
                      "conId/tradingClass/multiplier)")
            doc = _rejected_document(args, old_inst, candidates, notes, now, reason)
        else:
            doc = roll.build_roll_recommendation(
                snapshot, con_id=con_id, quantity=qty, candidates=candidates,
                quotes=quotes, constraints=constraints,
                confirmed_deliverables=confirmed_ids, assess=assess, now=now)
        if isinstance(doc, dict):
            extra = [n for n in notes if n not in doc.get("issues", ())]
            doc["issues"] = list(doc.get("issues", ())) + extra
    except (Exception, SystemExit) as exc:
        # Broker/account failures can contain raw account IDs; never echo them.
        notes.append(f"roll recommendation incomplete ({type(exc).__name__}); "
                     "broker details withheld for privacy")
    finally:
        if ib is not None:
            try:
                with _private_diagnostics():
                    _cli.finish(ib)
            except Exception:
                pass

    if doc is None:
        doc = _failure_document(args, notes, now)
    text = _render(doc, fmt)
    output = getattr(args, "output", None)
    if output:
        try:
            from .report import write_report  # create-only
            write_report(output, text)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"roll recommendation not written (create-only): "
                             f"{type(exc).__name__}\n")
            return 3
        sys.stderr.write(f"roll recommendation created: {output}\n")
    else:
        # Raw broker diagnostics were suppressed; stdout stays pure output.
        sys.stdout.write(text)
    recommended = isinstance(doc, Mapping) and doc.get("recommended_count", 0) > 0
    return 0 if recommended else 3


__all__ = [
    "DELIVERABLES_SCHEMA", "DeliverableEvidenceError",
    "load_delivery_evidence", "cmd_roll_plan",
]
