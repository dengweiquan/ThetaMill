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
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping, Sequence

from . import broker_state as bs
from .delivery_evidence import (
    DELIVERABLES_SCHEMA as DELIVERABLES_SCHEMA,
    DeliverableEvidenceError as DeliverableEvidenceError,
    _IDENTITY_FIELDS as _IDENTITY_FIELDS,
    _aware as _aware,
    _finite as _finite,
    _freeze as _freeze,
    _identity_for as _identity_for,
    _identity_matches as _identity_matches,
    load_delivery_evidence as load_delivery_evidence,
)
from . import roll
from . import universe as uni
from .market_data import ROLL_POLICY, validate_ticker
from .broker_requests import safe_req_tickers

# Upper bound (seconds) on one snapshot request.  ib_async's own default of 0
# means "never time out", which turns an unanswered request into a hang.
SNAPSHOT_TIMEOUT_SECONDS = 20.0


from .broker_session import private_diagnostics as _private_diagnostics


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
    from . import cli as _cli  # lazy: avoid a module-level circular import

    raw: dict[int, object] = {}
    if not contracts:
        return raw
    previous = getattr(ib, "RequestTimeout", None)
    caller_timeout = _cli._validate_request_timeout(previous, default=None)
    if caller_timeout is not None:
        target = min(caller_timeout, SNAPSHOT_TIMEOUT_SECONDS)
    else:
        target = SNAPSHOT_TIMEOUT_SECONDS
    try:
        _cli._enforce_request_timeout(ib, target)
    except Exception as exc:
        notes.append(f"market data request failed ({type(exc).__name__}); "
                     "all quotes treated as missing")
        return raw
    try:
        with _private_diagnostics():
            tickers = safe_req_tickers(ib, *contracts.values(), timeout=target)
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

        validated = validate_ticker(t, cid, now, policy=ROLL_POLICY, source="reqTickers")
        if validated is not None:
            try:
                quotes[cid] = roll.Quote(cid, validated.bid, validated.ask, validated.time, source="reqTickers")
            except Exception as exc:
                notes.append(f"quote for conId {cid} rejected by roll engine "
                             f"({type(exc).__name__}); quote unavailable")
                quotes[cid] = None
            continue

        quotes[cid] = None
        data_type = getattr(t, "marketDataType", None)
        if type(data_type) is not int or data_type != 1:
            notes.append(f"ticker for conId {cid} is DELAYED/FROZEN "
                         f"(marketDataType={data_type!r}); not usable as a "
                         "current cash-estimate quote")
            continue
        stamp = getattr(t, "time", None)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None \
                or stamp.utcoffset() is None:
            notes.append(f"ticker for conId {cid} has no source timestamp; "
                         "quote missing (not re-stamped)")
            continue
        if stamp.astimezone(timezone.utc) > now + roll._CLOCK_SKEW:
            notes.append(f"ticker for conId {cid} is in the future; quote unavailable")
            continue

        bid_raw = getattr(t, "bid", None)
        ask_raw = getattr(t, "ask", None)
        try:
            b = Decimal(str(bid_raw)) if bid_raw is not None and not isinstance(bid_raw, bool) else None
            a = Decimal(str(ask_raw)) if ask_raw is not None and not isinstance(ask_raw, bool) else None
            if b is not None and a is not None and b.is_finite() and a.is_finite() and b > a:
                notes.append(f"ticker for conId {cid} is crossed (bid {b} > ask {a}); "
                             "quote unavailable")
                continue
        except Exception:
            pass
        notes.append(f"ticker for conId {cid} has no bid/ask; quote missing")
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
