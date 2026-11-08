"""Read-only CLI adapter for the ORDINARY multi-leg sell plan.

This is the impure shell around the pure, shared cumulative-budget planner
(:mod:`thetamill.planning`).  It is deliberately a *different* document from a
roll recommendation: an ordinary plan stays re-preflightable by ``run``, so the
top level never sets ``recommendation_only=True`` and never carries a
``thetamill.roll.*`` schema.  Every item, however, is DATA ONLY
(``executable=False``) and carries an explicit ``allowed`` boolean: ``run``
refuses any item that is not exactly ``allowed is True``.

What this adapter does
----------------------
* connects in **readonly** mode and builds an ACCOUNT-SCOPED immutable
  :class:`~thetamill.broker_state.Snapshot` via ``cli._risk_snapshot`` (never a
  second, unscoped account view);
* derives targets, share cover and cost basis from that snapshot BY UNDERLYING
  IDENTITY (``Instrument.key``) — it never calls the old unscoped
  ``_shares`` / ``_coverage`` / ``_short_calls*`` / ``_cost_basis`` helpers;
* selects strikes with a FRESH, LIVE, timestamped stock midpoint
  (:func:`fresh_stock_mid`): ``ticker.time`` is the reception time, only
  ``marketDataType == 1`` is usable, and there is NO historical ``_spot``
  fallback.  A missing quote simply yields no candidate for that symbol;
* qualifies each selected option into a real ``broker_state.Instrument`` via
  ``broker_state._instrument`` (full identity, actual multiplier, underlying);
* loads explicit, per-OPTION-conId stock-only delivery evidence through
  :func:`thetamill.roll_cli.load_delivery_evidence`.  A candidate WITHOUT
  evidence is still displayed, but can NEVER be marked ``allowed``;
* adds the local submission ledger's EXISTING aggregate reservations
  (``execution.aggregate_reservations``) and calls
  :func:`thetamill.planning.assess_plan` ONCE for the whole plan, so budget is
  accumulated SEQUENTIALLY (no per-item double-spend of cash/shares/close).
  A BLOCKED item consumes nothing and is never executable.  Quantity is NEVER
  silently truncated;
* a missing / empty / corrupt ledger is NEVER created or repaired and instead
  BLOCKS usable budget (all items ``allowed=False``);
* honors the optional universe whitelist (subtraction only), masks the account
  by default, emits JSON amounts as strings with no NaN/Inf, and writes with
  create-only :func:`thetamill.report.write_report`.

All errors fail closed.  Broker stdout/stderr is suppressed during connect /
snapshot / finish so JSON stdout stays clean, and no raw account identifier is
ever forwarded.  Nothing here places, cancels or modifies orders.
"""
from __future__ import annotations

import io
import json
import math
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from . import broker_state as bs
from . import planning
from . import risk
from . import universe as uni

SCHEMA = "thetamill.plan.v1"
QUOTE_SOURCE = "IB timestamped bid/ask midpoint"
ZERO = Decimal(0)
ONE = Decimal(1)
FUTURE_SKEW = timedelta(seconds=5)
QUOTE_MAX_AGE = timedelta(seconds=30)
VALID_FOR = timedelta(hours=24)


@contextmanager
def _private_diagnostics():
    """Never forward arbitrary broker/library output (may carry raw account IDs)."""
    with io.StringIO() as sink, redirect_stdout(sink), redirect_stderr(sink):
        yield


# --------------------------------------------------------------------------- #
# Strict, injectable quote / number helpers
# --------------------------------------------------------------------------- #

def _finite_decimal(value: Any, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("expected a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("expected a finite number") from None
    if not result.is_finite() or (positive and result <= 0):
        raise ValueError("expected a positive finite number")
    return result


def _finite_float(value: Any) -> float | None:
    """A JSON-safe finite float, or None (never NaN / Inf)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


class StockQuote(Decimal):
    """A fresh stock midpoint quote with preserved metadata."""
    time: datetime
    con_id: int
    bid: Decimal
    ask: Decimal
    source: str

    def __new__(cls, value: Any, *, time: datetime, con_id: int,
                bid: Decimal, ask: Decimal, source: str = QUOTE_SOURCE):
        instance = super().__new__(cls, value)
        instance.time = time
        instance.con_id = con_id
        instance.bid = bid
        instance.ask = ask
        instance.source = source
        return instance


def fresh_stock_mid(ib, contract, now: datetime | None = None) -> StockQuote | None:
    """A FRESH, LIVE, timestamped bid/ask midpoint for strike selection.

    ``ticker.time`` is the RECEPTION time, not proof of freshness, so a usable
    quote additionally requires ``marketDataType == 1`` (LIVE), an aware
    timestamp within ``[-5s, 30s]`` of the production clock after potentially
    slow requests (or injected deterministic clock), and a non-crossed positive
    bid/ask.  Delayed / frozen (2/3/4) or missing data yields ``None``: there
    is deliberately NO historical ``_spot`` fallback.  ``now`` is injectable so
    offline tests are deterministic; production passes ``None`` (wall clock).
    """
    con_id = getattr(contract, "conId", None)
    if type(con_id) is not int or con_id <= 0:
        return None
    try:
        ticks = ib.reqTickers(contract)
        if not isinstance(ticks, (list, tuple)) or len(ticks) != 1:
            return None
        tick = ticks[0]
        if getattr(tick, "marketDataType", None) != 1:
            return None
        if getattr(getattr(tick, "contract", None), "conId", None) != con_id:
            return None
        stamp = getattr(tick, "time", None)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None \
                or stamp.utcoffset() is None:
            return None
        # Price collection freshness uses current production clock after potentially
        # slow requests, not frozen start clock; injected deterministic clock allowed.
        moment = now if now is not None else datetime.now(timezone.utc)
        if not isinstance(moment, datetime) or moment.tzinfo is None \
                or moment.utcoffset() is None:
            return None
        moment = moment.astimezone(timezone.utc)
        stamp_utc = stamp.astimezone(timezone.utc)
        age = moment - stamp_utc
        if age < -FUTURE_SKEW or age > QUOTE_MAX_AGE:
            return None
        bid, ask = tick.bid, tick.ask
        for value in (bid, ask):
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                return None
            try:
                if not math.isfinite(float(value)) or float(value) <= 0:
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
        bid_dec = Decimal(str(bid))
        ask_dec = Decimal(str(ask))
        if bid_dec > ask_dec:
            return None
        mid = (bid_dec + ask_dec) / 2
        return StockQuote(mid, time=stamp_utc, con_id=con_id,
                          bid=bid_dec, ask=ask_dec, source=QUOTE_SOURCE)
    except Exception:
        return None


def _clean_amount_str(val: Any) -> str | None:
    """Format numeric string or Decimal without trailing point-zero, no scientific notation."""
    if val is None or isinstance(val, bool):
        return None
    try:
        d = Decimal(str(val))
        if d.is_finite():
            if d == d.to_integral_value():
                return str(int(d))
            s = format(d, "f")
            if "." in s:
                s = s.rstrip("0").rstrip(".")
            return s
    except Exception:
        pass
    return str(val)


def _clean_reservations(res: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if res is None:
        return None
    return {
        "cash_base": _clean_amount_str(res.get("cash_base", "0")),
        "shares": {str(k): _clean_amount_str(v) for k, v in res.get("shares", {}).items()},
        "close": {str(k): _clean_amount_str(v) for k, v in res.get("close", {}).items()},
        "exposure": {str(k): _clean_amount_str(v) for k, v in res.get("exposure", {}).items()},
    }


def _json_safe(value):
    """Recursively JSON-safe: Decimal -> str, datetime -> ISO, no NaN/Inf."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return _clean_amount_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _privacy(value, account: str, masked: str):
    """Force account fields to a mask and strip any raw account from strings."""
    if isinstance(value, str):
        return value.replace(account, masked) if account else value
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if str(key).lower() == "account":
                out[key] = masked
            else:
                out[key] = _privacy(item, account, masked)
        return out
    if isinstance(value, (list, tuple)):
        return [_privacy(v, account, masked) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Snapshot-derived (account-scoped) share / cost helpers
# --------------------------------------------------------------------------- #

def _snapshot_shares(snapshot: bs.Snapshot, stock_key: str) -> Decimal:
    """Net STK shares for one underlying identity, from the scoped snapshot."""
    total = ZERO
    for p in snapshot.positions:
        if p.instrument.sec_type == "STK" and p.instrument.key == stock_key:
            try:
                total += _finite_decimal(p.quantity)
            except ValueError:
                continue
    return total


def _snapshot_busy_shares(snapshot: bs.Snapshot, stock_key: str) -> Decimal:
    """Shares already committed to short calls (positions + working orders)."""
    busy = ZERO
    for p in snapshot.positions:
        inst = p.instrument
        if (inst.sec_type == "OPT" and inst.right.upper() == "C"
                and inst.key == stock_key and p.quantity < 0):
            busy += (-_finite_decimal(p.quantity)) * _finite_decimal(
                inst.multiplier, positive=True)
    for o in snapshot.orders:
        inst = o.instrument
        if (inst.sec_type == "OPT" and inst.right.upper() == "C"
                and inst.key == stock_key and o.action.upper() == "SELL"):
            busy += _finite_decimal(o.remaining) * _finite_decimal(
                inst.multiplier, positive=True)
    return busy


def _snapshot_cost(snapshot: bs.Snapshot, stock_key: str) -> Decimal | None:
    """Cost basis for one underlying identity, from the scoped snapshot."""
    for p in snapshot.positions:
        if (p.instrument.sec_type == "STK" and p.instrument.key == stock_key
                and p.quantity > 0):
            for candidate in (p.average_cost, p.price):
                if candidate is None:
                    continue
                try:
                    value = _finite_decimal(candidate, positive=True)
                except ValueError:
                    continue
                return value
    return None


# --------------------------------------------------------------------------- #
# Document rendering
# --------------------------------------------------------------------------- #

def _human_summary(doc: Mapping) -> str:
    lines = [f"{doc['env']} | 账户 {doc['account']} | 快照 {doc['snapshot_time']}"
             f" | 本计划只读，不下单/撤单"]
    for issue in doc["issues"]:
        lines.append(f"  ⚠ {issue}")
    lines.append(f"  候选 {len(doc['items'])} 条 | 通过 {doc['allowed_count']}"
                 f" | 阻断 {doc['blocked_count']}")
    for it in doc["items"]:
        right = "C" if it["preset"] != "cash-put" else "P"
        lines.append(f"  {'✓' if it['allowed'] else '✗'} #{it['i']} "
                     f"{it['preset']:<12}{it['symbol']:<8}{it['expiry']} "
                     f"{it['strike']}{right} × {it['qty']}")
        for reason in it["reasons"][:3]:
            lines.append(f"      - {reason}")
    lines.append("  说明：每条建议都不是执行授权；run 会重新预检，并拒绝 allowed 非 true 的条目。")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main entry point (called by cli.cmd_plan after main installs a delegate)
# --------------------------------------------------------------------------- #

def cmd_plan(cfg, args, now: datetime | None = None) -> int:
    """Build a read-only, cumulative-budget ordinary plan.  Never executes.

    Production calls ``cmd_plan(cfg, args)``.  ``now`` is an optional injectable
    aware clock used by deterministic offline tests; it is never used to
    fabricate a quote timestamp.  Returns 0 when at least one item is allowed,
    3 when all items are blocked, no candidates are available, the plan is
    incomplete, or the create-only output could not be written.
    """
    from . import cli as _cli  # lazy import avoids a circular import with cli.py

    injected_now = now
    if (now is None or not isinstance(now, datetime) or now.tzinfo is None
            or now.utcoffset() is None):
        injected_now = None

    current_now = injected_now if injected_now is not None else datetime.now(timezone.utc)

    issues: list[str] = []
    metas: list[dict] = []
    ib = None
    snapshot: bs.Snapshot | None = None
    limits: risk.RiskLimits | None = None
    pd: Mapping | None = None
    confirmed: Mapping[int, Any] = MappingProxyType({})
    uni_doc = None
    uni_map: dict = {}
    whitelist_failed = False
    reservations = None
    fatal = False
    block_reasons: list[str] = []
    env = "paper"
    port = None
    is_live = False
    cur_default = str(cfg.get("default_currency", "USD")).upper()
    arg_cur = str(getattr(args, "currency", None) or cur_default).upper()

    try:
        # ---- strict, local argument validation (fail closed, never sys.exit) ---- #
        qty = getattr(args, "qty", 1)
        if isinstance(qty, bool) or type(qty) is not int or qty <= 0:
            raise ValueError("qty must be a positive integer")
        dte_min = getattr(args, "dte_min", 21)
        dte_max = getattr(args, "dte_max", 45)
        target_dte = getattr(args, "target_dte", 30)
        otm = _finite_float(getattr(args, "otm", 0.05))
        if otm is None or otm < 0 or otm >= 1:
            raise ValueError("otm must be a finite number in [0, 1)")
        for name, value in (("dte_min", dte_min), ("dte_max", dte_max),
                            ("target_dte", target_dte)):
            if isinstance(value, bool) or type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        preset_choice = getattr(args, "preset", "auto") or "auto"
        if preset_choice not in {"auto", "sell-call", "cash-put"}:
            raise ValueError("preset must be auto / sell-call / cash-put")

        port, env = _cli.resolve_port(cfg, args)
        is_live = bool(_cli._is_live_env(cfg, args, port))
        environment = "live" if is_live else "paper"
        with _private_diagnostics():
            ib = _cli.connect(cfg, port, readonly=True,
                              force_delayed=getattr(args, "delayed", False), args=args)
            # Account-scoped snapshot.
            snapshot = _cli._risk_snapshot(ib, cfg, args, environment,
                                           required_currencies=(arg_cur,))
        if snapshot is None or not isinstance(snapshot, bs.Snapshot):
            raise ValueError("account snapshot unavailable")
        account = snapshot.account
        if not isinstance(account, str) or not account:
            raise ValueError("account scope unavailable")

        # ---- validated risk limits (config-level) --------------------------- #
        try:
            limits = risk.validate_risk_config(cfg)
        except Exception as exc:
            limits = None
            block_reasons.append(
                f"risk config invalid ({type(exc).__name__}); new opens blocked")

        # ---- EXISTING ledger only; never create / migrate / repair ----------- #
        ledger = None
        try:
            ledger_path = _cli._data_dir() / "state.sqlite3"
            if not ledger_path.is_file():
                raise ValueError("ledger missing")
            from .state import SubmissionLedger
            from .execution import aggregate_reservations
            # read_only=True validates the schema (empty/corrupt/foreign ->
            # SchemaError) without ever writing, creating or migrating.
            ledger = SubmissionLedger(
                ledger_path, (limits.day_timezone if limits else None) or "UTC",
                read_only=True)
            reservations = aggregate_reservations(ledger, account, environment)
        except Exception as exc:
            reservations = None
            block_reasons.append(
                "local submission ledger missing/empty/corrupt/unreadable "
                f"({type(exc).__name__}); available budget unknown; new opens "
                "blocked (ledger not created or repaired)")
        finally:
            if ledger is not None:
                close_fn = getattr(ledger, "close", None)
                if callable(close_fn):
                    try:
                        close_fn()
                    except Exception:
                        pass

        # ---- optional universe whitelist (subtraction only) ----------------- #
        universe_requested = bool(getattr(args, "universe", None))
        if universe_requested:
            try:
                with _private_diagnostics():
                    uni_doc = uni.load_universe(args.universe)
                uni_map = uni.universe_allowed(uni_doc)
                if not uni_map:
                    whitelist_failed = True
            except (Exception, SystemExit) as exc:
                whitelist_failed = True
                issues.append(f"universe whitelist rejected ({type(exc).__name__}); "
                              "no whitelist applied; no recommendations produced")
            if whitelist_failed and not issues:
                issues.append("universe whitelist is empty/invalid; no recommendations produced")
        if whitelist_failed:
            block_reasons.append("universe whitelist invalid/empty; plan incomplete")

        # ---- targets (explicit args, else scoped snapshot STK positions) ---- #
        targets: list[tuple[str, str]] = []
        symbols = getattr(args, "symbols", None)
        if symbols:
            targets += [(s.strip().upper(), arg_cur)
                        for s in str(symbols).split(",") if s.strip()]
        if getattr(args, "symbol", None):
            targets.append((str(args.symbol).upper(), arg_cur))
        if not targets:
            for p in snapshot.positions:
                if p.instrument.sec_type == "STK" and p.quantity > 0:
                    key = (p.instrument.symbol.upper(), p.instrument.currency)
                    if key not in targets:
                        targets.append(key)
        targets = list(dict.fromkeys(targets))
        if uni_doc:
            before = len(targets)
            targets = [t for t in targets if t[0] in uni_map]
            if before > len(targets):
                issues.append(f"{before - len(targets)} target(s) filtered out "
                              "by the universe whitelist")

        # ---- per-target candidate selection -------------------------------- #
        cache: dict = {}
        seen_con_ids: set[int] = set()
        for sym, cur in targets:
            cur = str(cur or arg_cur).upper()
            stk, chain = _cli._chain_safe(ib, sym, cur)
            if stk is None or chain is None:
                issues.append(f"{sym}: no option chain available; skipped")
                continue
            px = fresh_stock_mid(ib, stk, injected_now)
            if px is None:
                issues.append(f"{sym}: no fresh LIVE timestamped stock quote; "
                              "historical fallback refused; skipped")
                continue
            expiry, dte = _cli._pick_expiry(chain.expirations, dte_min, dte_max,
                                            target_dte)
            if not expiry:
                issues.append(f"{sym}: no expiry within the DTE window; skipped")
                continue

            stock_con_id = getattr(stk, "conId", None)
            if type(stock_con_id) is not int or stock_con_id <= 0:
                issues.append(f"{sym}: underlying conId unavailable; skipped")
                continue
            stock_key = f"{stock_con_id}:{cur}"
            have = _snapshot_shares(snapshot, stock_key)
            busy = _snapshot_busy_shares(snapshot, stock_key)
            avail = have - busy
            if avail < 0:
                avail = ZERO
            cost = _snapshot_cost(snapshot, stock_key)
            note: list[str] = []

            right = ("P" if preset_choice == "cash-put"
                     or (preset_choice == "auto" and have <= 0) else "C")
            preset = "cash-put" if right == "P" else "sell-call"
            target = px * (1 + Decimal(str(otm))) if right == "C" \
                else px * (1 - Decimal(str(otm)))
            opt, strike = _cli._resolve_option(
                ib, sym, expiry, chain.strikes, target, right, cur, chain,
                floor=cost if right == "C" else None)
            inst = bs._instrument(opt, ib, cache) if opt is not None else None
            mult = inst.multiplier if inst is not None else None

            if preset_choice == "auto" and right == "C":
                if inst is None:
                    note.append("无可用 Call 合约，自动转为现金担保卖 Put")
                elif avail < (mult or ZERO):
                    note.append(f"净可用 {avail} 股不足 1 张 Call（需 {mult} 股），"
                                "自动转为现金担保卖 Put")
                if inst is None or avail < (mult or ZERO):
                    right, preset = "P", "cash-put"
                    opt, strike = _cli._resolve_option(
                        ib, sym, expiry, chain.strikes,
                        px * (1 - Decimal(str(otm))), right, cur, chain)
                    inst = bs._instrument(opt, ib, cache) if opt is not None else None
                    mult = inst.multiplier if inst is not None else None
            if inst is None or strike is None:
                issues.append(f"{sym}: no usable strike on {expiry}; skipped")
                continue
            if inst.con_id in seen_con_ids:
                # Never let two items spend the same conId's budget twice.
                issues.append(f"{sym}: duplicate candidate conId {inst.con_id}; skipped")
                continue
            seen_con_ids.add(inst.con_id)

            try:
                strike_dec = _finite_decimal(inst.strike, positive=True)
                res_strike_dec = _finite_decimal(strike, positive=True)
                mult_dec = _finite_decimal(mult, positive=True)
                qty_dec = _finite_decimal(qty, positive=True)
            except ValueError:
                issues.append(f"{sym}: unusable option strike/multiplier/qty; skipped")
                continue

            # Ensure strike agrees exactly with qualified Instrument.strike,
            # never scalar-round to hide contradiction.
            if strike_dec != res_strike_dec:
                issues.append(
                    f"{sym}: resolved strike {res_strike_dec} contradicts "
                    f"qualified instrument strike {strike_dec}; skipped")
                continue

            px_float = _finite_float(px)
            strike_float = _finite_float(strike_dec)
            if px_float is None or strike_float is None or px_float <= 0:
                issues.append(f"{sym}: unusable price/strike; skipped")
                continue
            otm_pct = (strike_float - px_float) / px_float

            # Quantity is NEVER silently truncated: the configured qty is kept
            # and the cumulative risk gate (not a local cap) decides allowance.
            if right == "C":
                need = mult_dec * qty_dec
                if avail < need:
                    note.append(f"净可用 {avail} 股不足 {qty} 张 Call（需 {need} 股）；"
                                "不自动下调张数，由累计预算门禁判定")
                if cost is not None and strike_dec < cost:
                    note.append(f"行权价低于成本 {cost}，被行权即锁定亏损")
                collat = (qty_dec * mult_dec * cost) if cost is not None \
                    else (qty_dec * mult_dec * px)
                collat_txt = f"{qty_dec * mult_dec} 股"
            else:
                collat = strike_dec * mult_dec * qty_dec
                collat_txt = f"{collat} {cur}"
            try:
                prem, src = _cli._quote_premium(ib, opt)
            except Exception:
                prem, src = None, "无行情"
            prem_dec = None
            if prem is not None:
                try:
                    prem_dec = _finite_decimal(prem, positive=True)
                except ValueError:
                    prem_dec = None
            if prem_dec is None:
                src = "无行情"
            ann = None
            if prem_dec is not None and collat and dte:
                try:
                    ann = float(prem_dec * mult_dec * qty_dec / collat * 365 / max(dte, 1))
                    if not math.isfinite(ann):
                        ann = None
                except Exception:
                    ann = None

            # Whitelist gate is a BLOCK (item still shown, never allowed), not
            # a silent skip: run refuses any item with allowed != true.
            universe_block = None
            if uni_doc:
                ok, why = uni.universe_gate(sym, preset, strike_float, uni_map)
                if not ok:
                    universe_block = f"准入白名单拦截：{why}"

            quote_evidence = {
                "source": getattr(px, "source", QUOTE_SOURCE),
                "time": getattr(px, "time", None).isoformat() if getattr(px, "time", None) else None,
                "conId": getattr(px, "con_id", stock_con_id),
                "bid": _clean_amount_str(getattr(px, "bid", None)),
                "ask": _clean_amount_str(getattr(px, "ask", None)),
                "mid": _clean_amount_str(px),
            }

            m = {
                "display": {
                    "i": len(metas) + 1,
                    "preset": preset,
                    "symbol": sym,
                    "currency": cur,
                    "expiry": str(expiry),
                    "dte": int(dte) if isinstance(dte, (int, float)) else dte,
                    "strike": _clean_amount_str(strike_dec),
                    "spot": round(px_float, 3),
                    "otm_pct": round(otm_pct, 6),
                    "qty": _clean_amount_str(qty_dec),                       # never truncated
                    "mult": _clean_amount_str(mult_dec),
                    "conId": inst.con_id,
                    "tradingClass": inst.trading_class,
                    "exchange": inst.exchange,
                    "premium": _clean_amount_str(prem_dec) if prem_dec is not None else None,
                    "premium_src": src,
                    "collateral": _clean_amount_str(collat),        # amount as str, no NaN
                    "collateral_txt": str(collat_txt),
                    "ann_return": (str(round(ann, 6)) if ann is not None else None),
                    "note": "；".join(note),
                },
                "quote": quote_evidence,
                "instrument": inst,
                "request": risk.OrderRequest(inst, "SELL", Decimal(qty), "LMT",
                                             None, "DAY"),
                "pre_reasons": [],
                "delivery": None,
            }
            # Identity completeness: run requires these verbatim; an incomplete
            # identity is an explicit block, never a guessed one.
            if (type(inst.con_id) is not int or inst.con_id <= 0
                    or not isinstance(inst.trading_class, str) or not inst.trading_class.strip()
                    or not isinstance(inst.exchange, str) or not inst.exchange.strip()):
                m["pre_reasons"].append("incomplete qualified contract identity "
                                        "(conId / tradingClass / exchange)")
            if inst.key != stock_key:
                m["pre_reasons"].append("chain underlying identity does not match "
                                        "the qualified option")
            if universe_block:
                m["pre_reasons"].append(universe_block)
            metas.append(m)

        current_now = injected_now if injected_now is not None else datetime.now(timezone.utc)

        # ---- per-OPTION-conId delivery evidence (shared with roll_cli) ------ #
        try:
            from .roll_cli import load_delivery_evidence
            confirmed = load_delivery_evidence(
                getattr(args, "delivery_evidence", None),
                [m["instrument"] for m in metas], current_now)
        except Exception as exc:
            # If the entire file had an issue (e.g. one candidate's evidence expired),
            # try loading each candidate's record individually so a single expired/missing
            # candidate does not block later valid candidates.
            ev_path = getattr(args, "delivery_evidence", None)
            individual_confirmed: dict[int, Any] = {}
            if ev_path:
                try:
                    p = Path(ev_path).expanduser()
                    if p.is_file():
                        raw_doc = json.loads(p.read_text(encoding="utf-8"))
                        records = raw_doc.get("records", [])
                        if isinstance(records, list):
                            import tempfile
                            for rec in records:
                                try:
                                    with tempfile.TemporaryDirectory() as td:
                                        tf = Path(td) / "single_ev.json"
                                        tf.write_text(json.dumps({
                                            "schema": "thetamill.deliverables.v1",
                                            "records": [rec],
                                        }), encoding="utf-8")
                                        c_map = load_delivery_evidence(
                                            tf, [m["instrument"] for m in metas], current_now)
                                        individual_confirmed.update(c_map)
                                except Exception:
                                    pass
                except Exception:
                    pass
            confirmed = MappingProxyType(individual_confirmed)
            if not confirmed:
                issues.append(f"delivery evidence rejected ({type(exc).__name__}); "
                              "no contract authorized")
            else:
                issues.append(f"partial delivery evidence accepted ({type(exc).__name__} for some records)")
        for m in metas:
            con_id = m["instrument"].con_id
            record = confirmed.get(con_id) if confirmed else None
            m["delivery"] = {
                "confirmed": bool(record is not None),
                "source": (record.get("source") if record is not None else None),
                "verified_at": (record.get("verified_at") if record is not None else None),
                "expires_at": (record.get("expires_at") if record is not None else None),
                "stock_only": (record.get("stock_only") if record is not None else None),
                "record": record,
            }

        # ---- ONE cumulative assessment for eligible candidates ------------- #
        # Missing/expired deliverable blocked candidates (and pre-blocked items)
        # must NOT consume shared cumulative capacity to block later valid candidates.
        eligible_metas = [
            m for m in metas
            if not m["pre_reasons"] and (m["delivery"] or {}).get("confirmed")
        ]
        eligible_results: dict[int, dict] = {}

        if not block_reasons and limits is not None:
            try:
                assessment = planning.assess_plan(
                    snapshot, [m["request"] for m in eligible_metas], limits, reservations,
                    now=current_now,
                    evidence_currencies=tuple(sorted(
                        {m["instrument"].currency for m in metas})),
                )
                pd = planning.plan_document(assessment)
                for row, em in zip(pd["items"], eligible_metas):
                    eligible_results[id(em)] = row
            except Exception as exc:
                pd = None
                block_reasons.append(
                    f"cumulative assessment failed ({type(exc).__name__}); "
                    "all candidates blocked")
        else:
            pd = None
    except (Exception, SystemExit) as exc:
        # Broker/account failures can contain raw account IDs; never echo them.
        fatal = True
        pd = None
        block_reasons.append(f"plan computation incomplete ({type(exc).__name__}); "
                             "all candidates blocked; broker details withheld for privacy")
    finally:
        if ib is not None:
            try:
                with _private_diagnostics():
                    _cli.finish(ib)
            except (Exception, SystemExit):
                issues.append("broker disconnect incomplete; details withheld for privacy")

    # ---- merge per-item outcomes ------------------------------------------ #
    hard_reason = "；".join(dict.fromkeys(block_reasons))
    for index, m in enumerate(metas):
        item = dict(m["display"])
        pre_reasons = list(m["pre_reasons"])
        reasons: list[str] = []
        evidence_reasons: list[str] = []
        requirement = None

        if id(m) in eligible_results:
            row = eligible_results[id(m)]
            engine_allowed = bool(row["allowed"])
            reasons = list(row["reasons"])
            evidence_reasons = list(row["evidence_reasons"])
            requirement = _clean_reservations(row["requirement"])
        else:
            engine_allowed = False
            evidence_reasons = list(pd["evidence_reasons"]) if pd is not None else []

        reasons += pre_reasons
        if m["display"]["note"]:
            reasons.append(str(m["display"]["note"]))
        if hard_reason:
            reasons.append(hard_reason)
        delivery = m["delivery"] or {"confirmed": False, "source": None}
        if not delivery["confirmed"]:
            reasons.append(
                f"no per-conId stock-only delivery evidence for option conId "
                f"{m['instrument'].con_id}; candidate shown but not admissible")
            engine_allowed = False
        if pre_reasons or hard_reason:
            engine_allowed = False
        if engine_allowed and not reasons:
            reasons.append("cumulative budget and evidence gates satisfied")
        item["allowed"] = bool(engine_allowed)
        item["executable"] = False
        item["reasons"] = [str(r) for r in reasons]
        item["evidence_reasons"] = [str(r) for r in evidence_reasons]
        item["requirement"] = requirement
        item["delivery_evidence"] = delivery
        item["quote"] = m.get("quote")
        item["account"] = "***"          # replaced by _privacy below
        item["environment"] = ("live" if is_live else "paper")
        m["item"] = item

    items = [m["item"] for m in metas]
    allowed_count = sum(1 for it in items if it["allowed"] is True)
    blocked_count = len(items) - allowed_count

    account = (snapshot.account if snapshot is not None else "")
    try:
        from .report import mask_account
        masked = mask_account(account) if account else "***"
    except Exception:
        masked = "***"

    budget_confirmed = bool(pd and pd.get("budget_confirmed"))
    summary = _json_safe(pd["summary"]) if pd is not None else None
    reservations_view = _clean_reservations(pd["reservations"]) if pd is not None else None
    doc = {
        "schema": SCHEMA,
        "kind": "ordinary_cumulative_budget_plan",
        # Ordinary plans MUST stay re-preflightable by run: no roll schema and
        # NO top-level recommendation_only=True (that would abort every run).
        "recommendation_only": False,
        "read_only": True,
        "executable": False,
        "disclaimer": ("仅供人工复核的累计预算评估；不是执行授权，不会下单/撤单。"
                       "run 会重新预检，并拒绝 allowed 非 true 的条目。"),
        "generated": current_now.isoformat(),
        "valid_until": (current_now + VALID_FOR).isoformat(),   # aware ISO
        "is_live": bool(is_live),
        "env": env,
        "port": port,
        "account": masked,
        "environment": ("live" if is_live else "paper"),
        "base_currency": (snapshot.base_currency if snapshot is not None else "unknown"),
        "snapshot_time": (snapshot.captured_at.isoformat()
                          if snapshot is not None else None),
        "source": {
            "account_state": "account-scoped broker snapshot (cli._risk_snapshot)",
            "quote": QUOTE_SOURCE,
            "risk": "thetamill.risk.assess / thetamill.planning.assess_plan",
            "budget": "existing local submission ledger, read-only aggregate",
            "delivery": "thetamill.deliverables.v1, per-OPTION conId",
        },
        "limits": {
            "enabled": bool(limits.enabled) if limits is not None else False,
            "display": (limits.display if limits is not None else "unavailable"),
            "max_snapshot_age_seconds": (limits.max_snapshot_age_seconds
                                         if limits is not None else None),
            "has_exposure_caps": (bool(limits.has_exposure_caps)
                                  if limits is not None else False),
        },
        "budget_confirmed": budget_confirmed,
        "delivery_evidence_confirmed": sum(
            1 for m in metas if (m["delivery"] or {}).get("confirmed")),
        "currencies": sorted({it["currency"] for it in items}),
        "window": {"dte_min": getattr(args, "dte_min", 21),
                   "dte_max": getattr(args, "dte_max", 45),
                   "otm": getattr(args, "otm", 0.05)},
        "universe": uni.meta(uni_doc) if uni_doc else None,
        "evidence_reasons": ([str(r) for r in pd["evidence_reasons"]]
                             if pd is not None else []),
        "reservations": reservations_view,
        "summary": summary,
        "issues": list(dict.fromkeys(issues + block_reasons)),
        "items": items,
        "allowed_count": allowed_count,
        "blocked_count": blocked_count,
    }
    doc = _privacy(_json_safe(doc), account, masked)

    text = json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output = getattr(args, "json_out", None) or getattr(args, "output", None)
    fmt = getattr(args, "format", None)
    write_failed = False
    if output:
        try:
            from .report import write_report  # create-only (open("x"))
            out = Path(output).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            write_report(out, text)
            sys.stderr.write(f"plan created (create-only): {out}\n")
            sys.stdout.write(_human_summary(doc))
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"plan not written (create-only, no overwrite): "
                             f"{type(exc).__name__}\n")
            write_failed = True
    elif fmt == "terminal":
        sys.stdout.write(_human_summary(doc))
    else:
        # Raw broker diagnostics were suppressed; stdout stays pure JSON.
        sys.stdout.write(text)

    if write_failed or fatal or block_reasons:
        return 3
    if not items:
        return 3
    return 0 if allowed_count > 0 else 3


__all__ = ["SCHEMA", "QUOTE_SOURCE", "fresh_stock_mid", "cmd_plan"]
