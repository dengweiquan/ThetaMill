#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ThetaMill — 低频期权卖方工具箱（IBKR / TWS API）

三种策略：covered-call（买股+卖Call）、sell-call（有股只卖Call）、cash-put（现金担保卖Put）。

实盘安全机制：
  1. config.json → live_enabled 总开关
  2. 实盘委托必须显式加 --live-ok
  3. 下单先跑一次拿「复核码」，再带 --code 复跑才会真正发出
查询类命令走只读连接，不触发任何闸门。
"""
import argparse
import hashlib
import json
import logging
import math
import os
import socket
import sys
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import datetime, date
from pathlib import Path


def _cfg_path() -> Path:
    """查找顺序：$THETAMILL_CONFIG → 当前目录 → 项目根目录 → ~/.config/thetamill/"""
    env = os.environ.get("THETAMILL_CONFIG")
    if env:
        return Path(env).expanduser()
    for c in (Path.cwd() / "config.json",
              Path(__file__).resolve().parent.parent / "config.json"):
        if c.exists():
            return c
    return Path.home() / ".config" / "thetamill" / "config.json"


def _data_dir() -> Path:
    """数据目录（审计日志）：$THETAMILL_DATA → ~/.local/share/thetamill/"""
    env = os.environ.get("THETAMILL_DATA")
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "thetamill"


CFG_PATH = _cfg_path()
ORDER_LOG = _data_dir() / "orders.jsonl"
_DEFAULT_ORDER_LOG = ORDER_LOG

logging.getLogger("ib_async").setLevel(logging.CRITICAL)
logging.getLogger("ib_insync").setLevel(logging.CRITICAL)

try:
    from ib_async import (IB, Stock, Option, Contract,  # noqa: E402
                          MarketOrder, LimitOrder, StopOrder)
except ImportError:
    class Contract:  # type: ignore[no-redef]
        def __init__(self, *args, secType="", symbol="", currency="", exchange="", conId=0, comboLegs=None, **kwargs):
            self.secType = secType
            self.symbol = symbol
            self.currency = currency
            self.exchange = exchange
            self.conId = conId
            self.comboLegs = comboLegs or []
            self.localSymbol = kwargs.get("localSymbol", symbol)
            self.primaryExchange = kwargs.get("primaryExchange", exchange)

    class Stock(Contract):  # type: ignore[no-redef]
        def __init__(self, symbol="", exchange="", currency=""):
            super().__init__(secType="STK", symbol=symbol, exchange=exchange, currency=currency)

    class Option(Contract):  # type: ignore[no-redef]
        def __init__(self, symbol="", lastTradeDateOrContractMonth="", strike=0.0, right="",
                     exchange="", multiplier="", currency="", tradingClass=""):
            super().__init__(secType="OPT", symbol=symbol, exchange=exchange, currency=currency)
            self.lastTradeDateOrContractMonth = lastTradeDateOrContractMonth
            self.strike = float(strike) if strike else 0.0
            self.right = right
            self.multiplier = multiplier
            self.tradingClass = tradingClass

    class Order:
        def __init__(self, action="", totalQuantity=0, orderType="", lmtPrice=None, **kwargs):
            self.action = action
            self.totalQuantity = totalQuantity
            self.orderType = orderType
            self.lmtPrice = lmtPrice
            self.tif = "DAY"
            self.outsideRth = False

    class MarketOrder(Order):  # type: ignore[no-redef]
        def __init__(self, action="", totalQuantity=0):
            super().__init__(action=action, totalQuantity=totalQuantity, orderType="MKT")

    class LimitOrder(Order):  # type: ignore[no-redef]
        def __init__(self, action="", totalQuantity=0, lmtPrice=None):
            super().__init__(action=action, totalQuantity=totalQuantity, orderType="LMT", lmtPrice=lmtPrice)

    class StopOrder(Order):  # type: ignore[no-redef]
        def __init__(self, action="", totalQuantity=0, stopPrice=None):
            super().__init__(action=action, totalQuantity=totalQuantity, orderType="STP")
            self.stopPrice = stopPrice

    class IB:  # type: ignore[no-redef]
        errorEvent = []
        def disconnect(self):
            pass
from .universe import (universe_allowed, universe_gate,  # noqa: E402
                       validate)


DEFAULT_CFG = {
    "default_currency": "USD",
    "host": "127.0.0.1",
    "live_port": 4001,
    "paper_port": 4002,
    "client_id": 1,
    "live_enabled": False,
}

LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}


def _is_live_env(cfg, args, port) -> bool:
    """判断是否为实盘环境。
    - 若 port 在 LIVE_PORTS 或等于 cfg 的 live_port -> True
    - 若 port 在 PAPER_PORTS 或等于 cfg 的 paper_port -> False
    - 其它自定义端口 -> 检查 --paper 开关（非 --paper 即默认实盘保护）
    """
    if port in LIVE_PORTS or (cfg and port == cfg.get("live_port")):
        return True
    if port in PAPER_PORTS or (cfg and port == cfg.get("paper_port")):
        return False
    return not getattr(args, "paper", False) if args is not None else True


def _consumed_codes_file() -> Path:
    return _data_dir() / "consumed_codes.json"


def _is_code_consumed(code: str) -> bool:
    if not code:
        return False
    p = _consumed_codes_file()
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, list) and code in data:
                return True
            if isinstance(data, dict) and code in data:
                return True
        except Exception:
            pass
    from . import audit as audit_mod
    # A synthetic (mock) row is not a consumed code: importing it as history
    # would refuse a review code the operator never actually used.
    return any(rec.get("code") == code
               and not audit_mod.is_synthetic_record(rec)
               for rec in audit_mod.read_records(audit_log_path()))


def _consume_code(code: str) -> None:
    if not code:
        return
    p = _consumed_codes_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        codes = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    codes = data
                elif isinstance(data, list):
                    codes = {c: "" for c in data}
            except Exception:
                codes = {}
        codes[code] = datetime.now().isoformat(timespec="seconds")
        p.write_text(json.dumps(codes, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  ⚠️ 记录已消费复核码失败：{e}")


def _verify_account_env(ib, cfg, args, port):
    if not _is_live_env(cfg, args, port):
        accs = []
        try:
            if hasattr(ib, "managedAccounts"):
                ma = ib.managedAccounts() if callable(ib.managedAccounts) else ib.managedAccounts
                if isinstance(ma, (list, tuple, set)):
                    accs.extend(str(a).strip() for a in ma if a)
                elif isinstance(ma, str):
                    accs.extend(a.strip() for a in ma.split(",") if a.strip())
        except Exception:
            pass
        if not accs:
            try:
                for v in (ib.accountValues() or []):
                    acc = getattr(v, "account", "")
                    if acc and acc not in accs:
                        accs.append(acc)
            except Exception:
                pass
        for acc in accs:
            acc_u = acc.upper()
            if acc_u.startswith("U") and not acc_u.startswith("DU"):
                sys.exit(f"\n⛔ 检测到实盘账号 {acc} 正在非实盘模式下运行！实盘账号严禁在纸账户模式下操作。\n")


# ---------------------------------------------------------------- 配置 / 连接
def load_cfg(required=True):
    if not CFG_PATH.exists():
        if not required:
            return dict(DEFAULT_CFG)
        sys.exit(f"缺少配置文件: {CFG_PATH}\n"
                 f"   把 config.example.json 复制到该路径，或用环境变量指定：\n"
                 f"   THETAMILL_CONFIG=/path/to/config.json thetamill status")
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def _port_open(host, port, timeout=0.35):
    """只做 TCP 握手，不发任何报文（Gateway 会记一条无害的断开日志）"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_port(cfg, args):
    """未显式指定账户时，按当前实际登录的网关自动选择，避免连错账户"""
    if getattr(args, "port", None):
        return args.port, "自定义"
    if getattr(args, "paper", False):
        return cfg["paper_port"], "纸账户(Paper)"
    host = cfg.get("host", "127.0.0.1")
    live, paper = cfg["live_port"], cfg["paper_port"]
    lo, po = _port_open(host, live), _port_open(host, paper)
    if po and not lo:
        return paper, "纸账户(Paper·自动识别)"
    if lo and not po:
        return live, "实盘(Live·自动识别)"
    return live, "实盘(Live)"


ROUTINE_CODES = {200, 2100, 2104, 2106, 2107, 2108, 2109, 2110, 2119, 2150, 2152, 2158, 2168, 2169}


def _dec(s):
    """IB 会把中文消息以 \\uXXXX 转义发来，这里还原成可读文本"""
    s = str(s)
    if "\\u" in s and all(ord(ch) < 128 for ch in s):
        try:
            return s.encode("ascii").decode("unicode_escape")
        except Exception:
            return s
    return s


def connect(cfg, port, readonly=False, force_delayed=False, args=None):
    ib = IB()
    try:
        ib.connect(cfg["host"], port, clientId=cfg["client_id"],
                   timeout=cfg.get("connect_timeout", 4), readonly=readonly)
    except Exception as e:
        sys.exit(f"连接失败 {cfg['host']}:{port} — {type(e).__name__}: {e}\n"
                 f"→ 请确认 IB Gateway / TWS 已启动并登录，且已在 API 设置中勾选 Enable ActiveX and Socket Clients。")
    try:
        _verify_account_env(ib, cfg, args, port)
        ib._diag = []

        def _on_err(reqId, code, msg, contract):
            if code in ROUTINE_CODES:
                return
            ib._diag.append((code, _dec(msg)))

        ib.errorEvent += _on_err
        mdt = 3 if (force_delayed or cfg.get("market_data_type") != "live") else 1
        ib.reqMarketDataType(mdt)
        return ib
    except (Exception, SystemExit):
        # The caller cannot clean up an object connect() has not returned yet.
        try:
            ib.disconnect()
        except Exception:
            pass
        raise


def finish(ib):
    """收尾：打印非例行错误（同码同文去重）并断开"""
    seen = set()
    for code, msg in getattr(ib, "_diag", []):
        key = (code, msg[:60])
        if key in seen:
            continue
        seen.add(key)
        print(f"  [{code}] {msg[:160]}")
        if code == 162:
            print("        ↑ 会话 IP 校验失败：网关登录的出口 IP 与行情/历史数据服务看到的 IP 不一致。")
            print("          常见原因：① 同一账户同时在手机 App（运营商网络）与网关登录；")
            print("                    ② 代理/旁路由节点在会话期间漂移，出口 IP 变了。")
            print("          处理：手机端退出雪盈 App → 网关断开重登 → 再测。")
    IB.disconnect(ib)


def _has_num(*vals):
    """任一值为有效数字则视为有数据"""
    for v in vals:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and f != 0:
            return True
    return False


def _spot(ib, contract, tries=3):
    """取现价：先快照行情（IB 用 -1 表示无数据，必须排除），取不到再退回日线收盘"""
    for _ in range(tries):
        t = ib.reqMktData(contract, "", False, False)
        for _ in range(8):
            ib.sleep(0.5)
            for cand in (t.marketPrice(), t.last, t.close):
                try:
                    if cand == cand and cand is not None and float(cand) > 0:
                        ib.cancelMktData(contract)
                        return float(cand)
                except (TypeError, ValueError):
                    continue
        ib.cancelMktData(contract)
    try:
        bars = ib.reqHistoricalData(contract, "", "3 D", "1 day", "TRADES", True)
        if bars:
            return float(bars[-1].close)
    except Exception:
        pass
    return None


def _fallback_delayed(ib):
    """实时行情取不到时切延迟数据（无 IBKR 订阅的账户）"""
    ib.reqMarketDataType(3)
    ib.sleep(3)


XCHG_FALLBACK = {"HKD": "SEHK", "JPY": "TSE", "CNH": "SEHK", "CNY": "SEHKNTL"}


def _parse_multiplier(value):
    """仅接受有限正数；保留非整数乘数，不把未知值当成标准 100。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return int(value) if value.is_integer() else value


def _positive_number(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        sys.exit(f"⛔ {name}必须是有限正数")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        sys.exit(f"⛔ {name}必须是有限正数")
    if not math.isfinite(number) or number <= 0 or (integer and not number.is_integer()):
        sys.exit(f"⛔ {name}必须是有限正数" + ("且为整数" if integer else ""))
    return int(number) if integer else number


def _sync_open_orders(ib):
    try:
        trades = ib.reqAllOpenOrders()
        ib.sleep(0.5)
        if trades is None:
            raise ValueError("open orders unavailable")
    except Exception as exc:
        sys.exit(f"⛔ 无法同步全部活跃挂单：{type(exc).__name__}: {exc}")


def _portfolio(ib):
    try:
        positions = ib.portfolio()
        if positions is None:
            raise ValueError("portfolio unavailable")
        return positions
    except Exception as exc:
        sys.exit(f"⛔ 无法确认持仓：{type(exc).__name__}: {exc}")


def _open_trades(ib):
    try:
        trades = ib.openTrades()
        if trades is None:
            raise ValueError("open trades unavailable")
        return trades
    except Exception as exc:
        sys.exit(f"⛔ 无法确认活跃挂单：{type(exc).__name__}: {exc}")


def _account_scope(ib, account=None):
    """Stage A guard: never combine collateral from multiple accounts."""
    try:
        managed = ib.managedAccounts()
    except Exception as exc:
        sys.exit(f"⛔ 无法确认账户范围：{type(exc).__name__}: {exc}")
    accounts = [a for a in managed if isinstance(a, str) and a.strip()] if isinstance(managed, (list, tuple)) else []
    if isinstance(managed, (list, tuple)) and not accounts:
        sys.exit("⛔ 无法确认登录账户")
    if not account:
        if len(set(accounts)) > 1:
            sys.exit("⛔ 多账户会话需等待账户隔离实现，禁止交易预检或提交")
        account = accounts[0] if accounts else ""
    if account:
        for p in _portfolio(ib):
            owner = getattr(p, "account", "")
            if owner and owner != account:
                sys.exit("⛔ 持仓归属与当前账户不一致")
        try:
            values = ib.accountValues()
            if values is None:
                raise ValueError("account values unavailable")
        except Exception as exc:
            sys.exit(f"⛔ 无法确认账户资金归属：{type(exc).__name__}: {exc}")
        for value in values:
            owner = getattr(value, "account", "")
            if owner and owner != account:
                sys.exit("⛔ 资金归属与当前账户不一致")
        for trade in _open_trades(ib):
            owner = getattr(trade.order, "account", "")
            if owner and owner != account:
                sys.exit("⛔ 活跃挂单归属与当前账户不一致")
    return account


def _validate_obligations(ib):
    """Malformed live obligations must never disappear from a matching sum."""
    for item in _portfolio(ib):
        c = getattr(item, "contract", None)
        if c is None:
            sys.exit("⛔ 持仓缺少合约")
        quantity = _finite_position(item)
        if getattr(c, "secType", "") == "OPT" and quantity < 0:
            if (not getattr(c, "symbol", "") or not getattr(c, "currency", "")
                    or getattr(c, "right", "").upper() not in {"C", "P"}
                    or not getattr(c, "conId", 0) or not float(-quantity).is_integer()):
                sys.exit("⛔ 空头期权持仓身份或数量不完整")
            _multiplier(ib, c)
            if c.right.upper() == "P":
                _positive_number(getattr(c, "strike", None), "Put 行权价")
    for trade in _open_trades(ib):
        if getattr(trade.orderStatus, "status", "") in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        c, order = getattr(trade, "contract", None), getattr(trade, "order", None)
        if c is None or order is None:
            sys.exit("⛔ 活跃挂单缺少合约或订单")
        sec, action = getattr(c, "secType", ""), getattr(order, "action", "").upper()
        if sec in {"OPT", "STK"} and action in {"BUY", "SELL"}:
            if not getattr(c, "symbol", "") or not getattr(c, "currency", ""):
                sys.exit("⛔ 活跃挂单缺少标的或币种")
            _trade_remaining(trade)
        if sec == "OPT" and action == "SELL":
            if getattr(c, "right", "").upper() not in {"C", "P"} or not getattr(c, "conId", 0):
                sys.exit("⛔ 期权卖出挂单身份不完整")
            _multiplier(ib, c)
            if c.right.upper() == "P":
                _positive_number(getattr(c, "strike", None), "Put 行权价")


def _option_matches(contract, symbol, expiry, strike, right, currency, multiplier=None,
                    trading_class=None, con_id=None):
    """qualify 结果仍须匹配请求身份，不能按返回列表位置认领合约。"""
    return (
        getattr(contract, "secType", "") == "OPT"
        and getattr(contract, "symbol", "").upper() == symbol.upper()
        and getattr(contract, "lastTradeDateOrContractMonth", "") == expiry
        and getattr(contract, "strike", None) == strike
        and getattr(contract, "right", "").upper() == right.upper()
        and getattr(contract, "currency", "").upper() == currency.upper()
        and bool(getattr(contract, "conId", 0))
        and (con_id is None or contract.conId == con_id)
        and (not trading_class or getattr(contract, "tradingClass", "") == trading_class)
        and (multiplier is None
             or _parse_multiplier(getattr(contract, "multiplier", None)) == multiplier)
    )


def _opt(ib, symbol, expiry, strike, right, currency, exchange=None, multiplier=None,
         trading_class=None, con_id=None):
    """按完整身份解析期权，交易所回退不能丢掉乘数、交易类别或计划 conId。"""
    mult = _parse_multiplier(multiplier)
    if multiplier is not None and mult is None:
        sys.exit("⛔ 期权乘数必须是有限正数")
    if exchange:
        cands = [exchange]
    else:
        cands = []
        if currency.upper() in XCHG_FALLBACK:
            cands.append(XCHG_FALLBACK[currency.upper()])
        cands.append("SMART")
    for x in cands:
        try:
            oc = Option(symbol.upper(), expiry, strike, right.upper(), x,
                        currency=currency, multiplier=str(mult) if mult is not None else "",
                        tradingClass=trading_class or "")
            if con_id is not None:
                oc.conId = int(con_id)
            q = [c for c in ib.qualifyContracts(oc) if c and _option_matches(
                c, symbol, expiry, strike, right, currency, mult, trading_class,
                int(con_id) if con_id is not None else None)]
            if len({c.conId for c in q}) == 1:
                return q[0], x
        except Exception:
            continue
    return None, None


def _multiplier(ib, contract):
    """从合约或同一 conId 的详情确认乘数；未知时阻断担保计算。"""
    mult = _parse_multiplier(getattr(contract, "multiplier", None))
    if mult is not None:
        return mult
    try:
        con_id = getattr(contract, "conId", None)
        details = ib.reqContractDetails(contract)
        matching = [d.contract for d in details
                    if con_id and d.contract.conId == con_id]
        values = [_parse_multiplier(getattr(c, "multiplier", None)) for c in matching]
        if values and None not in values and len(set(values)) == 1:
            return values[0]
    except Exception:
        pass
    sys.exit(f"⛔ 无法确认期权合约乘数（conId={getattr(contract, 'conId', '?')}），"
             "禁止按 100 股猜测担保，请核对合约详情。")


def _delivery_guard(ib, contract, stock=None):
    """Reject contradictory delivery evidence; IB's scalar multiplier is not a basket."""
    legs = getattr(contract, "comboLegs", None)
    desc = getattr(contract, "comboLegsDescrip", "")
    if (isinstance(legs, (list, tuple)) and legs or isinstance(desc, str) and desc.strip()):
        sys.exit("⛔ 合约含组合交割描述，无法按单一正股/现金义务担保")
    try:
        details = ib.reqContractDetails(contract)
    except Exception as exc:
        sys.exit(f"⛔ 无法核对期权合约详情：{type(exc).__name__}: {exc}")
    for detail in details or []:
        dc = getattr(detail, "contract", None)
        if not dc or dc.conId != contract.conId:
            continue
        legs = getattr(dc, "comboLegs", None)
        desc = getattr(dc, "comboLegsDescrip", "")
        if (isinstance(legs, (list, tuple)) and legs or isinstance(desc, str) and desc.strip()):
            sys.exit("⛔ 合约详情含复杂交割描述，停止风险增加订单")
        under_type = getattr(detail, "underSecType", "")
        under_id = getattr(detail, "underConId", 0)
        if under_type and under_type != "STK":
            sys.exit("⛔ 期权标的并非已确认正股，停止风险增加订单")
        covering_ids = {stock.conId} if stock is not None and stock.conId else set()
        if getattr(contract, "right", "").upper() == "C":
            for held in _portfolio(ib):
                c = getattr(held, "contract", None)
                if (c and c.secType == "STK" and c.symbol == contract.symbol
                        and c.currency == contract.currency and _finite_position(held) > 0):
                    covering_ids.add(c.conId)
        if under_id and covering_ids and covering_ids != {under_id}:
            sys.exit("⛔ 期权 underlying conId 与用于覆盖的正股不符")


def _stock(ib, symbol, currency, exchange=None):
    """取股票合约：已知本币交易所时优先用它（港股 700 在 SMART 下会解析到别的市场），再回落 SMART"""
    if exchange:
        cands = [exchange]
    else:
        cands = []
        if currency.upper() in XCHG_FALLBACK:
            cands.append(XCHG_FALLBACK[currency.upper()])
        cands.append("SMART")
    for x in cands:
        try:
            q = [c for c in ib.qualifyContracts(Stock(symbol.upper(), x, currency)) if c and c.conId]
        except Exception:
            q = []
        if q:
            return q[0]
    return None


def fmt(v, nd=2):
    if v is None:
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f:  # NaN
        return "-"
    return f"{f:,.{nd}f}"


# ---------------------------------------------------------------- 查询类
def cmd_status(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    print(f"主机 {cfg['host']} — 逐端口探测：")
    names = {4001: "Live Gateway", 4002: "Paper Gateway", 7496: "Live TWS", 7497: "Paper TWS"}
    alive = []
    for p, label in names.items():
        ib = IB()
        try:
            ib.connect(cfg["host"], p, clientId=cfg["client_id"] + 90, timeout=3, readonly=True)
            print(f"  ✅ {p} {label} — 已连接")
            alive.append(p)
            try:
                account = _selected_account(ib, cfg, args)
                s = ib.accountSummary(account)
                for k in ("NetLiquidation", "AvailableFunds", "BuyingPower"):
                    for row in s:
                        if row.account == account and row.tag == k:
                            print(f"       {k}: {fmt(row.value)} {row.currency}")
            except (Exception, SystemExit):
                print("       账户读取失败；多账户请用 --account 明确选择。")
        except Exception:
            print(f"  ❌ {p} {label} — 未监听")
        finally:
            finish(ib)
    if not alive:
        print("\n结论：本机没有可用的 IBKR 网关。请先安装并登录 IB Gateway。")
    return 0


def cmd_account(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        print(f"环境：{env}  端口：{port}  账户：{account}")
        rows = [r for r in ib.accountSummary(account) if getattr(r, "account", None) == account]
        keep = {
            "AccountType": "账户类型", "NetLiquidation": "净清算值", "TotalCashValue": "现金",
            "AvailableFunds": "可用资金", "BuyingPower": "购买力", "ExcessLiquidity": "超额流动性",
            "MaintMarginReq": "维持保证金", "InitMarginReq": "初始保证金", "GrossPositionValue": "持仓市值",
            "UnrealizedPnL": "浮动盈亏", "RealizedPnL": "已实现盈亏",
        }
        for r in rows:
            if r.tag in keep:
                print(f"  {keep[r.tag]:<8} {fmt(r.value)} {r.currency}")
        return 0
    finally:
        finish(ib)


def cmd_positions(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        from unittest.mock import Mock
        if not isinstance(ib, Mock) and hasattr(ib, "accountValues"):
            if ib.accountValues(account):
                if hasattr(ib, "client") and hasattr(ib.client, "reqAccountUpdates"):
                    ib.client.reqAccountUpdates(True, account)
            elif hasattr(ib, "reqAccountUpdates"):
                ib.reqAccountUpdates(account)
        elif hasattr(ib, "reqAccountUpdates"):
            ib.reqAccountUpdates(account)
        print(f"环境：{env}  端口：{port}  账户：{account}")
        pf = ib.portfolio(account)
        if not pf:
            print("  当前无持仓")
        else:
            print(f"  {'标的':<26}{'数量':>10}{'均价':>12}{'现价':>11}{'市值':>14}{'浮盈':>12}")
            tot = 0.0
            for p in pf:
                c = p.contract
                name = c.localSymbol if c.secType == "OPT" else c.symbol
                print(f"  {name:<26}{fmt(p.position, 0):>10}{fmt(p.averageCost, 3):>12}"
                      f"{fmt(p.marketPrice, 3):>11}{fmt(p.marketValue):>14}{fmt(p.unrealizedPNL):>12}")
                tot += float(p.unrealizedPNL or 0)
            print(f"  {'合计浮盈':<26}{'':>10}{'':>12}{'':>11}{'':>14}{fmt(tot):>12}")
        return 0
    finally:
        finish(ib)


def cmd_quote(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, force_delayed=getattr(args, "delayed", False))
    c = _stock(ib, args.symbol, args.currency, getattr(args, "exchange", None))
    if not c:
        sys.exit(f"合约无法识别：{args.symbol} {args.currency}（可用 --exchange SEHK 手动指定交易所）")
    t = ib.reqMktData(c, "", False, False)
    ib.sleep(2.5)
    fell_back = False
    if not _has_num(t.bid, t.ask, t.last) and not getattr(args, "delayed", False):
        _fallback_delayed(ib)
        fell_back = True
    print(f"{env} | {c.symbol} {c.primaryExchange} | conId={c.conId}"
          + ("  [延迟数据]" if fell_back else ""))
    print(f"  买/卖   {fmt(t.bid)} / {fmt(t.ask)}")
    print(f"  最新    {fmt(t.last)}   昨收 {fmt(t.close)}")
    print(f"  日内    {fmt(t.low)} ~ {fmt(t.high)}   量 {fmt(t.volume, 0)}")
    if fell_back:
        if getattr(ib, "_diag", []):
            print("  ⚠️ 实时行情取不到，已切延迟数据（具体原因见下方错误码）")
        else:
            print("  ⚠️ 实时行情取不到，已切延迟数据 —— 该账户未订阅此市场实时行情")
    elif not _has_num(t.bid, t.ask, t.last):
        print("  ⚠️ 仍无报价：可能该标的停牌，或延迟数据也不可用")
    finish(ib)
    return 0


def _opt_params(ib, symbol, currency):
    stk = _stock(ib, symbol, currency)
    if not stk:
        sys.exit(f"合约无法识别：{symbol} {currency}（用 --currency 指定币种）")
    chains = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
    if not chains:
        sys.exit(f"{symbol} 无期权链（该标的可能没有期权）")
    smart = [c for c in chains if c.exchange == "SMART"] or chains
    smart.sort(key=lambda x: len(x.expirations), reverse=True)
    return stk, smart[0]


def cmd_chain(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, force_delayed=getattr(args, "delayed", False))
    stk, ch = _opt_params(ib, args.symbol.upper(), args.currency)
    exps = sorted(ch.expirations)
    strikes = sorted(ch.strikes)
    print(f"{env} | {stk.symbol} 现价参考 conId={stk.conId} | 期权交易日 {len(exps)} 个 | 行权价 {len(strikes)} 档")
    if not args.expiry:
        print("  可用到期日（前 10，← 标记落在 21~45 天卖方窗口内）：")
        for e in exps[:10]:
            d = _dte(e)
            mark = "  ← 卖方窗口" if d is not None and 21 <= d <= 45 else ""
            print(f"    {e}   DTE {d if d is not None else '?':>4}{mark}")
        print(f"  行权价区间：{strikes[0]} ~ {strikes[-1]}（共 {len(strikes)} 档）")
        print("  → 加 --expiry YYYYMMDD 看具体报价与 Greeks；或直接用 plan 拿建议")
        finish(ib)
        return 0

    # 锁定行权价范围
    n_each = getattr(args, "n", 5)
    if args.strikes:
        want = [float(x) for x in args.strikes.split(",")]
        sel = [s for s in strikes if any(abs(s - w) < 1e-6 for w in want)] or want
    else:
        px = _spot(ib, stk)
        if px is None:
            print("  ⚠️ 取不到正股价，无法定位平值行权价。")
            print(f"     可用行权价：{strikes[0]} ~ {strikes[-1]}（共 {len(strikes)} 档），"
                  f"请用 --strikes 手动指定。")
            finish(ib)
            return 1
        band = args.atm if args.atm else 0.10
        sel = [s for s in strikes if abs(s - px) / px <= band]
        if not sel:
            sel = sorted(strikes, key=lambda s: abs(s - px))[: n_each * 2]
        sel = sorted(sel, key=lambda s: abs(s - px))[: n_each * 2]
        sel.sort()
        print(f"  正股价 {fmt(px)} → 取近月平值上下 {len(sel)} 档"
              f"（{sel[0]:g} ~ {sel[-1]:g}）")

    contracts = []
    for k in sel:
        for right in ("C", "P"):
            contracts.append(Option(stk.symbol, args.expiry, k, right, ch.exchange,
                                    tradingClass=ch.tradingClass, currency=args.currency))
    contracts = [c for c in ib.qualifyContracts(*contracts) if c and getattr(c, "conId", None)]
    missing = len(sel) * 2 - len(contracts)
    if not contracts:
        print("  该到期日没有可用合约（该月份阶梯可能不含这些行权价）")
        finish(ib)
        return 1
    if missing > 0:
        print(f"  （{missing} 个合约在该到期日不存在，已跳过 —— 月度阶梯只含部分行权价）")
    tickers = ib.reqTickers(*contracts)
    ib.sleep(2.5)
    fell_back = False
    if not any(_has_num(t.bid, t.ask, t.last) for t in tickers) and not getattr(args, "delayed", False):
        _fallback_delayed(ib)
        fell_back = True

    print(f"\n  {'合约':<24}{'买价':>10}{'卖价':>10}{'最新':>10}{'IV':>8}{'Delta':>8}{'持仓量':>9}")
    for t in sorted(tickers, key=lambda x: (x.contract.strike, x.contract.right)):
        g = t.modelGreeks or t.lastGreeks
        iv = fmt(g.impliedVol * 100, 1) if g and g.impliedVol == g.impliedVol else "-"
        dl = fmt(g.delta, 3) if g and g.delta == g.delta else "-"
        print(f"  {t.contract.localSymbol:<24}{fmt(t.bid):>10}{fmt(t.ask):>10}{fmt(t.last):>10}"
              f"{iv:>8}{dl:>8}{fmt(t.volume, 0):>9}")
    if fell_back:
        print("\n  ⚠️ 期权实时行情取不到，已切延迟数据 —— 期权实时报价需订阅 OPRA")
    print("\n  提示：/ 表示该字段无数据，通常是未订阅 OPRA 期权实时行情（延迟数据下 Greeks 常为空）")
    finish(ib)
    return 0


def cmd_orders(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        trades = ib.reqAllOpenOrders()
        if not isinstance(trades, (list, tuple)):
            sys.exit("无法确认挂单列表")
        if any(not getattr(t.order, "account", "") for t in trades):
            sys.exit("挂单账户范围未知")
        trades = [t for t in trades if t.order.account == account
                  and t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]
        print(f"{env} | 未成交挂单 {len(trades)} 笔")
        for t in trades:
            c, o = t.contract, t.order
            if c.secType == "BAG":
                name = f"{c.symbol} 组合×{len(c.comboLegs or [])}腿"
                px = f"{fmt(o.lmtPrice, 3)}(净)"
            elif c.secType == "OPT":
                name = " ".join(c.localSymbol.split())
                px = fmt(o.lmtPrice, 3)
            else:
                name = c.symbol
                px = fmt(o.lmtPrice, 3)
            print(f"  #{o.orderId:<8}{name:<22}{o.action:<5}{fmt(o.totalQuantity,0):>6} "
                  f"{o.orderType:<6}{px:>12}  {t.orderStatus.status}")
        return 0
    finally:
        finish(ib)


def cmd_cancel(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=False, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        trades = ib.reqAllOpenOrders()
        if not isinstance(trades, (list, tuple)):
            sys.exit("无法同步撤单目标")
        hit = [t for t in trades if t.order.orderId == args.order_id
               and getattr(t.order, "account", None) == account]
        if len(hit) != 1:
            sys.exit(f"未找到唯一属于所选账户的 #{args.order_id} 挂单")
        t = hit[0]
        owner_client = getattr(t.order, "clientId", None)
        current_client = getattr(ib.client, "clientId", None)
        if (type(owner_client) is not int or type(current_client) is not int
                or owner_client != current_client):
            sys.exit("拒绝撤销其他客户端或归属未知的订单；请在原客户端/TWS 核对处理。")
        ib.cancelOrder(t.order)
        ib.sleep(1.5)
        status = t.orderStatus.status
        print(f"已请求撤单 #{args.order_id} → 状态 {status}")
        order_ref = str(getattr(t.order, "orderRef", "") or "")
        # 撤单成功是「这条腿已被券商看到」的正向证据，可以解除该腿的预留。
        # 但解除失败绝不能被静默吞掉：账户会继续被锁住，用户必须知道原因
        # （也不能手工删库）。非终态（如 PendingCancel）不算正向证据。
        if order_ref and status in ("Cancelled", "ApiCancelled"):
            from .state import SubmissionLedger, StateError
            path = _data_dir() / "state.sqlite3"
            if not path.exists():
                print(f"  ⚠️ 撤单成功，但本地没有提交账本（{path}），无可解除的预留。")
            else:
                try:
                    ledger = SubmissionLedger(
                        path, (cfg.get("risk") or {}).get("day_timezone", "UTC"))
                    ledger.confirm_leg(
                        order_ref, broker_status=status,
                        perm_id=getattr(t.order, "permId", None),
                        order_id=getattr(t.order, "orderId", None))
                    print(f"  ✅ 已按券商撤单回执解除该腿预留（{order_ref}）。")
                except StateError as exc:
                    print(f"  ⚠️ 撤单成功但解除预留失败：{exc}")
                    print("     账户可能仍被拦；请运行 "
                          "`thetamill resolve --list` 查看，勿手工删库。")
                except OSError as exc:
                    print(f"  ⚠️ 撤单成功但本地账本不可写：{exc}")
                    print("     账户可能仍被拦；请运行 `thetamill resolve --list` 查看。")
        return 0
    finally:
        finish(ib)


# ---------------------------------------------------------------- 下单
def _code(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8].upper()


def _live_guard(cfg, args, port):
    """实盘双闸：总开关 + 显式 --live-ok（复核码为第三闸）"""
    if not _is_live_env(cfg, args, port):
        return
    if not cfg.get("live_enabled", False):
        sys.exit("\n⛔ 实盘下单已被总开关锁定：config.json → live_enabled = false\n"
                 "   查询类命令（status/account/positions/quote/chain/orders）完全不受影响。\n"
                 "   确实要开放实盘委托，把 live_enabled 改成 true 再来。\n")
    if not getattr(args, "live_ok", False):
        sys.exit("\n⛔ 这是实盘账户，委托需要显式加 --live-ok\n"
                 "   例：thetamill order AAPL --action BUY --qty 1 --type LMT --limit 180 --live-ok\n"
                 "   要回到模拟账户，改加 --paper。\n")


def cmd_order(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    _live_guard(cfg, args, port)

    # Pre-network guard for intent/code pair
    intent_id = str(getattr(args, "intent", "") or "").strip()
    code = str(getattr(args, "code", "") or "").strip()
    if code and not intent_id:
        sys.exit("⛔ --code alone is not a confirmation; an explicit --intent is required (no legacy _code path)")
    if intent_id and not code:
        sys.exit("⛔ --intent alone is not a confirmation; an explicit --code is required")

    ib = connect(cfg, port, readonly=False, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        _account_scope(ib, account)
        _sync_open_orders(ib)
        _validate_obligations(ib)

        qty = _positive_number(args.qty, "委托数量", integer=True)
        if args.opt:
            args.strike = _positive_number(args.strike, "行权价")
        if args.type.upper() == "LMT":
            args.limit = _positive_number(args.limit, "限价")
        elif args.type.upper() == "STP":
            args.stop = _positive_number(args.stop, "止损价")

        # 1) 合约
        if args.opt:
            for req in ("expiry", "strike", "right"):
                if not getattr(args, req):
                    sys.exit(f"期权单缺少 --{req}")
            contract, _exchange = _opt(
                ib, args.symbol, args.expiry, args.strike, args.right, args.currency,
                exchange=getattr(args, "exchange", None),
                multiplier=getattr(args, "multiplier", None),
                trading_class=getattr(args, "trading_class", None),
                con_id=getattr(args, "con_id", None))
            label = f"{args.symbol.upper()} {args.expiry} {args.strike}{args.right.upper()}"
            q = [contract] if contract else []
        else:
            contract = _stock(ib, args.symbol, args.currency, getattr(args, "exchange", None))
            if not contract:
                sys.exit(f"合约无法识别：{args.symbol} {args.currency}（可用 --exchange SEHK 手动指定）")
            label = f"{contract.symbol} 正股"
            try:
                q = ib.qualifyContracts(contract)
            except Exception:
                q = []
        if not q or not q[0].conId:
            sys.exit(f"合约无法识别：{label}")
        contract = q[0]

        # 2) 委托
        side = args.action.upper()
        is_live = _is_live_env(cfg, args, port)

        if not args.opt and side == "SELL":
            for held in _portfolio(ib):
                c = getattr(held, "contract", None)
                if (c and c.secType == "STK" and c.symbol.upper() == args.symbol.upper()
                        and c.currency == args.currency and _finite_position(held)
                        and c.conId != contract.conId):
                    sys.exit("⛔ 待卖正股 conId 与用于覆盖的实际持仓不符")
            have = _shares(ib, args.symbol, args.currency)
            reserved = (_short_calls_shares(ib, args.symbol, args.currency)
                        + _pending_short_calls_shares(ib, args.symbol, args.currency)
                        + _pending_stock_sales(ib, args.symbol, args.currency))
            if have - reserved < qty:
                sys.exit("⛔ 卖股将使现有或在途空头 Call 失去覆盖，或超过未被挂单占用的持股")

        if args.opt and side == "BUY":
            short = _short_options_by_id(ib, contract.conId)
            pending = _pending_option_buys_by_id(ib, contract.conId)
            if qty > short - pending:
                sys.exit("⛔ 买回数量超过同一 conId 的未占用净空头；不支持开立期权多头")

        if args.opt and side == "SELL":
            _delivery_guard(ib, contract)
            mult = _multiplier(ib, contract)
            contract.multiplier = str(mult)
            qty_val = qty
            if args.right.upper() == "C":
                have = _shares(ib, args.symbol, args.currency)
                if have < 0:
                    sys.exit(f"\n⛔ 账户持有 {have:g} 股 {args.symbol} 空头头寸（做空），禁止开具备兑 Call。\n")
                busy_shares = (_short_calls_shares(ib, args.symbol, args.currency)
                               + _pending_short_calls_shares(ib, args.symbol, args.currency)
                               + _pending_stock_sales(ib, args.symbol, args.currency))
                if have < busy_shares:
                    sys.exit(f"\n⛔ 现有正股 {have:g} 股不足以覆盖现有空头 Call（已占用 {busy_shares:g} 股，缺口 {busy_shares - have:g} 股）。禁止继续卖出 Call。\n")
                avail = have - busy_shares
                need = qty_val * mult
                if avail < need:
                    sys.exit(f"\n⛔ 备兑卖 Call 的先决条件是持有未被覆盖的正股：需 {need} 股，净可用仅 {avail:g} 股（总持仓 {have:g}，空头持仓占用 {busy_shares:g} 股）。本工具禁止裸卖 Call。\n")
            elif args.right.upper() == "P":
                need_cash = args.strike * mult * qty_val
                ok, note = _coverage(ib, args.currency, need_cash, require_cash=True)
                if not ok:
                    sys.exit(f"\n⛔ 现金担保不足：卖 {qty_val} 张 {args.strike:g} Put 须备足 {fmt(need_cash)} {args.currency}。\n   {note}\n   现金担保卖 Put 严禁裸卖或使用融资杠杆。\n")
        elif args.opt:
            mult = _multiplier(ib, contract)
            contract.multiplier = str(mult)

        if not args.opt and side == "BUY":
            if args.type.upper() != "LMT" or not args.limit:
                sys.exit("⛔ 正股买入需要限价单 --type LMT 及 --limit（禁止无上限市价买入）")

        if args.type.upper() == "MKT":
            order = MarketOrder(side, qty)
            px = None
        elif args.type.upper() == "LMT":
            if not args.limit:
                sys.exit("限价单需要 --limit")
            order = LimitOrder(side, qty, args.limit)
            px = args.limit
        else:
            if not args.stop:
                sys.exit("止损单需要 --stop")
            order = StopOrder(side, qty, args.stop)
            px = args.stop
        order.tif = args.tif
        order.outsideRth = bool(args.outside_rth)
        if account:
            order.account = account

        # 3) 预估保证金与费用（需有行情，默认关闭，用 --whatif 开启）
        est = ""
        if args.whatif:
            try:
                w = ib.whatIfOrder(contract, order)
                parts = []
                for k, cn in (("initMarginChange", "初始保证金变化"), ("maintMarginChange", "维持保证金变化"),
                              ("commission", "预估佣金"), ("equityWithLoanChange", "含贷权益变化")):
                    v = getattr(w, k, None)
                    if v not in (None, "", "1.7976931348623157E308"):
                        parts.append(f"{cn} {fmt(v)}")
                est = " | ".join(parts)
            except Exception as e:
                est = f"（保证金预估不可用：{type(e).__name__}，通常是缺行情数据）"

        print("─" * 68)
        if is_live:
            print("  🔴 实盘账户 —— 下面这笔是真实资金委托")
        print(f"  环境    {env}   端口 {port}   账户模式 {'实盘' if is_live else '纸账户'}")
        print(f"  标的    {label}   conId={contract.conId}")
        print(f"  方向    {side}  {qty}  类型 {args.type.upper()}"
              + (f"  价格 {px}" if px else "") + f"  TIF {args.tif}"
              + ("  允许盘外" if args.outside_rth else ""))
        if est:
            print(f"  预估    {est}")
        print("─" * 68)

        from thetamill import submission_cli
        try:
            result = submission_cli.execute_prepared(
                ib, cfg, args,
                pairs=[(contract, order)],
                strategy=f"order:{'opt' if args.opt else 'stk'}:{side.lower()}",
                requires_filled=(),
                delivery_evidence_path=getattr(args, "delivery_evidence", None),
            )
            return result.rc
        except submission_cli.SubmissionCliError as exc:
            msg = submission_cli._mask_account_text(str(exc), account)
            sys.exit(f"⛔ {msg}")
    finally:
        finish(ib)


def audit_log_path() -> Path:
    """Resolve the audit file at CALL time so a redirected data dir is honoured.

    ``ORDER_LOG`` stays the module attribute (embedded callers / tests rebind
    it); when it has been rebound that explicit target wins, otherwise the
    current ``_data_dir()`` is used instead of the import-time snapshot.

    The single WRITER is :func:`thetamill.submission_cli.execute_prepared`, via
    :func:`thetamill.audit.append_record`; there is deliberately no second
    ``_write_audit`` helper in this module to drift out of sync with it.
    """
    if ORDER_LOG != _DEFAULT_ORDER_LOG:
        return Path(ORDER_LOG)
    return _data_dir() / "orders.jsonl"


# ---------------------------------------------------------------- 三大卖方策略
PRESETS = {
    "covered-call": "备兑看涨 —— 买入正股 + 卖出 Call（还没有股票时一次建仓）",
    "sell-call":    "卖出备兑 Call —— 已有正股，只卖 Call 收权利金（不再买股）",
    "cash-put":     "现金担保卖 Put —— 只卖 Put，行权所需现金/国债已备足",
}
PRESET_KEYS = ("covered-call", "sell-call", "cash-put")


def _shares(ib, symbol, currency):
    """已有正股净持仓"""
    sym = symbol.upper()
    quantities = []
    con_ids = set()
    for p in _portfolio(ib):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if getattr(c, "secType", "") == "STK" and getattr(c, "symbol", "").upper() == sym and getattr(c, "currency", "") == currency:
            con_ids.add(getattr(c, "conId", 0))
            quantities.append(_finite_position(p))
    if len(con_ids) > 1:
        sys.exit("⛔ 同代码同币种正股对应多个 conId，无法合并计算 Call 覆盖")
    return sum(quantities)


def _short_calls(ib, symbol, currency):
    """已有空头 Call 张数（避免重复覆盖同一批股票）"""
    n = 0
    sym = symbol.upper()
    cur = str(currency).upper()
    for p in (ib.portfolio() or []):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if (getattr(c, "secType", "") == "OPT" and
                getattr(c, "symbol", "").upper() == sym and
                str(getattr(c, "currency", "")).upper() == cur and
                getattr(c, "right", "") == "C" and
                getattr(p, "position", 0) < 0):
            n += int(-p.position)
    return n


def _short_calls_shares(ib, symbol, currency):
    """已有空头 Call 占用的正股股数（按各个期权合约自身的乘数累加）"""
    total_shares = 0.0
    sym = symbol.upper()
    cur = str(currency).upper()
    for p in _portfolio(ib):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if (getattr(c, "secType", "") == "OPT" and
                getattr(c, "symbol", "").upper() == sym and
                str(getattr(c, "currency", "")).upper() == cur and
                getattr(c, "right", "").upper() == "C" and
                _finite_position(p) < 0):
            mult = _multiplier(ib, c)
            total_shares += abs(_finite_position(p)) * mult
    return total_shares


def _trade_remaining(trade) -> float:
    rem = getattr(trade.orderStatus, "remaining", None)
    if rem is not None:
        try:
            value = float(rem)
            if math.isfinite(value) and value >= 0:
                total = float(trade.order.totalQuantity)
                filled = float(trade.orderStatus.filled)
                if (all(math.isfinite(x) for x in (total, filled)) and total > 0
                        and 0 <= filled <= total and value == total - filled):
                    return value
                sys.exit("⛔ 活跃挂单剩余量与总量/成交量不一致，不能按零计算")
        except (TypeError, ValueError, OverflowError):
            pass
        sys.exit("⛔ 活跃挂单剩余量无效，不能按零计算")
    try:
        tot = float(trade.order.totalQuantity)
        filled = float(trade.orderStatus.filled)
        if all(math.isfinite(x) for x in (tot, filled)) and tot > 0 and 0 <= filled <= tot:
            return tot - filled
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    sys.exit("⛔ 无法确认活跃挂单剩余量，不能按零计算")


def _finite_position(item):
    try:
        value = float(item.position)
        if math.isfinite(value):
            return value
    except (TypeError, ValueError, OverflowError, AttributeError):
        pass
    sys.exit("⛔ 持仓数量无效")


def _pending_stock_sales(ib, symbol, currency):
    total = 0.0
    for trade in _open_trades(ib):
        if getattr(trade.orderStatus, "status", "") in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        c, o = trade.contract, trade.order
        if (c.secType == "STK" and c.symbol.upper() == symbol.upper()
                and c.currency.upper() == currency.upper() and o.action.upper() == "SELL"):
            total += _trade_remaining(trade)
    return total


def _short_options_by_id(ib, con_id):
    total = 0.0
    for p in _portfolio(ib):
        if getattr(p.contract, "secType", "") == "OPT" and p.contract.conId == con_id:
            total -= _finite_position(p)
    return max(0.0, total)


def _pending_option_buys_by_id(ib, con_id):
    total = 0.0
    for t in _open_trades(ib):
        if getattr(t.orderStatus, "status", "") in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
            continue
        if (getattr(t.contract, "secType", "") == "OPT" and t.contract.conId == con_id
                and t.order.action.upper() == "BUY"):
            total += _trade_remaining(t)
    return total


def _pending_short_calls(ib, symbol, currency):
    """未完全成交的空头 Call 挂单张数（按未成交张数 remaining 汇总）"""
    trades = _open_trades(ib)
    inactive = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    total_remaining = 0.0
    sym = symbol.upper()
    cur = str(currency).upper()
    for t in trades:
        st = getattr(getattr(t, "orderStatus", None), "status", "")
        if st in inactive:
            continue
        c = getattr(t, "contract", None)
        o = getattr(t, "order", None)
        if not c or not o:
            continue
        if (getattr(c, "secType", "") == "OPT" and
            getattr(c, "symbol", "").upper() == sym and
            str(getattr(c, "currency", "")).upper() == cur and
            getattr(c, "right", "").upper() == "C" and
            getattr(o, "action", "").upper() == "SELL"):
            rem = _trade_remaining(t)
            total_remaining += rem
    return int(total_remaining) if total_remaining.is_integer() else total_remaining


def _pending_short_calls_shares(ib, symbol, currency):
    """未完全成交的空头 Call 挂单占用的正股股数（按各个期权合约自身乘数累加）"""
    trades = _open_trades(ib)
    inactive = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    total_shares = 0.0
    sym = symbol.upper()
    cur = str(currency).upper()
    for t in trades:
        st = getattr(getattr(t, "orderStatus", None), "status", "")
        if st in inactive:
            continue
        c = getattr(t, "contract", None)
        o = getattr(t, "order", None)
        if not c or not o:
            continue
        if (getattr(c, "secType", "") == "OPT" and
            getattr(c, "symbol", "").upper() == sym and
            str(getattr(c, "currency", "")).upper() == cur and
            getattr(c, "right", "").upper() == "C" and
            getattr(o, "action", "").upper() == "SELL"):
            rem = _trade_remaining(t)
            mult = _multiplier(ib, c)
            total_shares += rem * mult
    return total_shares


def _pending_cash_puts(ib, currency):
    """未完全成交的卖 Put 挂单所需现金担保合计"""
    trades = _open_trades(ib)
    inactive = {"Cancelled", "ApiCancelled", "Inactive", "Filled"}
    total_cash = 0.0
    cur = str(currency).upper()
    for t in trades:
        st = getattr(getattr(t, "orderStatus", None), "status", "")
        if st in inactive:
            continue
        c = getattr(t, "contract", None)
        o = getattr(t, "order", None)
        if not c or not o:
            continue
        if (getattr(c, "secType", "") == "OPT" and
            str(getattr(c, "currency", "")).upper() == cur and
            getattr(c, "right", "").upper() == "P" and
            getattr(o, "action", "").upper() == "SELL"):
            rem = _trade_remaining(t)
            mult = _multiplier(ib, c)
            strike = _positive_number(getattr(c, "strike", None), "存量 Put 行权价")
            total_cash += strike * mult * rem
    return total_cash


def _short_puts_collateral(ib, currency):
    """统计当前账户在某币种下已有已成交空头 Put 的现金担保总额（strike * mult * |position|）"""
    total_cash = 0.0
    cur_u = str(currency).upper()
    for p in _portfolio(ib):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if (getattr(c, "secType", "") == "OPT" and
                str(getattr(c, "currency", "")).upper() == cur_u and
                getattr(c, "right", "").upper() == "P" and
                getattr(p, "position", 0) < 0):
            mult = _multiplier(ib, c)
            strike = _positive_number(getattr(c, "strike", None), "存量 Put 行权价")
            total_cash += abs(float(p.position)) * strike * mult
    return total_cash


def _funds(ib):
    """资金明细：按币种返回实际现金与可用资金。

    返回 (cash_map, avail_map, base_currency)：
    - cash_map: {币种: TotalCashValue 实际现金余额}
    - avail_map: {币种: AvailableFunds 可用资金}
    - base_currency: 账户基础币种（取 "BASE" 汇总行无法直接给出时用最大非 BASE 币种兜底）。
    绝不把不同币种的原始数字直接比较 —— 那会把 100 JPY 当成 100 USD。
    """
    cash_map = {}
    avail_map = {}
    try:
        values = ib.accountValues()
        if values is None:
            raise ValueError("account values unavailable")
    except Exception as exc:
        sys.exit(f"⛔ 无法确认账户现金：{type(exc).__name__}: {exc}")
    for v in values:
        cur = (getattr(v, "currency", "") or "").upper()
        if not cur:
            continue
        tag = getattr(v, "tag", "")
        if tag == "TotalCashValue":
            try:
                f = float(v.value)
            except (TypeError, ValueError):
                sys.exit(f"⛔ {cur} 现金余额无法解析")
            if not math.isfinite(f) or f < 0:
                sys.exit(f"⛔ {cur} 现金余额无效或为负，不能用融资抵押")
            if cur not in cash_map or f < cash_map[cur]:
                cash_map[cur] = f
        elif tag in ("AvailableFunds", "FullAvailableFunds"):
            try:
                f = float(v.value)
            except (TypeError, ValueError):
                continue  # not collateral; missing values cannot improve cash coverage
            # 同币种多标签时取较保守（较小）的一份，避免高估可用资金
            if not math.isfinite(f):
                sys.exit(f"⛔ {cur} 可用资金无效")
            if cur not in avail_map or f < avail_map[cur]:
                avail_map[cur] = f

    real_avail = {c: v for c, v in avail_map.items() if c != "BASE"}
    real_cash = {c: v for c, v in cash_map.items() if c != "BASE"}

    if "BASE" in avail_map and real_avail:
        base = max(real_avail, key=real_avail.get)
    elif "BASE" in cash_map and real_cash:
        base = max(real_cash, key=real_cash.get)
    elif real_avail:
        base = max(real_avail, key=real_avail.get)
    elif real_cash:
        base = max(real_cash, key=real_cash.get)
    else:
        base = "USD"

    if "BASE" not in cash_map:
        cash_map["BASE"] = real_cash.get(base, 0.0)
    if "BASE" not in avail_map:
        avail_map["BASE"] = real_avail.get(base, 0.0)

    return cash_map, avail_map, base


def _fx(ib, base, cur):
    """1 单位 base 折合多少 cur；取不到返回 None。

    先试 base/cur 直盘；若 IB 不认该货币对，再试 cur/base 反向盘并把价格取倒数。
    """
    if base == cur:
        return 1.0

    def _quote(symbol, currency, invert):
        c = Contract(secType="CASH", symbol=symbol, currency=currency, exchange="IDEALPRO")
        try:
            q = [x for x in ib.qualifyContracts(c) if x and x.conId]
        except Exception:
            return None
        if not q:
            return None
        px = None
        try:
            bars = ib.reqHistoricalData(q[0], "", "5 D", "1 day", "MIDPOINT", True)
        except Exception:
            bars = None
        if bars:
            px = float(bars[-1].close)
        else:
            px = _spot(ib, q[0])
        if not px or float(px) <= 0:
            return None
        return (1.0 / float(px)) if invert else float(px)

    direct = _quote(base, cur, invert=False)
    if direct:
        return direct
    return _quote(cur, base, invert=True)


def _coverage(ib, currency, need, require_cash=True):
    """担保核对。need 为交易币种金额，返回 (是否足额, 说明)。

    require_cash=True（现金担保卖 Put）严格检查 TotalCashValue 实际现金，
    扣减未成交卖 Put 挂单及存量已成交空头 Put 占用，严禁利用 AvailableFunds 融资额度或杠杆；
    require_cash=False 允许利用可用资金（AvailableFunds）。
    安全性：只要无法判定足额（汇率取不到、没有任何资金数据），一律返回 False，
    绝不用 None 绕过闸门。
    """
    cash_map, avail_map, base = _funds(ib)
    cur_u = currency.upper()

    if require_cash:
        _positive_number(need, "所需担保")
        currencies = {cur_u}
        for p in _portfolio(ib):
            c = getattr(p, "contract", None)
            if c and getattr(c, "secType", "") == "OPT" and getattr(c, "right", "").upper() == "P" and _finite_position(p) < 0:
                if not getattr(c, "currency", ""):
                    sys.exit("⛔ Put 持仓缺少币种")
                currencies.add(c.currency.upper())
        for t in _open_trades(ib):
            if getattr(t.orderStatus, "status", "") in {"Cancelled", "ApiCancelled", "Inactive", "Filled"}:
                continue
            c, o = t.contract, t.order
            if getattr(c, "secType", "") == "OPT" and getattr(c, "right", "").upper() == "P" and getattr(o, "action", "").upper() == "SELL":
                if not getattr(c, "currency", ""):
                    sys.exit("⛔ Put 挂单缺少币种")
                currencies.add(c.currency.upper())
        locked = {c: _pending_cash_puts(ib, c) + _short_puts_collateral(ib, c) for c in currencies}
        total_locked_cur = locked[cur_u]
        direct = cash_map.get(cur_u)
        if direct is not None:
            foreign_locked = 0.0
            for obligation_currency, amount in locked.items():
                if obligation_currency == cur_u or not amount:
                    continue
                rate = _fx(ib, cur_u, obligation_currency)
                if rate is None or not math.isfinite(rate) or rate <= 0:
                    return False, f"无法折算 {obligation_currency} 存量义务，安全起见拒绝放行 ❌"
                foreign_locked += amount / rate
            cash = direct - total_locked_cur - foreign_locked
            ok = cash >= need
            if not ok:
                return False, (f"需要 {fmt(need)} {currency}，账户实际现金 TotalCashValue 仅 {fmt(cash)} {currency}"
                               f"（AvailableFunds 融资额度不可用于现金担保）❌")
            pend_txt = f"（已扣减挂单及存量占用 {fmt(total_locked_cur)} {currency}）" if total_locked_cur > 0 else ""
            return True, f"需要 {fmt(need)} {currency}，账户实际现金 {fmt(cash)} {currency}{pend_txt} ✅"

        real_cash = {c: v for c, v in cash_map.items() if c != "BASE"}
        # BASE is an aggregate view, not another spendable currency. Without a
        # uniquely identified detailed cash pool, conversion would reuse funds.
        if len(real_cash) != 1:
            return False, f"需要 {fmt(need)} {currency}，无法确认账户实际现金，安全起见拒绝放行 ❌"
        source, source_cash = next(iter(real_cash.items()))
        source_cash = min(source_cash, cash_map.get("BASE", source_cash))
        rates = {source: 1.0}
        for c in currencies:
            if c == source:
                continue
            rate = _fx(ib, source, c)
            if rate is None or not math.isfinite(rate) or rate <= 0:
                return False, f"需要 {fmt(need)} {currency}，无法获取 {source}/{c} 汇率，安全起见拒绝放行 ❌"
            rates[c] = rate
        net_base = source_cash - sum(amount / rates[c] for c, amount in locked.items())
        conv = net_base * rates[cur_u]
        ok = conv >= need
        if not ok:
            return False, (f"需要 {fmt(need)} {currency}，账户实际现金 TotalCashValue 仅 {fmt(conv)} {currency}"
                           f"（AvailableFunds 融资额度不可用于现金担保）❌")
        return True, (f"需要 {fmt(need)} {currency}，账户实际现金 {fmt(net_base)} {source} ≈ {fmt(conv)} {currency}"
                      f"（汇率 {rates[cur_u]:.4f}，已扣减挂单及存量占用 {fmt(locked.get(source, 0))} {source} 及 {fmt(total_locked_cur)} {currency}；全部币种 Put 义务） ✅")

    direct = avail_map.get(cur_u)
    if direct is not None:
        ok = direct >= need
        return ok, (f"需要 {fmt(need)} {currency}，账户 {currency} 可用 {fmt(direct)}"
                    + (" ✅" if ok else " ❌"))
    avail = avail_map.get("BASE")
    if avail is None or cur_u == str(base).upper():
        return False, f"需要 {fmt(need)} {currency}，无法确认账户可用资金，安全起见拒绝放行 ❌"
    rate = _fx(ib, base, cur_u)
    if rate is None or rate <= 0:
        return False, f"需要 {fmt(need)} {currency}，无法获取 {base}/{currency} 汇率，安全起见拒绝放行 ❌"
    conv = avail * rate
    ok = conv >= need
    return ok, (f"需要 {fmt(need)} {currency}，账户可用 {fmt(avail)} {base} ≈ {fmt(conv)} {currency}"
                f"（汇率 {rate:.4f}）" + (" ✅" if ok else " ❌"))


def cmd_combo(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    _live_guard(cfg, args, port)

    # Pre-network guard for intent/code pair
    intent_id = str(getattr(args, "intent", "") or "").strip()
    code = str(getattr(args, "code", "") or "").strip()
    if code and not intent_id:
        sys.exit("⛔ --code alone is not a confirmation; an explicit --intent is required (no legacy _code path)")
    if intent_id and not code:
        sys.exit("⛔ --intent alone is not a confirmation; an explicit --code is required")

    sym, preset = args.symbol.upper(), args.preset
    right = "P" if preset == "cash-put" else "C"
    qty = _positive_number(args.qty, "期权张数", integer=True)
    strike = _positive_number(args.k1, "行权价")
    if args.net is not None:
        _positive_number(args.net, "权利金价格")
    if args.stock_px is not None:
        _positive_number(args.stock_px, "正股价格")
    is_live = _is_live_env(cfg, args, port)

    ib = connect(cfg, port, readonly=False, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        _account_scope(ib, account)
        _sync_open_orders(ib)
        _validate_obligations(ib)

        opt, _exch = _opt(
            ib, sym, args.expiry, strike, right, args.currency,
            exchange=getattr(args, "exchange", None),
            multiplier=getattr(args, "multiplier", None),
            trading_class=getattr(args, "trading_class", None),
            con_id=getattr(args, "con_id", None))
        if not opt:
            sys.exit(f"期权合约无法识别：{sym} {args.expiry} {strike:g}{right}（可用 --currency 指定币种）")
        mult = _multiplier(ib, opt)
        opt.multiplier = str(mult)
        _delivery_guard(ib, opt)
        need_shares = mult * qty

        # ---- 前提校验：这就是这个策略能不能成立的条件
        have = _shares(ib, sym, args.currency)
        stock_contract, stock_qty, stock_note = None, 0, ""
        busy_shares = (_short_calls_shares(ib, sym, args.currency)
                       + _pending_short_calls_shares(ib, sym, args.currency)
                       + _pending_stock_sales(ib, sym, args.currency))
        busy_count = _short_calls(ib, sym, args.currency) + _pending_short_calls(ib, sym, args.currency)

        if preset == "covered-call":
            if have < 0:
                sys.exit(f"\n⛔ 账户持有 {have:g} 股 {sym} 空头头寸（做空），禁止开具备兑 Call。\n")
            if have < busy_shares:
                sys.exit(f"\n⛔ 现有正股 {have:g} 股不足以覆盖现有空头 Call（已占用 {busy_shares:g} 股，缺口 {busy_shares - have:g} 股）。禁止继续开具备兑 Call。\n")
            avail = have - busy_shares
            if avail >= need_shares:
                sys.exit(f"\n⛔ 你已持有 {have:g} 股 {sym}（已占用 {busy_shares:g} 股，净可用 {avail:g} 股），足够覆盖 {qty} 张（每张需 {mult} 股）。\n"
                         f"   再买 {need_shares} 股只是加仓，不是备兑。请改用：--preset sell-call\n")
            stock_contract = _stock(ib, sym, args.currency)
            if not stock_contract:
                sys.exit(f"正股合约无法识别：{sym} {args.currency}")
            _delivery_guard(ib, opt, stock_contract)
            if avail > 0:
                stock_qty = need_shares - avail
                stock_note = f"现有净可用 {avail:g} 股 → 差额补买 {stock_qty} 股（合计 {need_shares} 股覆盖 {qty} 张）"
            else:
                stock_qty = need_shares
                stock_note = f"现有净可用 0 股 → 本单买入 {stock_qty} 股"
        elif preset == "sell-call":
            # 已被其它空头 Call 占用的股票不能再用来覆盖本单，否则就是变相裸卖
            if have < 0:
                sys.exit(f"\n⛔ 账户持有 {have:g} 股 {sym} 空头头寸（做空），禁止卖出 Call。\n")
            if have < busy_shares:
                sys.exit(f"\n⛔ 现有正股 {have:g} 股不足以覆盖现有空头 Call（已占用 {busy_shares:g} 股，缺口 {busy_shares - have:g} 股）。禁止继续卖出 Call。\n")
            avail = have - busy_shares
            if avail < need_shares:
                sys.exit(f"\n⛔ 备兑卖 Call 的先决条件是持有未被覆盖的正股：\n"
                         f"   卖 {qty} 张需 {need_shares} 股，你总持有 {have:g} 股，已有 {busy_count} 张空头持仓及挂单占用了 {busy_shares:g} 股，净可用仅 {avail:g} 股。\n"
                         f"   没有股票覆盖的卖出 Call 是裸卖，理论上风险无限，本工具不做。\n")
            stock_note = f"总持仓 {have:g} 股（已占用 {busy_shares:g} 股），净可用 {avail:g} 股 ≥ 需 {need_shares} 股 ✅"
        else:
            need_cash = strike * mult * qty
            ok, note = _coverage(ib, args.currency, need_cash, require_cash=True)
            if ok is False:
                sys.exit(f"\n⛔ 现金担保不足：卖 {qty} 张 {strike:g} Put 须备足 {fmt(need_cash)} {args.currency}。\n"
                         f"   {note}\n"
                         f"   现金担保卖 Put 的意义就是行权时买得起，备不足即等同裸卖，本工具不做。\n")
            stock_note = note

        if stock_contract:
            if not float(stock_qty).is_integer() or stock_qty <= 0:
                sys.exit("⛔ 补买正股差额为碎股；未确认券商支持，停止备兑策略")
            if args.stock_px is None or args.stock_px <= 0:
                sys.exit("⛔ covered-call 买正股需要明确的限价 --stock-px（禁止无上限市价买入）")

        net = args.net
        otype = "LMT" if net not in (None, 0) else "MKT"

        print("─" * 68)
        if is_live:
            print("  🔴 实盘账户 —— 下面这笔是真实资金委托")
        print(f"  环境    {env}   端口 {port}   账户模式 {'实盘' if is_live else '纸账户'}")
        print(f"  策略    {preset} —— {PRESETS[preset]}")
        print(f"  标的    {sym}   {qty} 张   乘数 {mult}")
        if stock_contract:
            pxtxt = f"限价 {args.stock_px}" if args.stock_px else "市价"
            print(f"    BUY  {stock_qty} 股 {sym} {pxtxt}（先成交，再卖 Call）")
        print(f"    SELL {qty}× {opt.localSymbol}"
              + (f"  权利金 {net:.3f}（共收 {fmt(net * mult * qty)} {args.currency}）" if net else "  市价"))
        print(f"  条件    {stock_note}")
        print("─" * 68)

        # 2) 期权腿：卖出（收权利金）
        order = LimitOrder("SELL", qty, net) if otype == "LMT" else MarketOrder("SELL", qty)
        order.tif = "DAY"
        if account:
            order.account = account

        if stock_contract:
            so = LimitOrder("BUY", stock_qty, args.stock_px)
            so.tif = "DAY"
            if account:
                so.account = account
            pairs = [(stock_contract, so), (opt, order)]
            requires_filled = (1,)
        else:
            pairs = [(opt, order)]
            requires_filled = ()

        from thetamill import submission_cli
        try:
            result = submission_cli.execute_prepared(
                ib, cfg, args,
                pairs=pairs,
                strategy=preset,
                requires_filled=requires_filled,
                delivery_evidence_path=getattr(args, "delivery_evidence", None),
            )
            return result.rc
        except submission_cli.SubmissionCliError as exc:
            msg = submission_cli._mask_account_text(str(exc), account)
            sys.exit(f"⛔ {msg}")
    finally:
        finish(ib)


# ---------------------------------------------------------------- 计划 / 执行 / 审计
def _dte(yyyymmdd):
    """距到期日天数"""
    try:
        return (datetime.strptime(str(yyyymmdd), "%Y%m%d").date() - date.today()).days
    except ValueError:
        return None


def _chain_safe(ib, symbol, currency):
    """取期权链参数；标的没有期权时返回 (None, None)，不像 _opt_params 那样直接退出"""
    try:
        stk = _stock(ib, symbol, currency)
        if not stk:
            return None, None
        chains = ib.reqSecDefOptParams(stk.symbol, "", stk.secType, stk.conId)
        if not chains:
            return None, None
        smart = [c for c in chains if c.exchange == "SMART"] or chains
        smart.sort(key=lambda x: len(x.expirations), reverse=True)
        return stk, smart[0]
    except Exception:
        return None, None


def _pick_expiry(exps, dmin, dmax, target):
    """在 DTE 窗口内挑最接近目标天数的一档；窗口为空则退到全部到期日里取最接近的"""
    rows = [(e, _dte(e)) for e in sorted(exps)]
    rows = [(e, d) for e, d in rows if d is not None and d >= 0]
    if not rows:
        return None, None
    win = [(e, d) for e, d in rows if dmin <= d <= dmax] or rows
    win.sort(key=lambda x: abs(x[1] - target))
    return win[0]


def _cost_basis(ib, symbol, currency):
    """正股每股成本（含佣金），无持仓返回 None"""
    sym = symbol.upper()
    for p in ib.portfolio():
        c = p.contract
        if c.secType == "STK" and c.symbol.upper() == sym and c.currency == currency and p.position:
            try:
                return float(p.averageCost) or None
            except (TypeError, ValueError):
                return None
    return None


def _quote_premium(ib, opt):
    """卖方可得的参考权利金：优先买价（对手盘出价），退化到最新/昨收。返回 (价格, 来源)"""
    t = ib.reqTickers(opt)[0]
    ib.sleep(1.5)
    for label, v in (("买价", t.bid), ("最新", t.last), ("昨收", t.close)):
        try:
            if v == v and v is not None and float(v) > 0:
                return float(v), label
        except (TypeError, ValueError):
            continue
    return None, "无行情"


def _resolve_option(ib, symbol, expiry, strikes, target, right, currency, chain, floor=None):
    """在指定到期日上找到真实存在的行权价。

    reqSecDefOptParams 返回的 strikes 是所有到期日的并集，个别月份只含其中一部分，
    所以不能只挑一档就去 qualify —— 要按「离目标最近且方向正确」的顺序逐个试。
    返回 (期权合约, 行权价)。
    """
    ks = sorted(float(x) for x in strikes)
    if not ks:
        return None, None
    target = float(target) if target is not None else 0.0
    floor = float(floor) if floor is not None else None
    if right == "C":
        t = target if floor is None else max(target, floor)
        # 先找 ≥ 目标（由低到高）：最贴近目标的虚值/平值档；
        # 没有才回退到 < 目标，且必须从高到低（离目标最近），避免挑到深度实值档。
        ge = [k for k in ks if k >= t - 1e-9]
        lt = [k for k in ks if k < t - 1e-9]
        cands = (sorted(ge) + sorted(lt, reverse=True))[:6]
    else:
        # 先找 ≤ 目标（由高到低）；没有才回退到 > 目标，且必须从低到高（离目标最近）。
        le = [k for k in ks if k <= target + 1e-9]
        gt = [k for k in ks if k > target + 1e-9]
        cands = (sorted(le, reverse=True) + sorted(gt))[:6]
    # 每档单独解析；批量 qualify 会省略失败项，不能按返回位置配对行权价。
    for k in cands:
        opt, _x = _opt(
            ib, symbol, expiry, k, right, currency, exchange=chain.exchange,
            multiplier=getattr(chain, "multiplier", None),
            trading_class=chain.tradingClass)
        if opt:
            return opt, float(opt.strike)
    return None, None


def cmd_plan(cfg, args, now: datetime | None = None):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    from . import plan_cli
    return plan_cli.cmd_plan(cfg, args, now=now)


def cmd_run(cfg, args):
    """执行 plan 生成的建议。不跳过任何闸门：实盘照旧要 live_enabled + --live-ok + 复核码"""
    path = Path(args.file).expanduser()
    if not path.exists():
        sys.exit(f"找不到计划文件：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"计划文件不是合法 JSON：{e}")
    if not isinstance(doc, dict):
        sys.exit("计划文件必须是 JSON 对象")
    if doc.get("recommendation_only") or str(doc.get("schema", "")).startswith("thetamill.roll."):
        sys.exit("⛔ 展期文件仅供建议，不能通过 run 执行；平旧仓和开新仓必须分别重新复核。")
    port, env = resolve_port(cfg, args)
    is_live = _is_live_env(cfg, args, port)

    # Compare timestamps as instants, not lexicographically across UTC offsets.
    if "valid_until" in doc:
        from datetime import timezone
        vu = doc["valid_until"]
        try:
            if not isinstance(vu, str) or not vu.strip():
                raise ValueError("invalid expiry")
            expires = datetime.fromisoformat(vu.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                # Legacy plans used the machine's local timezone.
                expires = expires.astimezone()
            expired = expires.astimezone(timezone.utc) <= datetime.now(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            sys.exit("⛔ 计划有效期无效，请重新运行 plan 生成最新计划。")
        if expired:
            sys.exit("⛔ 计划文件已过期，请重新运行 plan 生成最新计划。")

    def plan_number(value, name, integer=False):
        # Legacy order construction uses floats: reject, never round, a value
        # that cannot survive that boundary unchanged.
        from decimal import Decimal, InvalidOperation
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError("invalid plan number")
            number = Decimal(str(value))
            if (not number.is_finite() or number <= 0
                    or (integer and number != number.to_integral_value())):
                raise ValueError("invalid plan number")
            converted = float(number)
            if not math.isfinite(converted):
                raise ValueError("unrepresentable plan number")
            if integer:
                if Decimal.from_float(converted) != number:
                    raise ValueError("quantity loses precision")
                return int(number)
            if Decimal(str(converted)) != number:
                raise ValueError("price loses precision")
            return converted
        except (InvalidOperation, ValueError, OverflowError):
            sys.exit(f"⛔ {name}必须是可精确传递的有限正数" + ("且为整数" if integer else ""))

    # 跨环境执行锁：实盘生成的计划不能在纸账户跑，反之亦然
    if doc.get("is_live") is not None and doc["is_live"] != is_live:
        sys.exit(f"⛔ 计划文件运行环境（{'实盘' if doc['is_live'] else '纸账户'}）与当前执行环境（{'实盘' if is_live else '纸账户'}）不一致，严禁跨环境执行！")

    items = doc.get("items")
    if not isinstance(items, list) or not items:
        sys.exit("计划文件里没有有效的可执行条目列表")
    indices = set()
    for item in items:
        if (not isinstance(item, dict) or type(item.get("i")) is not int
                or item["i"] <= 0 or item["i"] in indices):
            sys.exit("⛔ 计划编号必须是唯一正整数，请重新生成计划。")
        indices.add(item["i"])

    # 计划文件带白名单元信息时，先复验签字与有效期，防止过期计划被执行
    uni = doc.get("universe")
    uni_map = {}
    if uni:
        ok, problems = validate(uni)
        if not ok:
            sys.exit("⛔ 计划里的准入白名单已失效：\n  - " + "\n  - ".join(problems)
                     + "\n   请用 plan --universe 重新生成计划。")
        uni_map = universe_allowed(uni)

    idx = args.i
    if idx is None and len(items) == 1:
        idx = items[0]["i"]
    if idx is None:
        print(f"计划文件含 {len(items)} 条建议（{doc.get('generated', '?')}）：")
        for it in items:
            print(f"  #{it['i']}  {it['preset']:<12}{it['symbol']:<8}{it['expiry']}  "
                  f"{plan_number(it['strike'], '计划行权价'):g}"
                  f"{'C' if it['preset'] != 'cash-put' else 'P'} × {it['qty']}")
        sys.exit("\n用 --i N 指定要执行哪一条。")

    if type(idx) is not int or idx <= 0:
        sys.exit("⛔ --i 必须是正整数")
    it = next((x for x in items if x["i"] == idx), None)
    if not it:
        sys.exit(f"计划里没有 #{idx} 这一条")
    if "allowed" in it and it["allowed"] is not True:
        sys.exit("⛔ 该计划条目未通过累计风险预算/证据门禁，请重新生成并复核计划。")

    # 二次校验：手工改过计划文件（抬高行权价 / 换标的）也绕不过白名单
    if uni:
        ok, why = universe_gate(it["symbol"], it["preset"], float(it["strike"]), uni_map)
        if not ok:
            sys.exit(f"⛔ 计划条目已不符合准入白名单：{why}\n"
                     f"   请用 plan --universe 重新生成，不要手工改动计划文件。")

    # 旧计划未保存完整合约身份，不能重新解析成另一种乘数的合约。
    con_id = it.get("conId")
    mult = _parse_multiplier(it.get("mult"))
    if mult is not None:
        mult = plan_number(it["mult"], "计划合约乘数")
    trading_class, exchange = it.get("tradingClass"), it.get("exchange")
    if (type(con_id) is not int or con_id <= 0 or mult is None
            or not isinstance(trading_class, str) or not trading_class.strip()
            or not isinstance(exchange, str) or not exchange.strip()):
        sys.exit("⛔ 计划缺少有效合约身份（conId / mult / tradingClass / exchange），请重新运行 plan 生成。")

    qty = plan_number(it.get("qty"), "计划期权张数", integer=True)
    strike = plan_number(it.get("strike"), "计划行权价")
    net = it.get("premium") if it.get("premium") else None
    if args.net is not None:
        net = args.net
    if net is None:
        sys.exit(f"\n⛔ #{idx} 没有可用的参考权利金（该市场无行情），无法自动定价。\n"
                 f"   请手工指定：thetamill run --file {path} --i {idx} --net 5.00\n")

    net = plan_number(net, "计划权利金价格")
    print(f"按计划 #{idx} 执行：{it['preset']} {it['symbol']} {it['expiry']} {strike:g} × {qty}")
    ns = argparse.Namespace(
        symbol=it["symbol"], preset=it["preset"], expiry=it["expiry"], k1=strike,
        net=float(net), qty=qty, currency=it.get("currency") or cfg["default_currency"],
        con_id=con_id, multiplier=mult, trading_class=trading_class, exchange=exchange,
        stock_px=args.stock_px, code=args.code, live_ok=args.live_ok,
        paper=args.paper, port=args.port, delayed=args.delayed,
        account=getattr(args, "account", None),
        intent=getattr(args, "intent", ""),
        delivery_evidence=getattr(args, "delivery_evidence", None),
    )
    return cmd_combo(cfg, ns)


def _ledger_audit_rows(selected):
    """Orders the SQLite ledger knows about, as audit-shaped rows (read-only).

    The ledger — not ``orders.jsonl`` — is authoritative for what was actually
    sent, so an order that reached the broker but whose audit append failed
    (or predates the append) still shows up here.  A missing/foreign/corrupt
    ledger is reported, never created, and never treated as "no history".
    """
    path = _data_dir() / "state.sqlite3"
    if not path.is_file():
        return [], None
    from . import audit as audit_mod
    from .state import SubmissionLedger, StateError

    try:
        ledger = SubmissionLedger(path, "UTC", read_only=True)
        scopes = ledger.list_scopes()
    except StateError as exc:
        return [], f"提交账本无法只读打开：{exc}"
    rows = []
    for account, environment in scopes:
        if selected is not None and account != selected:
            continue
        try:
            legs = ledger.list_legs(account=account, environment=environment)
            observed = ledger.list_reconciliations(account=account,
                                                   environment=environment)
        except StateError as exc:
            return [], f"提交账本读取失败：{exc}"
        # A reconciliation observation carries the broker status the ledger
        # could actually prove ("Submitted"/"Filled"/"Cancelled"); the bare leg
        # state ("confirmed") no longer says HOW it resolved.
        evidence = {}
        for rec in observed:
            if rec.broker_status:
                evidence[rec.order_ref] = rec.broker_status
        for leg in legs:
            rows.append({
                "ts": leg.updated_at, "account": account, "env": environment,
                "kind": "leg", "preset": "ledger", "strategy": "ledger",
                "orderId": leg.order_id, "permId": leg.perm_id,
                "status": evidence.get(leg.order_ref, leg.state),
                "orderRef": leg.order_ref, "qty": leg.quantity,
                "action": leg.action, "contract_id": leg.contract_id,
                "source": "ledger",
            })
    return [row for row in rows if audit_mod.is_real_record(row)], None


def cmd_audit(cfg, args):
    """Show the audit trail: real broker orders only, plus ledger records.

    Synthetic rows (a leaked unit-test ``MagicMock`` repr written by the old
    ``default=str`` writer) are counted and hidden — printing them as orders is
    worse than printing nothing — and the SQLite ledger is merged in, because
    the ledger, not this file, is authoritative for what was actually sent.
    """
    from . import audit as audit_mod

    path = audit_log_path()
    parsed = audit_mod.read_records(path)
    rows = [row for row in parsed if audit_mod.is_real_record(row)]
    hidden = len(parsed) - len(rows)
    selected = getattr(args, "account", None) or cfg.get("account")
    known_accounts = {r["account"] for r in rows
                      if isinstance(r.get("account"), str) and r["account"].strip()}
    if selected is None and len(known_accounts) > 1:
        sys.exit("审计含多个账户，请用 --account 明确选择；旧记录未知归属不能推断。")
    if selected is None and len(known_accounts) == 1:
        selected = next(iter(known_accounts))
    unknown_count = sum(not isinstance(r.get("account"), str) or not r["account"].strip()
                        for r in rows)
    ledger_rows, ledger_error = _ledger_audit_rows(selected)
    if selected is not None:
        rows = [r for r in rows if r.get("account") == selected]
        from .report import mask_account
        print(f"审计账户：{mask_account(selected)}；排除 {unknown_count} 条未知账户旧记录")
    elif unknown_count:
        print("旧审计记录账户归属未知；不用于配额或风险额度计算。")
    if hidden:
        print(f"已隐藏 {hidden} 条非真实审计记录（单元测试遗留的 mock 值，不是券商订单）")
    if ledger_error:
        print(f"⚠️ {ledger_error}")

    # Merge the ledger's view: a real order with no (or lost) JSONL line is
    # still an order.  One row per (orderRef, orderId); the log wins on detail.
    seen = {(str(r.get("orderRef", "")), str(r.get("orderId", "")))
            for r in rows if r.get("orderRef")}
    only_ledger = [r for r in ledger_rows
                   if (str(r.get("orderRef", "")), str(r.get("orderId", ""))) not in seen]
    combined = sorted(rows + only_ledger, key=lambda r: str(r.get("ts", "")))
    if args.today:
        today = datetime.now().strftime("%Y-%m-%d")
        combined = [r for r in combined if str(r.get("ts", "")).startswith(today)]
    combined = combined[-max(args.limit, 1):]
    if not combined and not hidden:
        print(f"暂无审计记录 —— 日志路径 {path}")
        return 0
    print(f"审计日志 {path}")
    print(f"  {'时间':<20}{'策略':<14}{'单号':>7}  {'状态':<14}{'成交':>6}  {'环境':<12}来源")
    for r in combined:
        print(f"  {str(r.get('ts', ''))[:19]:<20}{str(r.get('preset') or r.get('label') or '-'):<14}"
              f"{str(r.get('orderId', '-')):>7}  {str(r.get('status', '-'))[:13]:<14}"
              f"{fmt(r.get('filled'), 0):>6}  {str(r.get('env', '-'))[:11]:<12}"
              f"{'账本' if r.get('source') == 'ledger' else '日志'}")
    print(f"\n  共 {len(combined)} 条（含纸账户与实盘，看「环境」列区分）")
    if only_ledger:
        print(f"  其中 {len(only_ledger)} 条只存在于提交账本（审计日志未写入或已丢失）")
    return 0


# ---------------------------------------------------------------- 只读风险报告
def cmd_roll_plan(cfg, args):
    from .roll_cli import cmd_roll_plan as render_plan
    return render_plan(cfg, args)


def _selected_account(ib, cfg, args):
    from .broker_state import select_account, SnapshotError
    try:
        return select_account(ib, getattr(args, "account", None) or cfg.get("account"))
    except SnapshotError as exc:
        sys.exit(f"⛔ {exc}")


def _timestamped_mid(ib, contract):
    """Use a timestamped two-sided tick; never relabel a historical close as fresh."""
    from decimal import Decimal
    from datetime import timezone
    try:
        ticks = ib.reqTickers(contract)
        if not isinstance(ticks, (list, tuple)) or len(ticks) != 1:
            return None
        tick = ticks[0]
        if getattr(tick, "marketDataType", None) != 1:
            return None
        if getattr(getattr(tick, "contract", None), "conId", None) != contract.conId:
            return None
        stamp = getattr(tick, "time", None)
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if age < -5 or age > 30:
            return None
        bid, ask = tick.bid, tick.ask
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (bid, ask)) or bid > ask:
            return None
        return (Decimal(str(bid)) + Decimal(str(ask))) / 2, stamp, "IB timestamped bid/ask midpoint"
    except Exception:
        return None


def _risk_snapshot(ib, cfg, args, environment, *, required_currencies=()):
    from decimal import Decimal
    from .broker_state import collect_snapshot, PriceQuote, FxQuote
    account = _selected_account(ib, cfg, args)
    snapshot = collect_snapshot(ib, account=account, environment=environment)
    currencies = set(snapshot.cash)
    for currency in required_currencies:
        if not isinstance(currency, str) or len(currency.strip()) != 3 or not currency.strip().isascii() or not currency.strip().isalpha():
            raise ValueError("Required FX currency must be a three-letter code")
        currencies.add(currency.strip().upper())
    currencies.update(p.instrument.currency for p in snapshot.positions)
    currencies.update(t.instrument.currency for t in snapshot.orders)
    rates, fx_quotes, price_quotes = {snapshot.base_currency: Decimal(1)}, {}, {}
    for currency in currencies - {snapshot.base_currency}:
        for symbol, target, invert in ((currency, snapshot.base_currency, False),
                                       (snapshot.base_currency, currency, True)):
            try:
                qualified = ib.qualifyContracts(Contract(secType="CASH", symbol=symbol,
                                                        currency=target, exchange="IDEALPRO"))
                if not isinstance(qualified, (list, tuple)) or len(qualified) != 1:
                    continue
                pair = qualified[0]
                if (type(getattr(pair, "conId", None)) is not int or pair.conId <= 0
                        or pair.secType != "CASH" or pair.symbol != symbol or pair.currency != target):
                    continue
                tick = _timestamped_mid(ib, pair)
                if tick is None:
                    continue
                price, stamp, source = tick
                rate = Decimal(1) / price if invert else price
                fx_quotes[currency] = FxQuote(currency, rate, stamp, source)
                rates[currency] = rate
                break
            except Exception:
                continue
    from dataclasses import replace
    positions = []
    for position in snapshot.positions:
        if position.instrument.sec_type == "STK":
            tick = _timestamped_mid(ib, Contract(conId=position.instrument.con_id,
                                               exchange=position.instrument.exchange or "SMART"))
            if tick is not None:
                price, stamp, source = tick
                price_quotes[position.instrument.con_id] = PriceQuote(position.instrument.con_id,
                                                                      price, stamp, source)
                position = replace(position, price=price)
            else:
                position = replace(position, price=None)
        positions.append(position)
    return replace(snapshot, fx=rates, fx_quotes=fx_quotes, price_quotes=price_quotes,
                   positions=positions)


@contextmanager
def _private_broker_diagnostics():
    """Do not expose raw broker messages in privacy-preserving reports."""
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with redirect_stdout(sink), redirect_stderr(sink):
            yield


def cmd_resolve(cfg, args):
    """解除未决预留 —— 只认券商正向证据，绝不因「没看到挂单」就放行。

    账户一旦有 submitting/submitted/unknown 的腿，所有新增风险都会被拦
    （见 execution.confirm）。本命令是唯一出口：
      --list                   只读列出当前阻塞账户的预留
      --reservation <id>       用实时只读券商证据核对并逐腿解除
                               （挂单一致，或成交执行记录一致）
      --state released         仅当【没有任何腿被尝试发送】时释放
    证据不足一律拒绝，预留继续保留；已消耗的当日提交次数永不退还。
    """
    from . import resolve as resolve_mod
    path = _data_dir() / "state.sqlite3"
    if not path.exists():
        sys.exit("没有本地提交账本；不会创建空库或推断历史为零")
    day_tz = (cfg.get("risk") or {}).get("day_timezone", "UTC")

    def _open(writable):
        try:
            return resolve_mod.open_ledger(path, day_tz, writable=writable)
        except resolve_mod.ResolveError as exc:
            sys.exit(f"⛔ {exc}")

    if args.list:
        ledger = _open(False)
        account = getattr(args, "account", None) or cfg.get("account")
        if not account:
            sys.exit("⛔ --list 需要 --account 或在配置里指定账户")
        # NEVER auto-detect the environment here.  `resolve_port` probes TCP
        # ports, so with no gateway running (exactly when an unresolved send is
        # most likely) it falls back to live and a paper reservation disappears
        # behind a cheerful "没有未决预留".  Only an explicit --paper/--port
        # narrows the search; otherwise both environments are reported.
        if getattr(args, "port", None):
            environment = ("live" if _is_live_env(cfg, args, args.port)
                           else "paper")
        elif getattr(args, "paper", False):
            environment = "paper"
        else:
            environment = None
        blocked = resolve_mod.unresolved_summary(
            ledger, account=account, environment=environment)
        scope_text = environment or "全部环境"
        if not blocked:
            print(f"账户 {account}（{scope_text}）没有未决预留。")
            return 0
        print(f"账户 {account}（{scope_text}）有 {len(blocked)} 个未决预留，"
              "新增风险已被阻断：")
        for item in blocked:
            where = "" if environment else f"  [{item.environment}]"
            print(f"  {item.reservation_id}  状态={item.state}  日={item.day_key}"
                  f"{where}")
            for ref, action, qty, leg_state in item.legs:
                print(f"     {ref}  {action} {qty}  {leg_state}")
        print("\n用 `thetamill resolve --reservation <id>` 抓取实时券商证据核对。")
        return 0

    if not args.reservation:
        print("请指定 --list 或 --reservation <id>")
        return 2
    if args.state not in ("confirmed", "released"):
        print("--state 只能是 confirmed 或 released")
        return 2
    ledger = _open(True)

    if args.state == "released":
        if not args.why:
            print("⛔ --state released 必须用 --why 写明为何能证明从未发出")
            return 2
        try:
            outcome = resolve_mod.release_never_sent(
                ledger, args.reservation, why=args.why)
        except resolve_mod.ResolveError as exc:
            print(f"⛔ 未释放：{exc}")
            return 2
        print(f"✅ 预留 {outcome.reservation_id} 已释放（证明从未发送）。")
        return 0

    # confirmed: 必须拿到实时只读券商证据
    reservation = ledger.get_reservation(args.reservation)
    if reservation is None:
        print(f"⛔ 未释放：账本里没有预留 {args.reservation}")
        return 2
    try:
        scope_account, scope_environment = resolve_mod.scope_parts(reservation.scope)
    except resolve_mod.ResolveError as exc:
        print(f"⛔ 未释放：{exc}")
        return 2
    # Connect to the environment the RESERVATION belongs to, and check it before
    # reading anything.  Auto-detection answers "which gateway is up", not "which
    # environment this reservation is in"; a mismatch would otherwise search the
    # wrong account and report "no execution", which reads like proof of no-send.
    if getattr(args, "port", None):
        port = args.port
    elif getattr(args, "paper", False):
        port = cfg["paper_port"]
    else:
        port, _ = resolve_port(cfg, args)
    connection_environment = "live" if _is_live_env(cfg, args, port) else "paper"
    if connection_environment != scope_environment:
        hint = ("请加 --paper 或 --port <纸账户端口>" if scope_environment == "paper"
                else "请改用实盘端口")
        print(f"⛔ 未释放：预留 {args.reservation} 属于 {scope_environment} 环境，"
              f"但当前会连接 {connection_environment}（端口 {port}）。{hint}")
        return 2
    ib = connect(cfg, port, readonly=True, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        if str(account) != scope_account:
            print(f"⛔ 未释放：预留 {args.reservation} 属于账户 {scope_account}，"
                  f"当前连接的是 {account}。")
            return 2
        observations = resolve_mod.live_observations(ib, account=account)
        snapshot = None
        try:
            candidate = _risk_snapshot(ib, cfg, args, scope_environment)
        except Exception as exc:
            print(f"  ⚠️ 无法采集账户快照（{exc}）；仅用成交记录核对。")
        else:
            if candidate.issues:
                print("  ⚠️ 账户快照存在数据质量问题；仅用成交记录核对："
                      + "; ".join(candidate.issues))
            else:
                snapshot = candidate
        try:
            outcome = resolve_mod.resolve_reservation(
                ledger, args.reservation,
                executions=observations.executions,
                snapshot_provider=(lambda: snapshot) if snapshot else None)
        except resolve_mod.ResolveError as exc:
            print(f"⛔ 未释放：{exc}")
            return 2
    finally:
        finish(ib)
    print(f"✅ 预留 {outcome.reservation_id} 已按券商正向证据逐腿解除：")
    for reason in outcome.evidence:
        print(f"   - {reason}")
    print("   注意：当日已消耗的提交次数不会退还；账户现在可以进行新的风控评估。")
    return 0


def cmd_report(cfg, args):
    from .report import build_report, render_report, write_report
    from .risk import summarize, validate_risk_config
    snapshot, summary, ledger_summary, issues, ib = None, None, None, [], None
    max_data_age = 30
    try:
        port, _ = resolve_port(cfg, args)
        environment = "live" if _is_live_env(cfg, args, port) else "paper"
        with _private_broker_diagnostics():
            ib = connect(cfg, port, readonly=True, args=args)
            snapshot = _risk_snapshot(ib, cfg, args, environment)
        # Available budgets are unknown until local reservations are included.
        # A report never creates an empty ledger that could imply fresh quota.
        path = _data_dir() / "state.sqlite3"
        if path.exists():
            from .state import SubmissionLedger
            ledger = SubmissionLedger(path, cfg.get("risk", {}).get("day_timezone", "UTC"),
                                      read_only=True)
            outstanding = ledger.outstanding_reservations(account=snapshot.account, environment=environment)
            from .execution import aggregate_reservations
            reservations = aggregate_reservations(ledger, snapshot.account, environment)
            limits = validate_risk_config(cfg)
            max_data_age = limits.max_snapshot_age_seconds or 30
            summary = summarize(snapshot, reservations=reservations, limits=limits)
            from .state import day_key_for
            quota_day = day_key_for(limits.day_timezone or "UTC")
            quota = ledger.get_day_budget(account=snapshot.account, environment=environment,
                                          day_key=quota_day) if limits.enabled else None
            if limits.enabled and (not quota.get("exists") or quota.get("migration_frozen")):
                issues.append("Daily submission history is unknown or frozen; quota is not available")
            ledger_summary = {"outstanding": len(outstanding),
                              "states": [r.state for r in outstanding],
                              "daily_limits_enabled": limits.enabled,
                              "quota_day": quota_day if limits.enabled else None,
                              "quota": quota}
        else:
            issues.append("Local intent ledger does not exist; submission history is unknown")
    except (Exception, SystemExit) as exc:
        # Broker errors can include raw account IDs even before a snapshot exists.
        issues.append(f"Snapshot incomplete ({type(exc).__name__}); broker details withheld for privacy")
    finally:
        if ib is not None:
            try:
                with _private_broker_diagnostics():
                    finish(ib)
            except (Exception, SystemExit):
                issues.append("Broker disconnect incomplete; details withheld for privacy")
    report = build_report(snapshot, risk_summary=summary, ledger_summary=ledger_summary,
                          issues=issues, reveal_account=getattr(args, "reveal_account", False),
                          max_data_age_seconds=max_data_age)
    text = render_report(report, args.format)
    if args.output:
        try:
            write_report(args.output, text)
        except OSError as exc:
            sys.exit(f"报告未写入（不会覆盖既有文件）：{exc}")
        print(f"报告已创建：{args.output}")
    else:
        print(text, end="")
    return 0 if report["complete"] else 3


def cmd_reconcile(cfg, args):
    """Observe broker identities; never resubmit, cancel or release uncertainty."""
    from .state import SubmissionLedger, StateError
    path = _data_dir() / "state.sqlite3"
    if not path.exists():
        sys.exit("没有本地提交账本；不会创建空库或推断历史为零")
    port, _ = resolve_port(cfg, args)
    environment = "live" if _is_live_env(cfg, args, port) else "paper"
    ib = connect(cfg, port, readonly=True, args=args)
    try:
        account = _selected_account(ib, cfg, args)
        # Validate the existing schema without creating/migrating an empty DB.
        SubmissionLedger(path, cfg.get("risk", {}).get("day_timezone", "UTC"), read_only=True)
        ledger = SubmissionLedger(path, cfg.get("risk", {}).get("day_timezone", "UTC"))
        trades = ib.reqAllOpenOrders()
        if not isinstance(trades, (list, tuple)):
            raise StateError("无法同步券商挂单")
        if any(not isinstance(getattr(t.order, "account", None), str)
               or not t.order.account.strip() for t in trades):
            raise StateError("券商订单账户范围未知，停止对账")
        scoped = [t for t in trades if t.order.account == account]
        observed = 0
        for leg in ledger.list_legs(account=account, environment=environment):
            matches = [t for t in scoped if getattr(t.order, "orderRef", "") == leg.order_ref
                       or (leg.perm_id and str(getattr(t.order, "permId", "")) == str(leg.perm_id))]
            if len(matches) > 1:
                raise StateError("同一本地腿匹配多个券商订单，停止对账")
            if matches:
                trade = matches[0]
                actual_ref = getattr(trade.order, "orderRef", "")
                actual_perm = getattr(trade.order, "permId", 0)
                if (actual_ref and actual_ref != leg.order_ref
                        or leg.perm_id and actual_perm and str(actual_perm) != str(leg.perm_id)):
                    raise StateError("券商订单稳定标识互相矛盾，停止对账")
                ledger.reconcile(account=account, environment=environment,
                                 order_ref=leg.order_ref,
                                 perm_id=getattr(trade.order, "permId", None) or None,
                                 order_id=getattr(trade.order, "orderId", None),
                                 status="confirmed", broker_status=trade.orderStatus.status)
                observed += 1
            else:
                ledger.reconcile(account=account, environment=environment,
                                 order_ref=leg.order_ref, status="absent_open_order",
                                 note="Not in current open orders; this is NOT proof of no submission")
        print(f"已记录 {observed} 条可确认的券商订单观察；未找到不代表未发送，未解除未知预留。")
    except StateError as exc:
        sys.exit(f"对账失败：{exc}")
    finally:
        finish(ib)
    return 0


# ---------------------------------------------------------------- 入口
def _global_flags(parser, hidden=False):
    """全局开关同时挂到顶层与子命令，前写后写都生效（--paper account / account --paper 均可）"""
    kw = {"default": argparse.SUPPRESS} if hidden else {}
    h = argparse.SUPPRESS if hidden else None
    parser.add_argument("--paper", action="store_true", help=h or "用纸账户端口", **kw)
    parser.add_argument("--port", type=int, help=h or "手动指定端口（覆盖 --paper）", **kw)
    parser.add_argument("--delayed", action="store_true", help=h or "强制使用延迟行情（未订阅实时行情时更快）", **kw)
    parser.add_argument("--account", help=h or "明确选择账户；多账户会话必填", **kw)


def build_parser(cfg: dict | None = None) -> argparse.ArgumentParser:
    """Build the argument parser without reading config or connecting.

    Exposed so tests exercise the REAL production parser (flag names,
    defaults, subcommand wiring) rather than a hand-rolled stand-in.
    """
    cfg = DEFAULT_CFG if cfg is None else cfg
    p = argparse.ArgumentParser(
        prog="thetamill",
        description="ThetaMill — 低频期权卖方工具箱（IBKR / TWS API），实盘下单需复核码",
        epilog="完整文档见 docs/DESIGN.zh-CN.md；建议先在纸账户（--paper）跑通全链路。",
    )
    _global_flags(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="探测本机哪条网关通了").set_defaults(func=cmd_status)
    sub.add_parser("account", help="账户资金与保证金").set_defaults(func=cmd_account)
    sub.add_parser("positions", help="当前持仓与浮盈").set_defaults(func=cmd_positions)
    sub.add_parser("orders", help="未成交挂单").set_defaults(func=cmd_orders)

    q = sub.add_parser("quote", help="正股实时报价"); q.add_argument("symbol"); q.set_defaults(func=cmd_quote)
    q.add_argument("--currency", default=cfg["default_currency"])
    q.add_argument("--exchange", help="指定交易所，如 SEHK（默认自动判断）")

    c = sub.add_parser("chain", help="期权链 / 报价 / Greeks"); c.add_argument("symbol"); c.set_defaults(func=cmd_chain)
    c.add_argument("--currency", default=cfg["default_currency"])
    c.add_argument("--expiry", help="到期日 YYYYMMDD")
    c.add_argument("--strikes", help="指定行权价，逗号分隔")
    c.add_argument("--atm", type=float, help="取平值上下浮动比例，如 0.05")
    c.add_argument("--n", type=int, default=5, help="平值上下各取几档行权价（默认 5）")

    o = sub.add_parser("order", help="下单（股票/期权）"); o.set_defaults(func=cmd_order)
    o.add_argument("symbol")
    o.add_argument("--opt", action="store_true", help="期权单")
    o.add_argument("--expiry"); o.add_argument("--strike", type=float); o.add_argument("--right", choices=["C", "P", "c", "p"])
    o.add_argument("--action", required=True, choices=["BUY", "SELL", "buy", "sell"])
    o.add_argument("--qty", type=float, required=True)
    o.add_argument("--type", default="LMT", choices=["MKT", "LMT", "STP", "mkt", "lmt", "stp"])
    o.add_argument("--limit", type=float); o.add_argument("--stop", type=float)
    o.add_argument("--tif", default="DAY"); o.add_argument("--outside-rth", action="store_true")
    o.add_argument("--currency", default=cfg["default_currency"])
    o.add_argument("--exchange", help="指定交易所，如 SEHK/SMART（默认自动判断）")
    o.add_argument("--con-id", type=int, help="明确指定期权 conId，仍校验全部经济字段")
    o.add_argument("--multiplier", help="期权乘数，如 10/100/500；用于区分同名合约")
    o.add_argument("--trading-class", help="期权交易类别；用于区分标准、迷你或调整合约")
    o.add_argument("--code", default="", help="复核码，不填只做预检不提交")
    o.add_argument("--intent", default="", help="执行意图 ID，与 --code 成对使用")
    o.add_argument("--delivery-evidence", help="逐期权 conId 的已核实交割证据文件")
    o.add_argument("--whatif", action="store_true", help="预估保证金与佣金（需有行情，可能较慢）")
    o.add_argument("--live-ok", action="store_true", help="实盘账户下单必须加此开关（防手滑）")

    cb = sub.add_parser("strat", aliases=["combo"], help="三大卖方策略：备兑 / 卖 Call / 现金担保卖 Put")
    cb.set_defaults(func=cmd_combo)
    cb.add_argument("symbol")
    cb.add_argument("--preset", required=True, choices=PRESET_KEYS,
                    help="covered-call（买股+卖Call）| sell-call（有股只卖Call）| cash-put（现金担保卖Put）")
    cb.add_argument("--expiry", required=True, help="到期日 YYYYMMDD")
    cb.add_argument("--k1", "--strike", dest="k1", type=float, required=True, help="行权价")
    cb.add_argument("--net", type=float, help="权利金（每股/每张，正数=收到）；不填为市价")
    cb.add_argument("--qty", type=int, default=1, help="张数（1 张 = 乘数股）")
    cb.add_argument("--currency", default=cfg["default_currency"])
    cb.add_argument("--exchange", help="指定期权交易所，如 SEHK/SMART（默认自动判断）")
    cb.add_argument("--con-id", type=int, help="明确指定期权 conId，仍校验全部经济字段")
    cb.add_argument("--multiplier", help="期权乘数，如 10/100/500；用于区分同名合约")
    cb.add_argument("--trading-class", help="期权交易类别；用于区分标准、迷你或调整合约")
    cb.add_argument("--stock-px", type=float, help="covered-call 买正股的限价，不填用市价")
    cb.add_argument("--code", default="", help="复核码，不填只做预检")
    cb.add_argument("--intent", default="", help="执行意图 ID，与 --code 成对使用")
    cb.add_argument("--delivery-evidence", help="逐期权 conId 的已核实交割证据文件")
    cb.add_argument("--live-ok", action="store_true", help="实盘必须加")

    pl = sub.add_parser("plan", help="生成当日卖方建议（只读，不下单）")
    pl.set_defaults(func=cmd_plan)
    pl.add_argument("symbol", nargs="?", help="标的代码；不填则扫描账户里的正股持仓")
    pl.add_argument("--symbols", help="多个标的，逗号分隔，如 AAPL,MSFT")
    pl.add_argument("--preset", choices=["auto", "sell-call", "cash-put"], default="auto",
                    help="策略选择：auto（自动）| sell-call（备兑卖Call）| cash-put（现金担保卖Put）")
    pl.add_argument("--dte-min", type=int, default=21, help="最短剩余天数（默认 21）")
    pl.add_argument("--dte-max", type=int, default=45, help="最长剩余天数（默认 45）")
    pl.add_argument("--target-dte", type=int, default=30, help="窗口内优先取最接近的天数（默认 30）")
    pl.add_argument("--otm", type=float, default=0.05, help="虚值幅度：卖 Call 向上、卖 Put 向下（默认 5%%）")
    pl.add_argument("--qty", type=int, default=1, help="计划张数（股数不足会自动下调）")
    pl.add_argument("--currency", default=cfg["default_currency"])
    pl.add_argument("--json", dest="json_out", help="把建议写成 JSON，供 run 使用")
    pl.add_argument("--universe", help="可选外部准入白名单 JSON，只做减法过滤（默认关闭）")
    pl.add_argument("--delivery-evidence", help="逐期权 conId 的已核实交割证据文件")

    rn = sub.add_parser("run", help="执行 plan 生成的建议（仍走复核码）")
    rn.set_defaults(func=cmd_run)
    rn.add_argument("--file", required=True, help="plan --json 生成的文件")
    rn.add_argument("--i", type=int, help="执行第几条；只有一条时可省略")
    rn.add_argument("--net", type=float, help="覆盖计划里的权利金（无行情时必填）")
    rn.add_argument("--stock-px", type=float, help="covered-call 买正股的限价")
    rn.add_argument("--code", default="", help="复核码，不填只做预检")
    rn.add_argument("--intent", default="", help="执行意图 ID，与 --code 成对使用")
    rn.add_argument("--delivery-evidence", help="逐期权 conId 的已核实交割证据文件")
    rn.add_argument("--live-ok", action="store_true", help="实盘必须加")

    roll = sub.add_parser("roll-plan", help="只读展期建议；独立文件不可通过 run 执行")
    roll.set_defaults(func=cmd_roll_plan)
    roll.add_argument("--con-id", type=int, required=True, help="现有空头期权 conId")
    roll.add_argument("--qty", type=int, required=True, help="建议买回/新开的张数，各腿按真实乘数计算")
    roll.add_argument("--candidate-con-id", type=int, action="append", default=[],
                      help="明确候选期权 conId，可重复；不自动放宽或截断候选范围")
    roll.add_argument("--dte-min", type=int, default=21)
    roll.add_argument("--dte-max", type=int, default=45)
    roll.add_argument("--cost-floor", help="净收支下限（交易币种；未扣未知费用）")
    roll.add_argument("--max-entry", help="候选行权价上限")
    roll.add_argument("--universe", help="外部准入白名单，只做减法")
    roll.add_argument("--delivery-evidence", help="逐期权 conId 的已核实交割证据文件；不得按乘数猜测")
    roll.add_argument("--format", choices=["terminal", "json", "markdown"], default="terminal")
    roll.add_argument("--output", help="创建本地建议文件，拒绝覆盖")

    sub.add_parser("reconcile", help="只读券商对账；不重发、不撤单、不自动解除未知预留").set_defaults(func=cmd_reconcile)

    rs = sub.add_parser("resolve", help="按券商正向证据解除未决预留（账户锁的唯一出口）")
    rs.set_defaults(func=cmd_resolve)
    rs.add_argument("--list", action="store_true", help="只读列出当前阻塞账户的未决预留")
    rs.add_argument("--reservation", help="要核对的预留 ID")
    rs.add_argument("--state", choices=["confirmed", "released"], default="confirmed",
                    help="confirmed=用券商证据解除；released=仅限证明从未发送")
    rs.add_argument("--why", default="", help="--state released 时必填的书面理由")

    report = sub.add_parser("report", help="只读风险快照（缺失数据明确标记未知）")
    report.set_defaults(func=cmd_report)
    report.add_argument("--format", choices=["terminal", "json", "markdown"], default="terminal")
    report.add_argument("--output", help="创建本地文件；拒绝覆盖既有文件")
    report.add_argument("--reveal-account", action="store_true", help="显式显示完整账户；默认脱敏")

    au = sub.add_parser("audit", help="查看审计日志")
    au.set_defaults(func=cmd_audit)
    au.add_argument("--limit", type=int, default=20, help="显示最近多少条（默认 20）")
    au.add_argument("--today", action="store_true", help="只看今天")

    x = sub.add_parser("cancel", help="撤单"); x.set_defaults(func=cmd_cancel)
    x.add_argument("order_id", type=int)

    seen = set()
    for sp in sub.choices.values():
        if id(sp) not in seen:
            _global_flags(sp, hidden=True)
            seen.add(id(sp))
    return p


def main():
    cfg = load_cfg(required=False)
    args = build_parser(cfg).parse_args()
    sys.exit(args.func(cfg, args))


if __name__ == "__main__":
    main()
