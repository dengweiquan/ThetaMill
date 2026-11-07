"""Local, read-only risk snapshot rendering. No broker or submission API calls."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from typing import Mapping

from .broker_state import Snapshot
from .planning import quote_evidence_problem

SCHEMA = "thetamill.report.v1"


def mask_account(account: str) -> str:
    return "***" + account[-3:] if len(account) > 3 else "***"


def _safe(value):
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else None
    return value


def _has_nonfinite(value) -> bool:
    if isinstance(value, Decimal):
        return not value.is_finite()
    if isinstance(value, float):
        import math
        return not math.isfinite(value)
    if isinstance(value, Mapping):
        return any(_has_nonfinite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_nonfinite(v) for v in value)
    return False


def _redact(value, account: str):
    if isinstance(value, Mapping):
        return {_redact(str(k), account): ("[redacted]" if str(k).lower().replace("_", "") in
                        {"orderref", "intentid", "code", "approvalcode"} else
                        _redact(v, account)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v, account) for v in value]
    if isinstance(value, str) and account:
        return value.replace(account, mask_account(account))
    return value


def instrument_data(instrument) -> dict:
    return {"con_id": instrument.con_id, "underlying_id": instrument.underlying_id,
            "symbol": instrument.symbol, "currency": instrument.currency,
            "sec_type": instrument.sec_type, "exchange": instrument.exchange,
            "multiplier": str(instrument.multiplier), "right": instrument.right,
            "strike": str(instrument.strike), "expiry": instrument.expiry,
            "trading_class": instrument.trading_class}


def build_report(snapshot: Snapshot | None, *, risk_summary: Mapping | None = None,
                 ledger_summary: Mapping | None = None, issues=(),
                 now: datetime | None = None, reveal_account: bool = False,
                 max_data_age_seconds: int = 30) -> dict:
    """Consume a shared risk calculation; never invent a second collateral model.

    ``risk_summary`` is the pure risk engine's serialized account assessment.
    Omission means unknown, not zero. No realized P&L is inferred from local logs.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Report timestamp requires a timezone")
    if type(max_data_age_seconds) is not int or max_data_age_seconds <= 0:
        raise ValueError("Report freshness limit requires a positive integer")
    warnings = list(issues)
    valuations_confirmed = True

    def fresh(stamp, limit=30):
        age = (now - stamp).total_seconds()
        return -5 <= age <= limit
    result = {"schema": SCHEMA, "kind": "intraday_risk_snapshot",
              "generated_at": now.isoformat(), "read_only": True,
              "account": None, "environment": None, "captured_at": None,
              "base_currency": None, "cash": None, "net_liquidation": None,
              "positions": None, "orders": None, "expiry_distribution": None,
              "risk": _safe(risk_summary), "ledger": _safe(ledger_summary),
              "price_sources": None, "fx_sources": None,
              "strategy_realized_pnl": None}
    if snapshot is None:
        result["risk"] = None
        warnings.append("Broker snapshot unavailable; risk is unknown")
    else:
        result.update(account=snapshot.account if reveal_account else mask_account(snapshot.account),
                      environment=snapshot.environment, captured_at=snapshot.captured_at.isoformat(),
                      base_currency=snapshot.base_currency,
                      cash=[{"currency": c, "amount": str(a)} for c, a in sorted(snapshot.cash.items())],
                      net_liquidation={"amount": _safe(snapshot.net_liquidation),
                                       "currency": snapshot.base_currency})
        warnings.extend(snapshot.issues)
        if snapshot.net_liquidation is None:
            warnings.append("Net liquidation unavailable")
        result["price_sources"] = {str(cid): {"price": str(q.price), "as_of": q.as_of.isoformat(),
                                               "source": q.source}
                                   for cid, q in snapshot.price_quotes.items()}
        result["fx_sources"] = {currency: {"rate_base_per_unit": str(q.rate),
                                             "as_of": q.as_of.isoformat(), "source": q.source}
                                for currency, q in snapshot.fx_quotes.items()}
        if not fresh(snapshot.captured_at, max_data_age_seconds):
            warnings.append("Broker snapshot is stale or future-dated; budgets withheld")
            valuations_confirmed = False
        for position in snapshot.positions:
            if position.instrument.sec_type != "STK":
                continue
            cid = position.instrument.con_id
            quote = snapshot.price_quotes.get(cid)
            if quote is None:
                warnings.append(f"Stock valuation timestamp unavailable: {cid}")
                valuations_confirmed = False
            else:
                problem = quote_evidence_problem(quote, position.price, key=cid, now=now)
                if problem:
                    warnings.append(f"Stock valuation unconfirmed: {problem}")
                    valuations_confirmed = False
        currencies = set(snapshot.cash) | set(snapshot.fx)
        currencies.update(p.instrument.currency for p in snapshot.positions)
        currencies.update(o.instrument.currency for o in snapshot.orders)
        for currency in currencies - {snapshot.base_currency}:
            quote = snapshot.fx_quotes.get(currency)
            if quote is None:
                warnings.append(f"FX timestamp unavailable: {currency}")
                valuations_confirmed = False
            else:
                problem = quote_evidence_problem(
                    quote, snapshot.fx.get(currency), key=currency, now=now)
                if problem:
                    warnings.append(f"FX valuation unconfirmed: {problem}")
                    valuations_confirmed = False
        if not valuations_confirmed:
            # Raw source values remain visible, never a purported spendable budget.
            result["risk"] = None
        result.update(positions=[], orders=[], expiry_distribution=[])
        expiries = {}
        for position in snapshot.positions:
            instrument = position.instrument
            if position.price is None:
                warnings.append(f"Position market price unavailable: {instrument.con_id}")
            row = {"instrument": instrument_data(instrument), "quantity": str(position.quantity),
                   "market_price": _safe(position.price), "average_cost": _safe(position.average_cost)}
            result["positions"].append(row)
            if instrument.sec_type == "OPT":
                try:
                    if (not isinstance(instrument.expiry, str) or len(instrument.expiry) != 8
                            or not instrument.expiry.isascii() or not instrument.expiry.isdigit()):
                        raise ValueError("Expiry must be exactly YYYYMMDD")
                    expiry = datetime.strptime(instrument.expiry, "%Y%m%d").date()
                    days = (expiry - now.date()).days
                    entry = expiries.setdefault(instrument.expiry, {"expiry": instrument.expiry,
                                                                    "days_to_expiry": days,
                                                                    "contracts": "0"})
                    entry["contracts"] = str(Decimal(entry["contracts"]) + abs(position.quantity))
                except ValueError:
                    warnings.append(f"Invalid option expiry: {instrument.con_id}")
        result["expiry_distribution"] = [expiries[k] for k in sorted(expiries)]
        for order in snapshot.orders:
            result["orders"].append({"instrument": instrument_data(order.instrument),
                                     "action": order.action, "remaining": str(order.remaining),
                                     "order_type": order.order_type,
                                     "limit_price": _safe(order.limit_price)})
    if not risk_summary:
        warnings.append("Shared risk assessment unavailable; coverage and exposure are unknown")
    if not ledger_summary:
        warnings.append("Local intent ledger unavailable; unresolved submission status is unknown")
    if ledger_summary and ledger_summary.get("outstanding"):
        warnings.append("Local submission reservations remain unresolved; availability is conservative")
    if _has_nonfinite(risk_summary) or _has_nonfinite(ledger_summary):
        warnings.append("Nonfinite assessment values normalized to unknown")
    if risk_summary:
        unknown = risk_summary.get("unknown", ())
        if isinstance(unknown, (list, tuple)):
            warnings.extend(str(x) for x in unknown)
        elif unknown is not None:
            warnings.append("Risk assessment has malformed unknown-data details")
        if risk_summary.get("cash_confirmed") is False or risk_summary.get("exposure_confirmed") is False:
            warnings.append("Risk assessment contains unconfirmed cash or exposure")
    result["issues"] = list(dict.fromkeys(warnings))
    result["complete"] = not result["issues"]
    result = _redact(result, snapshot.account if snapshot and not reveal_account else "")
    return _safe(result)


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_report(report: Mapping, format: str = "terminal") -> str:
    if format == "json":
        return json.dumps(_safe(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if format not in {"terminal", "markdown"}:
        raise ValueError("Report format must be terminal, json or markdown")
    lines = ["# ThetaMill 风险快照", "", "盘中快照，非正式收盘报告；非投资建议。", "",
             f"账户：{_cell(report['account'] or 'N/A')} | 环境：{_cell(report['environment'] or 'N/A')}",
             f"快照：{_cell(report['captured_at'] or 'N/A')}",
             f"数据完整：{'是' if report['complete'] else '否 / 不可确认'}", "", "## 现金"]
    if report["cash"] is None:
        lines.append("N/A（未知，不按零计）")
    else:
        lines.extend(f"- {_cell(item['amount'])} {_cell(item['currency'])}" for item in report["cash"])
    lines.extend(["", "## 持仓", "| 标的 / conId | 数量 | 乘数 | 价格 / 币种 |", "|---|---:|---:|---|"])
    if report["positions"] is None:
        lines.append("持仓：N/A（未取得账户快照）")
    for p in report["positions"] or []:
        c = p["instrument"]
        lines.append(f"| {_cell(c['symbol'])} / {c['con_id']} | {_cell(p['quantity'])} | "
                     f"{_cell(c['multiplier'])} | {_cell(p['market_price'] or 'N/A')} {_cell(c['currency'])} |")
    lines.extend(["", "## 在途订单"])
    if report["orders"] is None:
        lines.append("N/A（未取得账户快照，不按零计）")
    else:
        lines.extend([f"在途订单：{len(report['orders'])}（不提前释放担保）",
                      "| 标的 / conId | 方向 | 剩余量 | 类型 | 限价 |",
                      "|---|---|---:|---|---:|"])
        for order in report["orders"]:
            c = order["instrument"]
            lines.append(f"| {_cell(c['symbol'])} / {c['con_id']} | {_cell(order['action'])} | "
                         f"{_cell(order['remaining'])} | {_cell(order['order_type'])} | "
                         f"{_cell(order['limit_price'] or 'N/A')} |")
    lines.extend(["", "## 到期分布"])
    if report["expiry_distribution"] is None:
        lines.append("N/A（未知，未取得持仓快照）")
    elif not report["expiry_distribution"]:
        lines.append("无已确认的期权到期记录；请同时检查数据质量提示。")
    else:
        lines.extend(["| 到期日 | 剩余天数 | 持仓张数（绝对值） |", "|---|---:|---:|"])
        for expiry in report["expiry_distribution"]:
            lines.append(f"| {_cell(expiry['expiry'])} | {_cell(expiry['days_to_expiry'])} | "
                         f"{_cell(expiry['contracts'])} |")
    lines.extend(["", "## 担保与名义敞口"])
    if report["risk"] is None:
        lines.append("N/A（未知，不按零计）")
    else:
        lines.extend(["```json", json.dumps(report["risk"], ensure_ascii=False, indent=2, allow_nan=False), "```"])
    lines.extend(["", "## 估值与汇率来源", "```json",
                  json.dumps({"prices": report.get("price_sources"), "fx": report.get("fx_sources")},
                             ensure_ascii=False, indent=2, allow_nan=False), "```"])
    lines.extend(["", "## 本地提交状态"])
    if report["ledger"] is None:
        lines.append("N/A（未确认，不得据此重发）")
    else:
        lines.extend(["```json", json.dumps(report["ledger"], ensure_ascii=False, indent=2, allow_nan=False), "```"])
    lines.extend(["", "## 数据质量与限制"])
    lines.extend(f"- {_cell(issue)}" for issue in report["issues"])
    lines.append("- 策略已实现收益：N/A；不从不完整本地日志推断策略归因。")
    return "\n".join(lines) + "\n"


def write_report(path: str | Path, content: str) -> None:
    """Create only; existing paths require a new user-chosen filename."""
    with Path(path).open("x", encoding="utf-8") as stream:
        stream.write(content)
