"""外部准入白名单契约（可选，默认关闭）。

ThetaMill 只把这份文件当作「减法过滤器」：
- 不评估标的质地、不因白名单自动产生建议或自动发单；
- 不在白名单里的标的不出建议；
- cash-put 的行权价不得超过上游给出的「愿意接货价」max_entry；
- 白名单必须带人工签字与有效期，过期或缺少签字即拒绝。

契约由上游研究工具产出，本模块不关心它是谁生成的，也不反向调用它。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

SCHEMA = "thetamill-universe/1"


def validate(doc: dict) -> tuple[bool, list[str]]:
    problems: list[str] = []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        problems.append(f"schema 不匹配，期望 {SCHEMA}")
        return False, problems
    if not doc.get("approved_by"):
        problems.append("缺少 approved_by（人工签字）")
    if not doc.get("signed_at"):
        problems.append("缺少 signed_at")
    vu = doc.get("valid_until")
    if not vu:
        problems.append("缺少 valid_until")
    elif str(vu) < date.today().isoformat():
        problems.append(f"白名单已于 {vu} 过期，需重新签字")
    items = doc.get("items") or []
    if not items:
        problems.append("白名单为空")
    seen = set()
    for x in items:
        if not isinstance(x, dict):
            problems.append(f"白名单条目格式错误（不是字典）：{x}")
            continue
        s = str(x.get("symbol", "?")).upper()
        if s in seen:
            problems.append(f"重复标的 {s}")
        seen.add(s)
        if x.get("tier") != "ELIGIBLE":
            problems.append(f"{s} 的 tier 不是 ELIGIBLE")
        me = x.get("max_entry")
        if not isinstance(me, (int, float)) or me <= 0:
            problems.append(f"{s} 缺少有效的 max_entry")
        if x.get("demo"):
            problems.append(f"{s} 是演示数据，不应进入真实白名单")
    return (not problems), problems


def load_universe(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.exists():
        sys.exit(f"找不到准入白名单：{p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"准入白名单不是合法 JSON：{p}（{e}）")
    ok, problems = validate(doc)
    if not ok:
        sys.exit("⛔ 准入白名单未通过校验，本计划不产生建议：\n  - " + "\n  - ".join(problems))
    return doc


def universe_allowed(doc: dict) -> dict:
    return {str(x["symbol"]).upper(): x for x in doc.get("items", []) if x.get("tier") == "ELIGIBLE"}


def universe_gate(symbol: str, preset: str, strike: float, amap: dict) -> tuple[bool, str]:
    """只做减法的准入判定。返回 (是否放行, 原因)。"""
    a = amap.get(str(symbol).upper())
    if not a:
        return False, f"{symbol} 不在准入白名单"
    if preset == "cash-put":
        me = a.get("max_entry")
        if isinstance(me, (int, float)) and me > 0 and strike > me:
            return False, f"行权价 {strike:g} 超过愿意接货价 {me:g}"
    return True, ""


def meta(doc: dict) -> dict:
    """写进 plan --json 的精简副本，供 run 复验，防止手工改计划绕过白名单。"""
    return {k: doc.get(k) for k in ("schema", "approved_by", "signed_at", "valid_until", "items")}
