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
import os
import socket
import sys
from datetime import datetime, date, timedelta
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
                     exchange="", multiplier="100", currency="", tradingClass=""):
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
from .universe import (load_universe, universe_allowed, universe_gate,  # noqa: E402
                       validate, meta)


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
    if ORDER_LOG.exists():
        try:
            for line in ORDER_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("code") == code:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
    return False


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


def _opt(ib, symbol, expiry, strike, right, currency, exchange=None):
    """取期权合约：已知本币交易所时优先（港股期权只能走 SEHK），再试 SMART"""
    if exchange:
        cands = [exchange]
    else:
        cands = []
        if currency.upper() in XCHG_FALLBACK:
            cands.append(XCHG_FALLBACK[currency.upper()])
        cands.append("SMART")
    for x in cands:
        try:
            oc = Option(symbol.upper(), expiry, strike, right.upper(), x, currency=currency)
            q = [c for c in ib.qualifyContracts(oc) if c and c.conId]
            if q:
                return q[0], x
        except Exception:
            continue
    return None, None


def _multiplier(ib, contract):
    """合约乘数：股票期权通常 100，取不到就按 100 算"""
    m = getattr(contract, "multiplier", None)
    if isinstance(m, (int, float)) and m > 0:
        return int(m)
    if isinstance(m, str) and m.isdigit() and int(m) > 0:
        return int(m)
    try:
        d = ib.reqContractDetails(contract)
        if d:
            dm = getattr(d[0].contract, "multiplier", None)
            if isinstance(dm, (int, float)) and dm > 0:
                return int(dm)
            if isinstance(dm, str) and dm.isdigit() and int(dm) > 0:
                return int(dm)
    except Exception:
        pass
    return 100


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
                s = ib.accountSummary()
                for k in ("NetLiquidation", "AvailableFunds", "BuyingPower"):
                    for row in s:
                        if row.tag == k:
                            print(f"       {k}: {fmt(row.value)} {row.currency}")
            except Exception as e:
                print(f"       账户读取失败: {e}")
            finish(ib)
        except Exception:
            print(f"  ❌ {p} {label} — 未监听")
    if not alive:
        print("\n结论：本机没有可用的 IBKR 网关。请先安装并登录 IB Gateway。")
    return 0


def cmd_account(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True)
    print(f"环境：{env}  端口：{port}")
    rows = ib.accountSummary()
    keep = {
        "AccountType": "账户类型", "NetLiquidation": "净清算值", "TotalCashValue": "现金",
        "AvailableFunds": "可用资金", "BuyingPower": "购买力", "ExcessLiquidity": "超额流动性",
        "MaintMarginReq": "维持保证金", "InitMarginReq": "初始保证金", "GrossPositionValue": "持仓市值",
        "UnrealizedPnL": "浮动盈亏", "RealizedPnL": "已实现盈亏",
    }
    for r in rows:
        if r.tag in keep:
            print(f"  {keep[r.tag]:<8} {fmt(r.value)} {r.currency}")
    finish(ib)
    return 0


def cmd_positions(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True)
    ib.sleep(2)
    print(f"环境：{env}  端口：{port}")
    pf = ib.portfolio()
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
    finish(ib)
    return 0


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
    ib = connect(cfg, port, readonly=True)
    ib.sleep(1.5)
    trades = ib.reqAllOpenOrders() or []
    trades = [t for t in trades if t.orderStatus.status not in ("Filled", "Cancelled", "ApiCancelled", "Inactive")]
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
    finish(ib)
    return 0


def cmd_cancel(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=False, args=args)
    ib.sleep(1.5)
    hit = [t for t in (ib.reqAllOpenOrders() or []) if t.order.orderId == args.order_id]
    if not hit:
        sys.exit(f"未找到 #{args.order_id} 的挂单")
    t = hit[0]
    ib.cancelOrder(t.order)
    ib.sleep(1.5)
    print(f"已请求撤单 #{args.order_id} → 状态 {t.orderStatus.status}")
    finish(ib)
    return 0


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
    ib = connect(cfg, port, readonly=False, args=args)
    try:
        ib.reqAllOpenOrders()
        ib.sleep(0.5)
    except Exception:
        pass

    # 1) 合约
    if args.opt:
        for req in ("expiry", "strike", "right"):
            if not getattr(args, req):
                sys.exit(f"期权单缺少 --{req}")
        exchange = getattr(args, "exchange", None) or ("SEHK" if args.currency == "HKD" else "SMART")
        contract = Option(args.symbol.upper(), args.expiry, args.strike, args.right.upper(),
                          exchange, currency=args.currency)
        label = f"{args.symbol.upper()} {args.expiry} {args.strike}{args.right.upper()}"
        try:
            q = ib.qualifyContracts(contract)
        except Exception:
            q = []
        if (not q or not q[0].conId) and exchange == "SEHK":
            try:
                contract = Option(args.symbol.upper(), args.expiry, args.strike, args.right.upper(),
                                  "SMART", currency=args.currency)
                q = ib.qualifyContracts(contract)
            except Exception:
                q = []
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
    qty = args.qty
    is_live = _is_live_env(cfg, args, port)

    if args.opt and side == "SELL":
        mult = _multiplier(ib, contract)
        qty_val = int(qty) if float(qty).is_integer() else qty
        if args.right.upper() == "C":
            have = _shares(ib, args.symbol, args.currency)
            if have < 0:
                sys.exit(f"\n⛔ 账户持有 {have:g} 股 {args.symbol} 空头头寸（做空），禁止开具备兑 Call。\n")
            busy_shares = _short_calls_shares(ib, args.symbol, args.currency) + _pending_short_calls_shares(ib, args.symbol, args.currency)
            busy_count = _short_calls(ib, args.symbol, args.currency) + _pending_short_calls(ib, args.symbol, args.currency)
            if busy_count > 0 and busy_shares == 0:
                busy_shares = busy_count * mult
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

    mode = "live" if is_live else "paper"
    payload = dict(mode=mode, port=port, contract=contract.conId, sec=contract.secType,
                   action=side, qty=qty, otype=args.type.upper(), px=px, tif=args.tif,
                   outside_rth=bool(args.outside_rth))
    code = _code(payload)

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

    if args.code != code:
        print(f"\n⚠️  这是{'实盘' if is_live else '纸账户'}订单，尚未提交。")
        print(f"    复核码：{code}")
        print(f"    确认无误后，用同一条命令追加  --code {code}  再跑一次即会真实发出。\n")
        finish(ib)
        return 2

    if _is_code_consumed(code):
        sys.exit(f"\n⛔ 复核码 {code} 已经被消费执行过，不可重复提交同一笔委托！如需重新下单，请重新生成复核码。\n")

    # 4) 真正下单
    trade = ib.placeOrder(contract, order)
    _consume_code(code)
    order_id = trade.order.orderId
    print(f"已提交 #{order_id}，等待回执…")
    st, filled, avg = "UNKNOWN", 0.0, None
    try:
        for _ in range(10):
            ib.sleep(1)
            st = trade.orderStatus.status
            if st in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        st = trade.orderStatus.status
        filled = trade.orderStatus.filled
        avg = trade.orderStatus.avgFillPrice
        print(f"  状态 {st}  已成交 {fmt(filled,0)}  均价 {fmt(avg,3)}")
        for lg in trade.log[-3:]:
            if lg.message:
                print(f"  日志 {lg.status}: {_dec(lg.message)}")
    except Exception as e:
        print(f"  ⚠️ 回执等待中断：{type(e).__name__}: {e}")
        print("  → 网关掉线或重启了。订单是否生效必须重新登录后用  ./ib orders  核对，")
        print(f"     不要重复提交（orderId #{order_id} 已分配）。")
        st = "UNKNOWN"

    try:
        ORDER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ORDER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"), "env": env, "port": port,
                "orderId": order_id, "label": label, "action": side, "qty": qty,
                "type": args.type.upper(), "px": px, "tif": args.tif, "code": code,
                "status": st, "filled": filled, "avgFillPrice": avg,
            }, ensure_ascii=False, default=str) + "\n")
    except Exception as e:
        print(f"  ⚠️ 审计日志写入失败（订单已发出，不影响成交）：{e}")
    finish(ib)
    return 0


def _write_audit(env, port, preset, data):
    """审计日志：每笔真实提交的委托落一份，写不进去不影响已发出的单"""
    try:
        ORDER_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "env": env, "port": port,
               "kind": "strat", "preset": preset}
        rec.update(data)
        with ORDER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except OSError as e:
        print(f"  ⚠️ 审计日志写入失败（订单已发出，不影响成交）：{e}")


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
    for p in (ib.portfolio() or []):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if getattr(c, "secType", "") == "STK" and getattr(c, "symbol", "").upper() == sym and getattr(c, "currency", "") == currency:
            return float(getattr(p, "position", 0) or 0)
    return 0.0


def _contract_multiplier(c) -> float:
    """提取合约乘数，默认 100.0"""
    m = getattr(c, "multiplier", None)
    if isinstance(m, (int, float)) and m > 0:
        return float(m)
    if isinstance(m, str):
        try:
            val = float(m)
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return 100.0


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
    for p in (ib.portfolio() or []):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if (getattr(c, "secType", "") == "OPT" and
                getattr(c, "symbol", "").upper() == sym and
                str(getattr(c, "currency", "")).upper() == cur and
                getattr(c, "right", "").upper() == "C" and
                getattr(p, "position", 0) < 0):
            mult = _contract_multiplier(c)
            total_shares += abs(float(p.position)) * mult
    return total_shares


def _trade_remaining(trade) -> float:
    try:
        rem = getattr(trade.orderStatus, "remaining", None)
        if rem is not None and float(rem) >= 0:
            return float(rem)
    except Exception:
        pass
    try:
        tot = float(trade.order.totalQuantity or 0)
        filled = float(trade.orderStatus.filled or 0)
        return max(0.0, tot - filled)
    except Exception:
        return 0.0


def _pending_short_calls(ib, symbol, currency):
    """未完全成交的空头 Call 挂单张数（按未成交张数 remaining 汇总）"""
    trades = ib.openTrades() if callable(getattr(ib, "openTrades", None)) else []
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
    trades = ib.openTrades() if callable(getattr(ib, "openTrades", None)) else []
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
            mult = _contract_multiplier(c)
            total_shares += rem * mult
    return total_shares


def _pending_cash_puts(ib, currency):
    """未完全成交的卖 Put 挂单所需现金担保合计"""
    trades = ib.openTrades() if callable(getattr(ib, "openTrades", None)) else []
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
            mult = _contract_multiplier(c)
            strike = float(getattr(c, "strike", 0.0) or 0.0)
            total_cash += strike * mult * rem
    return total_cash


def _short_puts_collateral(ib, currency):
    """统计当前账户在某币种下已有已成交空头 Put 的现金担保总额（strike * mult * |position|）"""
    total_cash = 0.0
    cur_u = str(currency).upper()
    for p in (ib.portfolio() or []):
        c = getattr(p, "contract", None)
        if not c:
            continue
        if (getattr(c, "secType", "") == "OPT" and
                str(getattr(c, "currency", "")).upper() == cur_u and
                getattr(c, "right", "").upper() == "P" and
                getattr(p, "position", 0) < 0):
            mult = _contract_multiplier(c)
            strike = float(getattr(c, "strike", 0.0) or 0.0)
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
    for v in (ib.accountValues() or []):
        cur = (getattr(v, "currency", "") or "").upper()
        if not cur:
            continue
        tag = getattr(v, "tag", "")
        if tag == "TotalCashValue":
            try:
                f = float(v.value)
            except (TypeError, ValueError):
                continue
            if cur not in cash_map or f < cash_map[cur]:
                cash_map[cur] = f
        elif tag in ("AvailableFunds", "FullAvailableFunds"):
            try:
                f = float(v.value)
            except (TypeError, ValueError):
                continue
            # 同币种多标签时取较保守（较小）的一份，避免高估可用资金
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
        pending_cur = _pending_cash_puts(ib, cur_u)
        short_cur = _short_puts_collateral(ib, cur_u)
        total_locked_cur = pending_cur + short_cur
        direct = cash_map.get(cur_u)
        if direct is not None:
            cash = direct - total_locked_cur
            ok = cash >= need
            if not ok:
                return False, (f"需要 {fmt(need)} {currency}，账户实际现金 TotalCashValue 仅 {fmt(cash)} {currency}"
                               f"（AvailableFunds 融资额度不可用于现金担保）❌")
            pend_txt = f"（已扣减挂单及存量占用 {fmt(total_locked_cur)} {currency}）" if total_locked_cur > 0 else ""
            return True, f"需要 {fmt(need)} {currency}，账户实际现金 {fmt(cash)} {currency}{pend_txt} ✅"

        base_cash = cash_map.get("BASE")
        if base_cash is None or cur_u == str(base).upper():
            return False, f"需要 {fmt(need)} {currency}，无法确认账户实际现金，安全起见拒绝放行 ❌"
        rate = _fx(ib, base, cur_u)
        if rate is None or rate <= 0:
            return False, f"需要 {fmt(need)} {currency}，无法获取 {base}/{currency} 汇率，安全起见拒绝放行 ❌"

        base_pending = _pending_cash_puts(ib, base)
        base_short = _short_puts_collateral(ib, base)
        base_locked = base_pending + base_short
        cur_locked_in_base = total_locked_cur / rate
        net_base = base_cash - base_locked - cur_locked_in_base
        conv = net_base * rate
        ok = conv >= need
        if not ok:
            return False, (f"需要 {fmt(need)} {currency}，账户实际现金 TotalCashValue 仅 {fmt(conv)} {currency}"
                           f"（AvailableFunds 融资额度不可用于现金担保）❌")
        pend_txt = f"（已扣减挂单及存量占用 {fmt(base_locked)} {base} 及 {fmt(total_locked_cur)} {currency}）" if (base_locked > 0 or total_locked_cur > 0) else ""
        return True, (f"需要 {fmt(need)} {currency}，账户实际现金 {fmt(net_base)} {base} ≈ {fmt(conv)} {currency}{pend_txt}"
                      f"（汇率 {rate:.4f}） ✅")

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
    sym, preset = args.symbol.upper(), args.preset
    right = "P" if preset == "cash-put" else "C"
    qty, strike = args.qty, args.k1
    is_live = _is_live_env(cfg, args, port)

    ib = connect(cfg, port, readonly=False, args=args)
    try:
        ib.reqAllOpenOrders()
        ib.sleep(0.5)
    except Exception:
        pass

    opt, _exch = _opt(ib, sym, args.expiry, strike, right, args.currency)
    if not opt:
        sys.exit(f"期权合约无法识别：{sym} {args.expiry} {strike:g}{right}（可用 --currency 指定币种）")
    mult = _multiplier(ib, opt)
    need_shares = mult * qty

    # ---- 前提校验：这就是这个策略能不能成立的条件
    have = _shares(ib, sym, args.currency)
    stock_contract, stock_qty, stock_note = None, 0, ""
    busy_shares = _short_calls_shares(ib, sym, args.currency) + _pending_short_calls_shares(ib, sym, args.currency)
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
        if avail > 0:
            stock_qty = need_shares - int(avail)
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

    net = args.net
    otype = "LMT" if net not in (None, 0) else "MKT"

    mode = "live" if is_live else "paper"
    payload = dict(mode=mode, port=port, kind="strat", preset=preset, symbol=sym,
                   conId=opt.conId, strike=strike, right=right, expiry=args.expiry,
                   qty=qty, net=net, otype=otype, shares=stock_qty, stock_px=args.stock_px or 0)
    code = _code(payload)

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

    if args.code != code:
        print(f"\n⚠️  这是{'实盘' if is_live else '纸账户'}委托，尚未提交。")
        print(f"    复核码：{code}")
        print(f"    确认无误后，加上  --code {code}  再跑一次即会真实发出。\n")
        finish(ib)
        return 2

    if _is_code_consumed(code):
        sys.exit(f"\n⛔ 复核码 {code} 已经被消费执行过，不可重复提交同一笔委托！如需重新下单，请重新生成复核码。\n")

    done = []

    # 1) 备兑的正股腿：先买股，没买齐就不卖 Call
    if stock_contract:
        if args.stock_px:
            so = LimitOrder("BUY", stock_qty, args.stock_px)
        else:
            so = MarketOrder("BUY", stock_qty)
        so.tif = "DAY"
        ts = ib.placeOrder(stock_contract, so)
        _consume_code(code)
        for _ in range(10):
            ib.sleep(1)
            if ts.orderStatus.status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        print(f"  正股 #{ts.order.orderId} {ts.orderStatus.status}"
              f"  成交 {fmt(ts.orderStatus.filled, 0)}/{stock_qty} 股"
              f"  均价 {fmt(ts.orderStatus.avgFillPrice, 3)}")
        done.append(("正股", ts.order.orderId, ts.orderStatus.status))
        if ts.orderStatus.filled < stock_qty:
            print("\n  ⛔ 正股未全部成交，已跳过卖 Call —— 不卖没有股票覆盖的 Call。")
            print("     成交后改用：--preset sell-call")
            _write_audit(env, port, preset, dict(orderId=ts.order.orderId, status=ts.orderStatus.status,
                                                 legs=[f"正股 {stock_qty}"], qty=qty, net=net,
                                                 otype=so.orderType, code=code, filled=ts.orderStatus.filled))
            finish(ib)
            return 3

    # 2) 期权腿：卖出（收权利金）
    order = LimitOrder("SELL", qty, net) if otype == "LMT" else MarketOrder("SELL", qty)
    order.tif = "DAY"
    trade = ib.placeOrder(opt, order)
    _consume_code(code)
    order_id = trade.order.orderId
    print(f"  期权 #{order_id} 已提交，等待回执…")
    st, filled = "UNKNOWN", 0.0
    try:
        for _ in range(10):
            ib.sleep(1)
            st = trade.orderStatus.status
            if st in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        filled = trade.orderStatus.filled
        print(f"  状态 {st}  成交 {fmt(filled, 0)} 张  均价 {fmt(trade.orderStatus.avgFillPrice, 3)}")
        for lg in trade.log[-4:]:
            if lg.message:
                print(f"  日志 {lg.status}: {_dec(lg.message)[:140]}")
    except Exception as e:
        print(f"  ⚠️ 回执中断：{type(e).__name__}（重登后用 ./ib orders 核对，勿重复提交）")
    done.append(("期权", order_id, st))

    _write_audit(env, port, preset, dict(orderId=order_id, status=st, legs=[opt.localSymbol],
                                         qty=qty, net=net, otype=otype, code=code, filled=filled,
                                         extra=[f"{k}#{i}:{s}" for k, i, s in done]))
    finish(ib)
    return 0


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
    tries = []
    for k in cands:
        try:
            tries.append(Option(symbol.upper(), expiry, k, right, chain.exchange,
                                tradingClass=chain.tradingClass, currency=currency))
        except Exception:
            tries.append(None)
    live = [(k, c) for k, c in zip(cands, tries) if c is not None]
    if not live:
        return None, None
    try:
        qual = ib.qualifyContracts(*[c for _, c in live])
    except Exception:
        qual = []
    for (k, _c), q in zip(live, qual):
        if q and getattr(q, "conId", None):
            return q, k
    # 交易所不匹配时按币种再兜一次
    for k in cands:
        opt, _x = _opt(ib, symbol, expiry, k, right, currency)
        if opt:
            return opt, k
    return None, None


def cmd_plan(cfg, args):
    if not CFG_PATH.exists():
        load_cfg(required=True)
    port, env = resolve_port(cfg, args)
    ib = connect(cfg, port, readonly=True, force_delayed=getattr(args, "delayed", False), args=args)
    try:
        ib.reqAllOpenOrders()
        ib.sleep(0.5)
    except Exception:
        pass

    # 可选外部准入白名单：只做减法，默认关闭
    uni_doc = load_universe(args.universe) if getattr(args, "universe", None) else None
    uni_map = universe_allowed(uni_doc) if uni_doc else {}

    # 标的按 (代码, 币种) 收集：显式指定的用 --currency，扫持仓的用持仓自身币种
    targets = []
    if args.symbols:
        targets += [(s.strip().upper(), args.currency) for s in args.symbols.split(",") if s.strip()]
    if args.symbol:
        targets.append((args.symbol.upper(), args.currency))
    if not targets:
        for p in ib.portfolio():
            c = p.contract
            if c.secType == "STK" and p.position > 0:
                key = (c.symbol.upper(), c.currency)
                if key not in targets:
                    targets.append(key)
    if not targets:
        sys.exit("未指定标的，且账户无正股持仓 —— 用 `plan AAPL` 或 `--symbols AAPL,MSFT`")

    if uni_doc:
        print(f"🔒 外部准入白名单：{len(uni_map)} 只标的 | 签字 {uni_doc['approved_by']}"
              f" | 有效期至 {uni_doc['valid_until']}")
        print("   本工具只做减法，不评估标的质地、不因白名单自动发单。")
        before = len(targets)
        targets = [t for t in targets if t[0] in uni_map]
        if before > len(targets):
            print(f"   已过滤 {before - len(targets)} 个不在白名单的标的")
        if not targets:
            print("   ⛔ 所有标的都不在白名单内，本计划不产生建议。")
            finish(ib)
            return 0
        print()

    print(f"{env} | 端口 {port} | DTE {args.dte_min}~{args.dte_max}（目标 {args.target_dte}）"
          f" | 虚值目标 {args.otm:.0%} | 本计划只读，不下单")
    print()

    items = []
    for sym, cur in targets:
        stk, ch = _chain_safe(ib, sym, cur)
        if not stk:
            print(f"  {sym:<8} ✗ 无期权链，跳过")
            continue
        px = _spot(ib, stk)
        if not px:
            print(f"  {sym:<8} ✗ 取不到现价，跳过")
            continue
        expiry, dte = _pick_expiry(ch.expirations, args.dte_min, args.dte_max, args.target_dte)
        if not expiry:
            print(f"  {sym:<8} ✗ {args.dte_min}~{args.dte_max} 天内无到期日")
            continue

        have = _shares(ib, sym, cur)
        cost = _cost_basis(ib, sym, cur)
        note = []
        preset_choice = getattr(args, "preset", "auto")
        if preset_choice == "sell-call":
            right, preset = "C", "sell-call"
        elif preset_choice == "cash-put":
            right, preset = "P", "cash-put"
        else:
            if have >= 100:
                busy_shares = _short_calls_shares(ib, sym, cur) + _pending_short_calls_shares(ib, sym, cur)
                busy = _short_calls(ib, sym, cur) + _pending_short_calls(ib, sym, cur)
                avail = max(0.0, have - busy_shares)
                if avail >= 100:
                    right, preset = "C", "sell-call"
                else:
                    right, preset = "P", "cash-put"
                    note.append(f"现有 {have:g} 股已被 {busy} 张 Call 占满，自动转为现金担保卖 Put")
            elif have > 0:
                right, preset = "P", "cash-put"
                note.append(f"持有 {have:g} 股不足 1 手（需 100 股），自动转为现金担保卖 Put")
            else:
                right, preset = "P", "cash-put"

        target = px * (1 + args.otm) if right == "C" else px * (1 - args.otm)
        opt, strike = _resolve_option(ib, sym, expiry, ch.strikes, target, right, cur, ch,
                                      floor=cost if right == "C" else None)
        if not opt:
            print(f"  {sym:<8} ✗ {expiry} 无可用行权价（该月份阶梯可能不含目标价）")
            continue
        mult = _multiplier(ib, opt)
        prem, src = _quote_premium(ib, opt)

        qty = args.qty
        if right == "C":
            busy_shares = _short_calls_shares(ib, sym, cur) + _pending_short_calls_shares(ib, sym, cur)
            busy = _short_calls(ib, sym, cur) + _pending_short_calls(ib, sym, cur)
            avail = max(0.0, have - busy_shares)
            cover = int(avail // mult) if mult else 0
            if cover < qty:
                qty = cover
                if busy:
                    note.append(f"已有 {busy} 张空头 Call 及挂单占用 {busy_shares:g} 股（净可用 {avail:g} 股只够 {cover} 张）")
                else:
                    note.append(f"股数只够 {cover} 张")
            if cost and strike < cost:
                note.append(f"行权价低于成本 {cost:.2f}，被行权即锁定亏损")
            # 年化分母只算本单实际卖出的张数对应的担保市值，
            # 不能用用户总持仓（多出来的股票与本单收益无关，会稀释/夸大年化）。
            collat = qty * mult * cost if cost else qty * mult * px
            collat_txt = f"{qty * mult} 股"
        else:
            need_cash = strike * mult * qty
            ok, cov_note = _coverage(ib, cur, need_cash, require_cash=True)
            if ok is False:
                note.append(cov_note)
            collat, collat_txt = need_cash, f"{fmt(need_cash)} {cur}"

        if qty < 1:
            print(f"  {sym:<8} ✗ 可卖张数为 0"
                  + (f"（{note[0]}）" if note else "")
                  + " —— 用 covered-call 先建仓")
            continue

        ann = None
        if prem and collat and dte:
            ann = prem * mult * qty / collat * 365 / max(dte, 1)
        otm_pct = (strike - px) / px

        if uni_doc:
            ok, why = universe_gate(sym, preset, strike, uni_map)
            if not ok:
                print(f"  {sym:<8} ✗ 准入白名单拦截：{why}")
                continue

        items.append(dict(
            i=len(items) + 1, preset=preset, symbol=sym, currency=cur, expiry=expiry, dte=dte,
            strike=strike, spot=round(px, 3), otm_pct=round(otm_pct, 4), qty=qty, mult=mult,
            premium=prem, premium_src=src, collateral=round(collat, 2),
            collateral_txt=collat_txt, ann_return=round(ann, 4) if ann else None,
            note="；".join(note),
        ))

    if not items:
        print("没有可执行的建议。")
        finish(ib)
        return 0

    print(f"  {'#':<3}{'策略':<12}{'标的':<8}{'到期日':<11}{'DTE':>5}{'行权价':>10}"
          f"{'距现价':>9}{'参考权利金':>11}{'年化':>8}  担保")
    for it in items:
        prem = f"{it['premium']:.2f}" if it["premium"] else "-"
        ann = f"{it['ann_return']:.1%}" if it["ann_return"] else "-"
        print(f"  {it['i']:<3}{it['preset']:<12}{it['symbol']:<8}{it['expiry']:<11}{it['dte']:>5}"
              f"{it['strike']:>10.2f}{it['otm_pct']:>8.1%}{prem:>11}{ann:>8}  {it['collateral_txt']}")
        if it["note"]:
            print(f"     ⚠️ {it['note']}")

    if uni_doc:
        print(f"\n  🔒 以上建议已受准入白名单约束（签字 {uni_doc['approved_by']}，"
              f"有效期至 {uni_doc['valid_until']}）；标的质地由上游评估，本工具不判断。")

    if any(not it["premium"] for it in items):
        print("\n  参考权利金为 - 的条目：该市场无实时/延迟行情（未订阅），需自行定价或加 --net 手工填价。")

    if args.json_out:
        is_live = _is_live_env(cfg, args, port)
        out = Path(args.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "generated": datetime.now().isoformat(timespec="seconds"),
            "valid_until": (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds"),
            "is_live": is_live,
            "env": env, "port": port, "currencies": sorted({it["currency"] for it in items}),
            "window": dict(dte_min=args.dte_min, dte_max=args.dte_max, otm=args.otm),
            "universe": meta(uni_doc) if uni_doc else None,
            "items": items,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n  已写入计划文件：{out}")
        print(f"  执行方式：thetamill run --file {out} --i 1 [--paper]")

    finish(ib)
    return 0


def cmd_run(cfg, args):
    """执行 plan 生成的建议。不跳过任何闸门：实盘照旧要 live_enabled + --live-ok + 复核码"""
    path = Path(args.file).expanduser()
    if not path.exists():
        sys.exit(f"找不到计划文件：{path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"计划文件不是合法 JSON：{e}")
    port, env = resolve_port(cfg, args)
    is_live = _is_live_env(cfg, args, port)

    # 计划文件有效期校验（默认24小时有效）
    vu = doc.get("valid_until")
    if vu and str(vu) < datetime.now().isoformat():
        sys.exit("⛔ 计划文件已过期，请重新运行 plan 生成最新计划。")

    # 跨环境执行锁：实盘生成的计划不能在纸账户跑，反之亦然
    if doc.get("is_live") is not None and doc["is_live"] != is_live:
        sys.exit(f"⛔ 计划文件运行环境（{'实盘' if doc['is_live'] else '纸账户'}）与当前执行环境（{'实盘' if is_live else '纸账户'}）不一致，严禁跨环境执行！")

    items = doc.get("items") or []
    if not items:
        sys.exit("计划文件里没有可执行条目")

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
                  f"{it['strike']:g}{'C' if it['preset'] != 'cash-put' else 'P'} × {it['qty']}")
        sys.exit("\n用 --i N 指定要执行哪一条。")

    it = next((x for x in items if int(x.get("i", -1)) == int(idx)), None)
    if not it:
        sys.exit(f"计划里没有 #{idx} 这一条")

    # 二次校验：手工改过计划文件（抬高行权价 / 换标的）也绕不过白名单
    if uni:
        ok, why = universe_gate(it["symbol"], it["preset"], float(it["strike"]), uni_map)
        if not ok:
            sys.exit(f"⛔ 计划条目已不符合准入白名单：{why}\n"
                     f"   请用 plan --universe 重新生成，不要手工改动计划文件。")

    net = it.get("premium") if it.get("premium") else None
    if args.net is not None:
        net = args.net
    if net is None:
        sys.exit(f"\n⛔ #{idx} 没有可用的参考权利金（该市场无行情），无法自动定价。\n"
                 f"   请手工指定：thetamill run --file {path} --i {idx} --net 5.00\n")

    print(f"按计划 #{idx} 执行：{it['preset']} {it['symbol']} {it['expiry']} {it['strike']:g} × {it['qty']}")
    ns = argparse.Namespace(
        symbol=it["symbol"], preset=it["preset"], expiry=it["expiry"], k1=float(it["strike"]),
        net=float(net), qty=int(it["qty"]), currency=it.get("currency") or cfg["default_currency"],
        stock_px=args.stock_px, code=args.code, live_ok=args.live_ok,
        paper=args.paper, port=args.port, delayed=args.delayed,
    )
    return cmd_combo(cfg, ns)


def cmd_audit(cfg, args):
    if not ORDER_LOG.exists():
        print(f"暂无审计记录 —— 日志路径 {ORDER_LOG}")
        return 0
    rows = []
    for line in ORDER_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if args.today:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = [r for r in rows if str(r.get("ts", "")).startswith(today)]
    rows = rows[-max(args.limit, 1):]
    print(f"审计日志 {ORDER_LOG}")
    print(f"  {'时间':<20}{'策略':<14}{'单号':>7}  {'状态':<14}{'成交':>6}  {'环境':<12}")
    for r in rows:
        print(f"  {str(r.get('ts', ''))[:19]:<20}{str(r.get('preset') or r.get('label') or '-'):<14}"
              f"{str(r.get('orderId', '-')):>7}  {str(r.get('status', '-'))[:13]:<14}"
              f"{fmt(r.get('filled'), 0):>6}  {str(r.get('env', '-'))[:11]:<12}")
    print(f"\n  共 {len(rows)} 条（含纸账户与实盘，看「环境」列区分）")
    return 0


# ---------------------------------------------------------------- 入口
def _global_flags(parser, hidden=False):
    """全局开关同时挂到顶层与子命令，前写后写都生效（--paper account / account --paper 均可）"""
    kw = {"default": argparse.SUPPRESS} if hidden else {}
    h = argparse.SUPPRESS if hidden else None
    parser.add_argument("--paper", action="store_true", help=h or "用纸账户端口", **kw)
    parser.add_argument("--port", type=int, help=h or "手动指定端口（覆盖 --paper）", **kw)
    parser.add_argument("--delayed", action="store_true", help=h or "强制使用延迟行情（未订阅实时行情时更快）", **kw)


def main():
    cfg = load_cfg(required=False)
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
    o.add_argument("--code", default="", help="复核码，不填只做预检不提交")
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
    cb.add_argument("--stock-px", type=float, help="covered-call 买正股的限价，不填用市价")
    cb.add_argument("--code", default="", help="复核码，不填只做预检")
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

    rn = sub.add_parser("run", help="执行 plan 生成的建议（仍走复核码）")
    rn.set_defaults(func=cmd_run)
    rn.add_argument("--file", required=True, help="plan --json 生成的文件")
    rn.add_argument("--i", type=int, help="执行第几条；只有一条时可省略")
    rn.add_argument("--net", type=float, help="覆盖计划里的权利金（无行情时必填）")
    rn.add_argument("--stock-px", type=float, help="covered-call 买正股的限价")
    rn.add_argument("--code", default="", help="复核码，不填只做预检")
    rn.add_argument("--live-ok", action="store_true", help="实盘必须加")

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

    args = p.parse_args()
    sys.exit(args.func(cfg, args))


if __name__ == "__main__":
    main()
