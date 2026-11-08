# 验证记录与证据边界

## 真实 IB Gateway 纸账户验收（2026-11-08）

本轮在**本地运行的 IB Gateway 10.x + 纸账户（端口 4002）**上完成了 A–F 执行链的真实网关验收；与下方纯离线检查点分列，不互相替代。

| 项目 | 实际执行 | 结果 |
|---|---|---|
| 连接与账户选择 | `status` / `account` / `positions` | 自动识别 4002，账户 `DUR***329`，读数正常 |
| 行情 | `quote AAPL` / `chain AAPL` | 延迟行情回退 `[10167]`；24 个期权到期日、131 档行权价 |
| 股票两阶段提交 | `order AAPL --action BUY --qty 1 --type LMT --limit 100` | 预检 rc=2 零发单；确认后 rc=0，券商回执 `[399]`，挂单 `#133` `PreSubmitted` |
| 期权两阶段提交 | `order AAPL --opt --expiry 20261204 --strike 350 --right P --action SELL --qty 1 --type LMT --limit 1.00` | 预检 rc=2 零发单；确认后 rc=0，券商回执 `[399]`，挂单 `#140` `PreSubmitted` |
| 多乘数担保 | 同上 Put（乘数 100、行权价 350、1 张） | 报告 `put_cash_obligations.USD = 35000.00`，`reserved_cash_base = 35000.0`，与 `350×100×1` 完全一致 |
| 撤单与账本恢复 | `cancel 133` / `cancel 140` | 券商回执 `[202]`，状态 `Cancelled`；账本腿置 `confirmed`，`has_unresolved_submissions = False` |
| 对账 | `reconcile` | 记录可确认券商观察；未找到不解除未知预留 |
| 全量离线门禁 | `python -m unittest discover -s tests` | Python 3.13.12 与 3.10.20 各 **778 项全绿**（0 错误 0 失败） |

本轮修复并验证的两项网关兼容缺陷：
1. `ib_async` 在 `connectAsync` 已完成账户同步后，再次同步调用 `reqAccountUpdates` 会因等不到 `accountDownloadEnd` 而无限阻塞 → 改为在已填充资金时仅以非阻塞底层订阅保活，`positions`/`collect_snapshot` 不再挂起。
2. 真实网关的 `AccountValue` 含 `AccountType`、`AccountReady`、`Cushion` 等空币种字段，且基准币通过 `$LEDGER-RealCurrency` 暴露 → 限定只校验货币相关标签，并支持单币种基准推断。

**仍未验证**：成交（本轮委托均在同日休市时段、收盘前未撮合）、成交后持仓变动、实盘账户、非美元合约、非标/调整合约交割结构。`delivery_evidence.json` 中的交割证据仅对具体 conId `937548729` 有效，不代表其他合约已获授权。

### 本轮新发现并修复：`roll-plan` 无限挂起（2026-11-08）

**现象**：`roll-plan --con-id 937548354 --qty 1` 在纸账户上**永不返回**。faulthandler 在 60s 与 75s 两次抓栈都停在 `selectors.select → run_until_complete → ib.py:676 reqTickers → roll_cli.py _fetch_tickers`。

**根因（三组只读探针分别证实）**：`_qualify_options()` 用 `reqContractDetails(Contract(conId=...))` 取得完整合约、完成身份与乘数校验后，**把校验过的详情合约丢弃**，只在传给 `reqTickers` 的表里保留了最初的 `Contract(conId=...)` 空壳。该空壳没有 `exchange`，IB 以错误 `[321] 原因 - Please enter exchange` 拒答且**不发送任何 tick**，快照 future 永不完成；而 `ib_async` 的 `IB.RequestTimeout` 默认值为 `0`（**永不超时**），于是整个进程阻塞。

| 探针 | 请求内容 | 结果 |
|---|---|---|
| A | `Contract(conId=937548354)` | **20.0s 超时**（探针自设上限）；`[321] Please enter exchange` |
| A | 详情派生的完整合约 | **0.3s** 返回 `marketDataType=1` |
| B | `conId` / `conId+secType` | 均**挂起**；同一 `[321]` |
| B | `conId+exchange` / `conId+secType+exchange` | 0.2–0.4s 返回 |

**修复**（`thetamill/roll_cli.py`）：
1. `_qualify_options()` 改为把**详情派生的合约**放进结果（`contracts[cid] = qualified`），空壳只用于最初的那次查找；若详情合约本身仍无 `exchange`，则该 conId 不予资格认定，而不是发出一个只可能阻塞的请求。
2. `_fetch_tickers()` 给 `RequestTimeout` 加**有界上限** `SNAPSHOT_TIMEOUT_SECONDS = 20.0`，**仅在这一次调用期间生效并在 `finally` 中还原**调用方原有策略；超时按既有降级路径记录 `market data request failed (TimeoutError)`，行情一律视为缺失，绝不放宽任何报价门禁。

**验证**：修复后同一命令在真实纸账户上 **3s 内返回** `rc=3`（无建议文档，非挂起），issues 明确写出 `ticker ... is DELAYED/FROZEN (marketDataType=3)`。另用不可路由空壳直连网关复验超时路径：**20.0s 返回**、`notes=['market data request failed (TimeoutError); all quotes treated as missing']`、`RequestTimeout` 调用前为 `0`、调用后仍为 `0`。

**回归**：`tests/test_roll_cli.py::SnapshotContractTests` 共 5 项（发往 `reqTickers` 的必须是可路由的详情合约、空壳不得被复用、请求失败降级而非抛出、超时在调用期间生效且事后还原、超时被报告为缺失行情）。全量离线 889 项通过，覆盖率门禁 `rc=0`（全局 84.1%）。CI 运行 `42464681864`（commit `527e6be`）在 Python 3.10 与 3.13 两条矩阵上 **9/9 步骤全绿**：源码测试 889 项、ruff 通过、覆盖率全局 84.0% 门禁通过、安装包测试 889 项与 17 个 `--help` 冒烟通过。

### 同一根因的通用化：`RequestTimeout` 全局设上限（2026-11-08）

上面的修复只解决了 `roll-plan`。更根本的问题是 `ib_async` 的 `IB.RequestTimeout` **默认值为 `0`（永不超时）**，因此**任何**得不到回应的阻塞请求（合约详情、行情快照、`whatIfOrder`、账户同步、`qualifyContracts`）都会让进程无限阻塞。这在实盘尤其危险：用户会以为命令还在跑。

修复（`thetamill/cli.py`）：`connect()` 建立会话后统一设置 `ib.RequestTimeout`，取值顺序为配置项 `request_timeout` → 默认 `DEFAULT_REQUEST_TIMEOUT = 30.0`；配置值为 `0`、负数、`NaN`/`Inf`、布尔或非数字时**一律回落到默认值**，无法通过配置关掉这道保护。`roll-plan` 的快照请求在此之上仅**进一步收紧**到 20 秒（`SNAPSHOT_TIMEOUT_SECONDS`），不会放宽调用方更严格的设置。

**真实网关验证**：`connect()` 后实测 `RequestTimeout = 30.0`（原为 `0`）；对不可路由空壳的请求 **20.0 秒**结束并记录 `market data request failed (TimeoutError)`；调用结束后 `RequestTimeout` 恢复为 `30.0`；同一会话内的正常正股请求仍在 5.5 秒内正常返回（`marketDataType=3`），说明上限没有破坏正常路径。

**回归**：`tests/test_account_scope.py::RequestTimeoutTests` 共 4 项（`connect()` 会设上限、配置值被尊重、十种不可用取值均回落默认、设上限不产生任何 `placeOrder`/`cancelOrder`）。全量离线 893 项通过；覆盖率门禁 `rc=0`（全局 84.1%，9 个模块全部达标）。

**边界**：本次只修复「不可路由合约 + 无上限等待」这一挂起路径，未证明延迟行情可用于现金估算——`_build_quotes` 仍拒收 `marketDataType != 1`，因此无实时订阅的账户上 `roll-plan` 只会给出「行情缺失」的阻塞结论。这与既有设计一致，不是本次修复放宽的项。


## 新一轮工程验收状态（2026-11-07）

以下为 2026-11-07 记录的可追溯离线检查点，不覆盖其后 2026-11-08 的真实网关验收，也不代表 A–F 已全部完成。

> **CI 状态说明（2026-11-08 核实）**：`.github/workflows/offline.yml` 自加入起 6 次 Actions 运行全部在 0 秒内失败且未创建任何 job——job 级 `env` 使用了 `runner` 上下文，而该上下文只在 step 级可用（`actionlint` 报 `context "runner" is not allowed here`）。因此上表所有"双环境通过"结论均来自**本地实跑**，不是 CI 通过。
>
> **CI 首次真实运行（2026-11-08 02:51 UTC，main `4e4de60`）**：修复后 Actions 运行 `42440262402` 成功，两个矩阵 job 均创建并通过——`offline (3.10)` 与 `offline (3.13)` 各 8 个步骤全绿（构建 wheel、源码测试、ruff E9/F 基线、空白/冲突标记、安装包测试与 16 个子命令帮助）。这是本仓库**第一条**真实 CI 通过记录，此前 6 次的 0 秒失败不应被引用为门禁通过。

> **静态检查基线（2026-11-08）**：`ruff check --select E9,F thetamill tests` 返回 `All checks passed!`（此前 tests/ 有 11 项 F401/F841 未处理）。该检查已作为独立步骤接入 `.github/workflows/offline.yml`，`pyproject.toml` 的 `dev` extra 固定了同一版本（`ruff==0.17.4`），本地与 CI 是同一个检查。基线建成后应要求变更不新增 E9/F 问题；这不等于 ruff 全规则（如 E501、I、B）通过，那些规则尚未启用。

> **语句覆盖率（2026-11-08 本地实测，非 CI 门禁）**：在隔离 venv（Python 3.10 + `ib_async` 2.1.0）用 `coverage run --source=thetamill -m unittest discover -s tests` 测得语句覆盖率 **83%（6993 条语句，未覆盖 1208 条）**；分模块：`report` 97%、`roll` 94%、`broker_state` 92%、`risk` 90%、`state` 89%、`planning` 89%、`plan_cli` 87%、`universe` 87%、`roll_cli` 86%、`execution` 82%、`submission_cli` 77%、`cli` 72%。这是**语句**覆盖率，不含分支覆盖率；仓库尚未配置覆盖率门禁或 CI 覆盖率上传，因此该数字不能替代逐入口的风险规则核对，也不能据此宣称任何交易场景已被完整覆盖。

| 固定候选 | 验证范围 | 实际结果 | 限制 |
|---|---|---|---|
| `834f1e60491c9bf62dbff8be246759404b0e7966` | A 合约身份、真实乘数、覆盖/资金及执行安全；全新浅 clone | Python 3.10.20 / 3.13.12 各 111 项源码测试及 111 项安装 wheel 测试通过；确认从 site-packages 导入 | 仅离线；PR #1 尚未合并，不是 main 上线或券商验收 |
| `f321eb0832850db0c699909202d40580e9493c32` | 公共快照、Decimal 风险、纯展期引擎及不可变行情/FX 证据基础 | 全新 clone 构建 wheel，显式安装实际依赖 ib_async 2.1.0 后 Python 3.10 / 3.13 各 300 项安装包测试通过 | 不含未提交的账本/执行适配层/CLI 接线；初次缺依赖的失败运行已废弃 |
| 本地完整工作区候选 | B–E 全量集成（权威 SQLite 账本、配额、净减例外、报告、展期建议、CLI 统一执行协议） | Python 3.13.12 / 3.10.20 各 778 项离线测试全绿（0 错误 0 失败）；独立构建 wheel `thetamill-0.1.0-py3-none-any.whl`（SHA-256 `6a707134e06caf52f727c6e8640cd828b3bd3d7ab7b9ea8153787280cea00329`），隔离纯净虚拟环境安装并验证 16 个子命令帮助正常 | 仅离线 Mock 与真实 ib_async 合约对象离线测试；零真实券商网络连接，未执行纸账户或实盘下单/撤单 |

第二个检查点 wheel SHA-256：`3c39c164519885b19016ac29011b8d425208fed10fcf6986f3db8d8fc6c9b7f2`。第三个完整集成候选 wheel SHA-256：`6a707134e06caf52f727c6e8640cd828b3bd3d7ab7b9ea8153787280cea00329`。构建存在 setuptools 既有 license 元数据弃用警告，不宣称零警告。

本地完整工作区集成测试矩阵（778 项，双环境 100% 通过）：
- `test_account_scope.py` (20)
- `test_broker_state.py` (27)
- `test_cli_logic.py` (38)
- `test_contract_identity.py` (25)
- `test_execution_safety.py` (34)
- `test_execution_state.py` (68)
- `test_plan_cli.py` (59)
- `test_planning.py` (60)
- `test_report.py` (31)
- `test_risk_limits.py` (78)
- `test_roll.py` (88)
- `test_roll_cli.py` (67)
- `test_state.py` (56)
- `test_submission_cli.py` (105)
- `test_submission_integration.py` (8)
- `test_universe.py` (14)
所有公共执行路径（`cmd_order`、`cmd_combo`、`cmd_run`）已完全统一代理至 `submission_cli.execute_prepared`，旧 `_code` 回退与直接 `ib.placeOrder` 完全移除；正向 handoff 严格核验 11 项规范合约身份字段；全新账本净减风险例外保留迁移冻结门禁。

## 当前阅读口径（Stage A 文档纠偏）

以下第 1–14 节保留 v0.1.0 以来的历史记录，不是本次修订重跑的券商证据。本次文档修改没有连接 Gateway、提交、撤单或验证成交。历史订单号、报价与账户余额仅指原记录中的时点，不能推定当前账户状态或新版风控已经通过同等验收；历史记录未提供可独立复现的完整原始回执和对应候选 SHA，须按这一限制使用。

| 证据等级 | 可证明的范围 | 本次文档修订状态 |
|---|---|---|
| 离线 Mock | 固定输入下的逻辑与失败路径；零真实券商调用 | 测试结果由主执行器按实际候选版本另行记录 |
| 离线真实 `ib_async` 合约对象 | 真实类字段/API 兼容；券商交互仍 Mock | 与纯 Mock 分列，不充当券商回执 |
| Gateway 只读 | 当时可读账户、持仓、挂单、合约与 FX | NOT_RUN；下文仅为历史记录 |
| 纸账户提交/撤单 | 券商接收与撤单回执，不等于成交 | NOT_RUN；下文仅为历史记录 |
| 成交及生成持仓 | 实际成交与持仓变化 | NOT_RUN；下文休市记录不覆盖 |

76 是历史测试数量，不是 100% 代码覆盖率。未配置和报告覆盖率测量时，不能声称语句、分支或所有风险场景全覆盖；不同版本测试数量也不能互相替代。100/200/500 的离线担保算术回归不证明所有真实合约交割结构均可自动处理。真实合约详情、跨客户端同步、行情/FX 与成交仍须单独授权和验证。

`readonly=True` 是 `ib_async` 客户端连接参数，不是服务端订单 ACL。JSON 复核码消费记录与尽力写入的 JSONL 审计不提供原子并发认领、资金预留或提交不明恢复；相关架构属于后续批次。`strat` 为主命令，`combo` 为同一实现的别名。旧 plan 缺少或不兼容合约身份时应重新生成，不能手改补齐。

---

# 历史记录（v0.1.0）

**时间**：2026-11-07（周六，美股/港股休市）
**环境**：IB Gateway 10.50 + 纸账户（端口 4002），Python 3.13 / ib_async 2.1.0
**历史结论范围**：原记录报告了所列纸账户查询、预检、部分委托提交/撤单与日志；不覆盖成交、新持仓上的 sell-call、当前版本或所有风险场景。行情与回执均为当时观察

> 原记录称所列委托均为纸账户，验证后已撤销且当时无残留挂单；本次未重新核对账户状态。

---

## 一、功能矩阵

| 命令 | 结果 | 备注 |
|---|---|---|
| `status` | 通过 | 自动识别 4001 / 4002 |
| `account` | 通过 | 净值、购买力、保证金、缓冲齐全 |
| `positions` | 通过 | 股票与期权持仓均可显示 |
| `orders` | 通过 | 组合单显示「×N 腿」与净价 |
| `quote` | 通过 | 美股与港股均取到延迟报价 |
| `chain` | 通过 | 到期日带 DTE，Greeks 可读（港股可选） |
| `plan` | 通过 | 美股 AAPL、港股 700 各出建议 |
| `run` | 通过 | 按计划执行，出复核码 |
| `order` | 通过 | 股票与期权单腿下单 + 撤单 |
| `strat` | 通过 | 三种策略准入校验符合预期 |
| `audit` | 通过 | 审计日志按时间倒序可查 |
| `cancel` | 通过 | 回执 `202`，状态转 Cancelled |

---

## 二、plan 实测输出

### 美股 AAPL（现价 342.37）

```
#  策略      标的   到期日      DTE  行权价  距现价  参考权利金   年化  担保
1  cash-put  AAPL  20261204    27   325.00  -5.1%     4.00    16.6%  32,500.00 USD
```

### 港股 700（现价 419.8）

```
#  策略      标的  到期日      DTE  行权价  距现价  参考权利金   年化  担保
1  cash-put  700  20261127    20   400.00  -7.1%     6.77    30.9%  40,000.00 HKD
```

港股那条同时验证了两件事：本币交易所回落（`HKD → SEHK`）与现金担保的汇率折算——`40,000 HKD` 对照美元余额时按 `USD.HKD ≈ 7.84` 折算为约 `5,102 USD`，远低于可用资金，故放行。

---

## 三、run 执行链

```bash
thetamill --paper run --file plan.json --i 1
#   → 打印订单摘要 + 复核码，不发单

thetamill --paper run --file plan.json --i 1 --code <CODE>
#   → 期权 #94 已提交，状态 PreSubmitted
#   → 日志 Warning 399: 委托单在 2026-11-09 09:30（美东）前不会被下达交易所

thetamill --paper audit --today
#   → 2026-11-07T12:05:06  cash-put  94  PreSubmitted  0  纸账户(Paper)
```

`Warning 399` 是周末休市的正常提示，不是失败。撤销后挂单数归零。

---

## 四、本轮发现并修复的问题

| 问题 | 现象 | 根因 | 处理 |
|---|---|---|---|
| 港股现价算成 1 元，行权价跑到 195 | `plan 700 --currency HKD` 输出 `距现价 -19600%` | IB 用 `-1` 表示「无行情」，`_spot` 把 `-1` 当有效价返回 | 取价时显式要求 `> 0`，并依次尝试 `marketPrice / last / close` 再退到日线 |
| 港股合约解析到错误市场 | 港币标的在 `SMART` 下解析成别的交易所 | `_stock` 先试 SMART，港股 `SMART` 会「成功」返回一个错合约 | 已知本币交易所时**优先**用它（`HKD → SEHK`），再回落 SMART |
| 港股期权合约无法识别 | `20261127 405P` qualify 返回空 | `reqSecDefOptParams` 的 strikes 是**所有到期日的并集**，11 月月度只含 400/420，不含 405 | 新增 `_resolve_option`：按「离目标最近且方向正确」依次试探 6 档真实存在的行权价 |
| `chain` 遇不存在的合约直接崩 | `AttributeError: 'NoneType'` | `qualifyContracts` 返回列表含 `None`，后续 `reqTickers` 未过滤 | 过滤空合约，并提示「N 个合约在该到期日不存在，已跳过」 |
| `plan` 漏掉非基础币种持仓 | 默认只扫 `--currency` 指定币种的持仓 | 扫描时按单一币种过滤 | 改为按 `(代码, 币种)` 收集，扫持仓时用持仓自身币种 |

前四个问题涉及 IBKR 返回值与合约解析语义，也反映了原实现的处理缺口。历史回执与离线回归承担不同验证职责，真实账户观察不能替代失败路径测试。

---

## 五、明确的能力边界

| 项 | 现状 | 原因 |
|---|---|---|
| 美股实时盘口 | 延迟 | 未订阅（`10167`） |
| 港股实时盘口 | 延迟 | 未订阅（`354`） |
| 期权报价 | 历史观察部分为空 | OPRA 针对美股期权；其他市场许可需分别核实 |
| 休市时段成交 | 不会成交 | Gateway 返回 `Warning 399`，委托排队到下一交易日开盘（美东 09:30） |
| 无人值守 | 不支持 | IBKR 每日断网并强制 2FA |
| 历史日线 | 正常 | 不受行情订阅限制，`plan` 取不到实时价时靠它兜底 |
| 行情农场偶发断连 | 历史观察 `2103 usfarm` | 需分别检查行情与账户/订单连接，不据此保证其他链路正常 |

行情许可、纸账户共享设置与并发会话状态须按当时账户配置核实。纸账户看到延迟数据本身不足以证明实盘未订阅；其他 App 的行情也不等于本工具 API 可用行情。

---

## 六、安全闸实测

| 场景 | 期望 | 实测 |
|---|---|---|
| 纸账户下单，不加 `--live-ok` | 放行，出复核码 | ✅ 正常 |
| 实盘 + `live_enabled=false` | 连网关前即拒绝 | ⛔ 提示「实盘下单已被总开关锁定」 |
| 实盘 + 总开关 true，无 `--live-ok` | 拒绝 | ⛔ 提示需显式加 `--live-ok` |
| 复核码跨环境复用 | 无效 | ✅ 同一笔单纸账户码 `46A2AC1B`，实盘码 `CCB94579` |

---

## 七、仍需人工验证的部分

1. **成交与持仓生成**：历史休市记录没有验证；纸账户与实盘分别记录，不能用纸账户回执证明实盘成交。美股期权数据许可涉及 OPRA，其他市场分别核实。
2. **`sell-call` 分支的端到端**：本地纸账户无持仓，只验证了逻辑与准入校验；真实持仓场景需在有股的账户上跑一次 `plan`。
3. **连续交易日的计划稳定性**：`plan` 的 DTE 与选价规则需要连续观察 5 个交易日才能确认与人工判断一致。

---

## 八、休市时段下单链路复核（同日补测）

针对「纸账户无持仓，能不能靠模拟下单补上 `sell-call` 的端到端」，在周六休市时段重跑了一遍。历史观察范围：所列委托进入纸账户并撤销；没有成交或新持仓证据，也不证明新版所有执行入口均通过。

```
基线        account 1,000,000.00 USD / positions 空 / orders 0 笔
预检        order AAPL BUY 100 LMT 200  → 复核码 C8424DBA，不发单
提交        --code C8424DBA  → #99 PreSubmitted，成交 0
回执        Warning 399：委托单在 2026-11-09 09:30（美东）前不会被下达交易所
复核        orders → 1 笔 #99；audit --today → 落盘一条
撤单        cancel 99 → Cancelled，orders 归 0，positions 仍为空

plan        AAPL → 仅出 1 条 cash-put（20261204 325P，年化 16.6%）
run         → 复核码 180EE04B → 提交 #104 PreSubmitted → 撤单归 0
准入        strat --preset sell-call → 「1 张需 100 股，你现有 0 股」正确拦截
```

因此休市时段能验证的是：**预检 → 复核码 → 提交 → 挂单复核 → 撤单 → 审计**这条完整写链路；
未验证的是：**成交 → 生成正股持仓 → 在该持仓上卖 Call**。原记录提及的下一开盘时点不是成交证据；后续测试须另行明确账户、权限、数据与交易范围。

### 本轮同时修复的三个 CLI 可用性缺陷

| 问题 | 现象 | 处理 |
|---|---|---|
| 配置只认当前目录 | 在任意目录敲 `thetamill status` 报「缺少配置文件」 | 查找链加入项目根 `config.json`，并给出 `THETAMILL_CONFIG` 提示 |
| `--paper` 只能前置 | `thetamill account --paper` 报 `unrecognized arguments` | 全局开关用 `argparse.SUPPRESS` 同时挂到子命令，前后置均生效 |
| 默认死连实盘 4001 | 实盘网关未登录时，不带参数的查询命令直接连接失败 | 未显式指定账户时自动探测已登录的网关，输出标明「自动识别」的环境 |

原记录中的 `Warning 399` 表示休市排队，不能作为成交证据；`2103` 表示行情连接异常，应核实所需数据，不据此保证风控或订单链路可用。

---

## 九、端到端验证：外部准入白名单 → 纸账户真实下单

上游研究工具产出人工签字的准入白名单（含 `max_entry`、有效期），本工具用 `plan --universe` 只读消费。该历史场景的验证目标是纸账户接收委托并返回回执；不涵盖成交或新版回归验收。

| 步骤 | 命令 / 动作 | 结果 |
|---|---|---|
| 1 | 白名单校验（`schema` / 签字 / 有效期 / tier / `max_entry`） | 通过，1 只标的 |
| 2 | `plan --universe` 默认 5% OTM → 行权价 325 > `max_entry` 300 | ⛔ 拦截，0 条建议 |
| 3 | `plan --universe --otm 0.15 --json` → 290P | ✅ 放行，写出带白名单元信息的计划 |
| 4 | 手工把计划文件 strike 改成 325 再 `run` | ⛔ 二次校验拒绝：「行权价 325 超过愿意接货价 300」 |
| 5 | `run` 预检 | 出复核码，不发单 |
| 6 | `run --code <复核码>` | ✅ 纸账户提交 `#109`：`SELL 1 AAPL 20261204 290P @0.880`，`PreSubmitted` + `Warning 399` |
| 7 | `orders` | 复核到 1 笔 `#109` |
| 8 | `cancel 109` → `orders` → `positions` → `audit` | `Cancelled`，挂单 0、持仓 0、审计留痕 |

**历史场景结论：所列白名单过滤、计划二次检查、纸账户提交与撤单步骤有记录。** 325 与 290 的单一场景不证明所有篡改均被拦截；此记录不覆盖成交或当前候选版本。

`Warning 399` 提示该委托排队到 `2026-11-09 09:30`（美东）——周末休市导致**未成交**，这是市场时间限制，不是链路缺陷；成交与持仓生成需等开盘。

配套单元测试 `tests/test_universe.py` 共 11 项：过期/无签字/错误 schema/演示数据/无效 `max_entry` 均被拒；未列标的、超过 `max_entry` 的行权价被拦；卖 Call 不受接货价约束。

---

## 十、历史 P0–P2 修复与离线测试记录

在多模型流水线（Gemini 3.8 Flash PM + Hy4 编码引擎）推进下，对代码库实施了深度的缺陷审计与工程加固，累计新增与优化 37 项全离线自动化测试，全部一次性通过。

### 1. P0 级核心安全缺陷修复
- **防范伪备兑裸卖 Call**：在 `cmd_combo` 和 `cmd_plan` 中全面统计现有空头 Call（`_short_calls`），计算净可用正股 `avail = max(0.0, have - busy * mult)`。严防已有空头 Call 占用的股票被重复当做覆盖资产，覆盖该次测试中的重复占用场景。
- **备兑正股差额补买**：修复 `covered-call` 在账户已有部分股票（如 50 股）时仍盲目买入整手（100 股）的问题，修正为只差额购买补齐至覆盖张数。
- **期权行权价回落排序倒置修复**：修正 `_resolve_option` 中当目标行权价区间缺失时的排序机制，优先按离目标最近且方向正确的行权价逐步试探，修正该次回归中的行权价回落方向。
- **跨币种保证金与汇率闭环**：`_funds` 与 `_coverage` 加入多币种现金判定及 IDEALPRO 倒挂外汇对换算；原记录覆盖汇率不可获取时拒绝的部分路径，不证明全部币种义务已统一去重。
- **年化收益率分母真实化**：修正 `cmd_plan` 中年化收益率计算，仅以当前实际卖出张数对应的担保资本作为分母，避免账户超额正股持仓扭曲收益率。
- **白名单字典防御**：`universe.validate` 增加对非字典项的类型拦截，防御格式异常导致的 `AttributeError`。

### 2. P1 级 CLI 可用性与健壮性优化
- **配置解耦与免配置帮助**：`load_cfg(required=False)` 支持免配置文件执行 `thetamill --help` 及所有子命令帮助，仅在执行需要 IB 连接的操作时阻断并提示。
- **复核码载荷标准化**：`_code` 摘要载荷使用标准化 `mode`（`live` / `paper`）替代动态 UI 字符串，解决自动识别环境与显式 `--paper` 导致的复核码不一致问题。
- **期权单腿港美交易所智能回落**：`cmd_order` 对期权单自动根据币种（如 HKD 默认 SEHK）解析交易所，并在 SEHK 失败时自动尝试 SMART 回退，支持 `--exchange` 手工覆盖。
- **碎股死锁解除与策略预设解耦**：`cmd_plan` 新增 `--preset {auto,sell-call,cash-put}` 开关。当持仓股数不足 1 手（如 50 股）或正股已被占满时，`auto` 模式自动智能转为现金担保卖 Put，避免 0 建议死锁。

### 3. P2 级离线单元测试与工程验证
- 新增 `tests/test_cli_logic.py`（26 个测试用例），覆盖复核码确定性、行权价候选排序、DTE 窗口到期日挑选、账户多币种资金解析、抵押品覆盖率、持仓成本与空头统计、白名单二次校验、免配置 CLI 帮助等全部核心离线逻辑。
- 自动化测试用例从 11 项扩充至 **37 项**，离线执行仅耗时 0.011s，无外部网络依赖，测试全部绿色（该次运行全部通过，未测量代码覆盖率）。

---

## 十一、历史风控修复记录（6 项检查与 48 项离线测试）

针对独立外部对抗性代码审查提出的 6 项关键交易风控漏洞，加入了相应检查与离线回归，自动化测试用例由 37 项全面扩展至 **48 项**：

### 1. 单腿期权委托（`cmd_order`）入口安全闭环
- **漏洞**：原 `cmd_order` 仅对股票和普通期权做基础校验，缺少对卖方期权的持仓与资金审查，可通过 `order AAPL SELL 1 --opt` 绕过 `cmd_combo` 的所有风控直接裸卖 Call 或裸卖 Put。
- **加固**：在 `cmd_order` 中对所有 `SELL` 方向期权委托前置注入强校验：
  - 卖 Call 时强制检查正股覆盖：扣减已有空头持仓及未成交挂单占用，净可用不足时阻断并告警；
  - 卖 Put 时强制校验实际现金担保：调用 `_coverage(..., require_cash=True)`，严禁裸卖或利用杠杆。

### 2. 复核码消费记录（不提供原子并发保护）
- **漏洞**：复核码为确定性哈希，生成后在未被消费状态下可被重复提交，存在被恶意重复执行的重放风险。
- **加固**：新增持久化消费记录机制（`_is_code_consumed` / `_consume_code`），由 `~/.thetamill/consumed_codes.json` 与 `orders.jsonl` 双重索引。提交后写入消费记录，后续串行重复提交可被拒绝；查询与消费非原子，且存在发送后未记录的崩溃窗口，不保证并发或故障恢复时仅提交一次。

### 3. TWS 实盘端口（7496）与多层环境安全闸
- **漏洞**：原 `_live_guard` 仅对比 `live_port`（默认 4001），若用户连接 TWS 实盘默认端口 7496，会被漏判为纸账户从而跳过实盘总开关与 `--live-ok` 确认。
- **加固**：
  - 静态端口识别：定义 `LIVE_PORTS = {4001, 7496}` 与 `PAPER_PORTS = {4002, 7497}`，7496 严格纳入实盘保护；
  - 动态账号探测：在网关握手连接时调用 `_verify_account_env`，检测到实盘账号（如 `U*` 开头且非 `DU*`）运行在非实盘模式下时，主动阻断。

### 4. 未完全成交挂单（Open Orders）全链路扣减
- **漏洞**：短时间内多次发单时，前序委托处于 `PreSubmitted` 或 `Submitted`（未成交）状态，其占用的正股或现金尚未计入持仓，可能导致并发卖出同一批正股造成变相裸卖。
- **加固**：
  - 扫描 IBKR 活跃委托列表 `ib.openTrades()`，提取 `remaining` 剩余未成交量；
  - 引入 `_pending_short_calls` 并在计算净可用正股时同时扣减 `(busy + pending) * mult`；
  - 引入 `_pending_cash_puts` 并在现金担保核对时扣减在途 Put 担保金。

### 5. 现金担保卖 Put（Cash-Secured Put）资金属性净化
- **漏洞**：原 `_funds` 混合读取 `AvailableFunds` 作为可用资金，在 Margin（融资借贷）账户中包含借贷购买力，导致现金担保卖 Put 实际使用了融资杠杆。
- **加固**：重构 `_funds` 与 `_coverage(..., require_cash=True)`：
  - 分离 `TotalCashValue`（账户实际硬现金余额）与 `AvailableFunds`（可用借贷资金）；
  - 现金担保卖 Put 严格以 `TotalCashValue` 作为准入基准并扣减在途挂单，杜绝借贷资金充当保证金。

### 6. 计划时效绑定与准入白名单防御加固
- **漏洞**：生成的 `plan.json` 缺少有效期及运行环境标识，可能导致过时的计划或实盘生成的计划在纸账户中误执行；准入白名单对 NaN、Inf 及畸形日期缺乏防御。
- **加固**：
  - `plan` 生成的计划自动注入 `valid_until`（默认 24 小时有效）和 `is_live` 环境标识；
  - `run` 执行时强校验是否过期、执行环境是否与生成环境一致（实盘/纸账户严禁跨环境执行）；
  - `universe.validate` 严格防范 `math.isnan`、`math.isinf` 以及 ISO 8601 日期格式解析。

---

## 十二、历史 IB Gateway 纸账户（4002 端口）提交/撤单记录

**测试环境**：IB Gateway 10.50.app 本机运行，纸账户（端口 4002），账号（已脱敏），净清算值 1,000,000.00 USD。

| 测试项 | 执行命令 / 动作 | 预期反应 | 真实网关实测回执 | 判定 |
|---|---|---|---|---|
| **网关探活** | `thetamill status --paper` | 成功探测 4002 端口并读取资金 | 4002 已连接，净清算值 1,000,000.00 USD | ✅ 通过 |
| **资金查询** | `thetamill account --paper` | 显示现金额度与可用资金 | 现金 1,000,000.00 USD，购买力 6,666,666.67 USD | ✅ 通过 |
| **单腿裸卖 Call 拦截** | `order AAPL --action SELL --qty 1 --opt --strike 250 --right C` | 零正股持仓时拒绝放行 | ⛔ 拦截：`需 100 股，净可用仅 0 股。本工具禁止裸卖 Call。` | ✅ 通过 |
| **单腿现金不足 Put 拦截** | `order AAPL --action SELL --qty 100 --opt --strike 200 --right P` | 200 万担保金 > 100 万现金时拒绝放行 | ⛔ 拦截：`现金担保不足：TotalCashValue 仅 1,000,000.00 USD，严禁裸卖或融资` | ✅ 通过 |
| **正股下单与复核码** | `order AAPL --action BUY --qty 10 --type LMT --limit 100` | 输出预检摘要与一次性复核码 | 输出复核码 `0E2531A4`，未发单 | ✅ 通过 |
| **正股委托提交** | 追加 `--code 0E2531A4` 提交 | 成功进入网关队列 | 提交委托 `#124`，状态 `PreSubmitted`（休市 Warning 399 排队） | ✅ 通过 |
| **复核码防重放攻击** | 再次以 `--code 0E2531A4` 提交 | 拒绝重复提交已消费复核码 | ⛔ 拦截：`复核码 0E2531A4 已经被消费执行过，不可重复提交同一笔委托！` | ✅ 通过 |
| **挂单查询与撤单** | `orders --paper` & `cancel 124` | 查询到挂单并成功撤回 | 查询到 `#124`，撤单回执 `202 Cancelled`，挂单归零 | ✅ 通过 |
| **计划生成与环境元数据** | `plan AAPL --paper --json plan.json` | 导出含 `valid_until` 与 `is_live=false` 的计划 | 成功生成 20261204 325P 建议，文件含 24h 有效期与环境标识 | ✅ 通过 |
| **计划篡改/过期防御** | `run --file expired_plan.json` | 拒绝执行过期计划 | ⛔ 拦截：`计划文件已过期，请重新运行 plan 生成最新计划。` | ✅ 通过 |
| **跨环境篡改执行防御** | 计划标记为 `is_live=true` 在纸账户执行 | 拒绝跨环境执行 | ⛔ 拦截：`计划文件运行环境（实盘）与当前执行环境（纸账户）不一致，严禁跨环境执行！` | ✅ 通过 |
| **在途未成交挂单扣减** | 提交现金担保 Put `#128`（占 32,500 USD），随后再发起需 980,000 USD 的 Put | 自动扣减在途挂单占用（100万 - 32,500 = 967,500 < 980,000）拦截 | ⛔ 拦截：`TotalCashValue 仅 967,500.00 USD（已扣减挂单占用）❌` | ✅ 通过 |
## 十三、交易风控体系全面加固第二阶段（Phase 1 架构级加固与 54 项离线门禁）

在对抗性代码审计与真实券商账户边界检验下，对底层保证金、担保资产核算、跨币种汇率锁、合约非标乘数及挂单同步实施了深度架构级加固，自动化离线测试用例扩充至 **54 项**并全部绿色通过：

### 1. 空头正股头寸与欠担保赤字绝对阻断（Covered Call Deficit Protection）
- **做空正股拦截**：当账户持有负股数（`have < 0`）时，严禁使用 `cmd_order` 卖 Call 或通过 `cmd_combo` 执行 `covered-call` / `sell-call`，覆盖负股数时拒绝新增 Call 的测试路径。
- **欠担保赤字拦截**：当持仓股数已不足以覆盖现有空头 Call（`have < busy_shares`）时，立即切断任何新的 Call 卖出委托，避免产生新的裸卖敞口。

### 2. 存量空头 Put 现金担保与跨币种外汇池双向锁定（Full Cash-Secured Put Reserve）
- **存量已成交空头 Put 锁定**：新增 `_short_puts_collateral`，计算持仓中所有空头 Put 所需接货担保（`strike × multiplier × |position|`），并在 `_coverage(..., require_cash=True)` 中与在途挂单合并扣减。
- **跨币种外汇池统一抵扣**：在基础币种（如 USD）担保非本币（如 HKD）卖 Put 时，不仅扣减基础币种自身的挂单与存量占用，同时将目标币种在途挂单与存量担保折算回基础币种后进行扣减，处理该次回归覆盖的跨币种义务；所有币种共享资金池仍需逐场景检验。

### 3. 非标合约乘数精准计量（Contract Multiplier Fidelity）
- **个性化合约乘数**：引入 `_contract_multiplier(c)`，分别在 `_short_calls_shares`、`_pending_short_calls_shares`、`_pending_cash_puts` 与 `_short_puts_collateral` 中按各期权合约真实乘数（如 mini 期权 10、25、50 或标准 100）精确计算股数与现金担保，消除了全盘死板假设 100 股造成的覆盖率偏差。

### 4. 全局挂单主动同步探测与参数贯穿（Broker State Synchronization）
- **跨客户端挂单主动刷新**：在 `cmd_combo`、`cmd_plan`、`cmd_order` 前置调用 `ib.reqAllOpenOrders()`，请求刷新跨客户端活跃挂单；调用成功不等于跨客户端状态永远完整或实时，历史版本还存在吞掉同步异常的路径。
- **环境安全校验贯穿**：`cmd_order` 与 `cmd_cancel` 正式向 `connect` 注入 `args=args`，确保在任何自定义端口下均能由 `_verify_account_env` 强行阻断实盘账号在模拟模式下的误操作。

### 5. 历史 54 项离线单元测试范围
- 测试覆盖率与边界防御能力全面提升：
  - 复核码确定性与防重放；
  - 账户实际现金提取与融资额度隔离；
  - 港币/美股交易所智能回落；
  - 单腿裸卖 Call / Put 前置拦截；
  - 做空正股与欠担保赤字强阻断；
  - 存量空头 Put 与在途挂单多币种现金池扣减；
  - 非标合约乘数解析；
  - 计划时效绑定与跨环境防篡改；
  - 免配置 CLI 帮助与全局环境安全隔离。
- 原记录为 54 项测试、0.025s、离线运行全部通过；不表示无本地依赖，也未测量代码覆盖率。

---

## 十四、期权合约身份精确绑定与多乘数交易风控加固（Phase 2 实施与 76 项测试门禁）

针对用户核心交易风控要求（「卖put合约有的100 有的200 有的500 这个一定不能错」），重点消除了非标乘数导致的资金穿透与同名期权合约身份歧义风险，将离线自动化测试套件扩充至 **76 项**并全部绿色通过：

### 1. 合约乘数严格校验与阻断机制（Fail-Closed Multiplier Fidelity）
- **严格正有限数校验**：实现 `_parse_multiplier(val)`，仅接受大于 0 的有限整数与浮点数（如 10, 12.5, 100, 500），严格阻断 `bool`、负数、零、`"nan"`、`"inf"` 及不可解析对象。
- **废除静默 100 股回退**：重构 `_multiplier(ib, contract)`，彻底废除「无法确认时默认当作 100 股」的潜在高危逻辑。优先读取合约自身 multiplier，其次通过 `reqContractDetails` 匹配确切 `conId`；若仍无法确认，立即阻断并退出（`sys.exit`），严防港股 500 股大合约被低估导致 80% 现金担保穿透。
- **存量与在途担保全量重核**：`_short_calls_shares`、`_pending_short_calls_shares`、`_short_puts_collateral` 与 `_pending_cash_puts` 均强制调用 `_multiplier` 逐一精确折算每张合约的实际担保义务，按合约乘数计算该次测试中的多标的、混合乘数义务；这不是券商清算或完整交割结构验证。

### 2. 合约身份全维度精确匹配（Exact Contract Identity & Anti-Ambiguity）
- **杜绝按返回索引盲认合约**：实现 `_option_matches(...)`，在 `qualifyContracts` 返回后逐项核对 `secType`, `symbol`, `lastTradeDateOrContractMonth`, `strike`, `right`, `currency`, `conId`, `multiplier`, `tradingClass`。若返回结果存在歧义或不匹配，坚决拒绝认领，防止标准合约与迷你/特殊调整合约混淆。
- **交易所回退参数保真**：在 `_opt` 执行 `SEHK -> SMART` 交易所回退时，全程保留并向券商传递 `multiplier`、`tradingClass` 与 `conId`。
- **行权价阶梯依次逐档解析**：重构 `_resolve_option`，弃用可能因中间合约不存在而引发索引错位的批量 qualify，改为按离目标价格最近且方向正确的行权价逐档解析，确保选取的行权价与合约身份完全咬合。

### 3. 计划生成至执行端到端闭环锁定（Plan -> Run -> Order Identity Lock）
- **计划元数据扩充**：`cmd_plan` 生成的 `plan.json` 导出确切合约身份 `conId`、`mult`、`tradingClass`、`exchange`。
- **执行前强身份校验与防旧版本计划**：`cmd_run` 启动时强校验计划中的 `conId`, `mult`, `tradingClass`, `exchange`，缺少任一有效字段即安全阻断，提示用户重新执行 `plan` 生成最新计划（旧版未保存完整身份的计划文件无法继续执行）。
- **复核码防篡改绑定**：`_code` 摘要载荷注入 `conId`、`multiplier`、`tradingClass`，确保从预检到提交真实执行的合约毫无偏差。
- **CLI 显式参数扩展**：`cmd_order` 与 `cmd_combo` 新增 `--multiplier` 与 `--trading-class` 参数，支持人工提供身份筛选参数；不能仅凭乘数和 tradingClass 宣称调整合约交割物已确认。
- **Auto 预设自适应降级**：`cmd_plan` 在 `--preset auto` 下，根据真实合约乘数核算所需正股；当净可用股数不足 1 张 Call 或市场上该到期日无可用 Call 时，自动平滑转为现金担保 Put。

### 4. 历史 76 项离线单元测试范围（非覆盖率报告）
- 测试套件扩充：
  - `tests/test_contract_identity.py`：新增 24 项测试，覆盖浮点与整数乘数解析、非法与非有限数拒绝、`conId` 精确对应、混合乘数保证金核算、歧义合约拒绝、计划与执行身份锁定等。
  - `tests/test_cli_logic.py`：38 项测试，全面适配显式期权乘数与最新风控逻辑。
  - `tests/test_universe.py`：14 项测试，保持准入白名单减法过滤器测试全绿。
- 历史执行记录：76 项测试离线运行 0.048s，全部通过；没有语句/分支覆盖率报告，也没有由此新增券商 E2E 或成交证据。

## 十五、A 阶段增量安全验收（2026-11-07，离线）

本节与上方历史纸账户记录分开：本批没有连接 Gateway，没有新增提交、撤单或成交证据。

- `tests/test_execution_safety.py`：34 项通过；`tests/test_cli_logic.py`：38 项通过；`tests/test_contract_identity.py`：25 项通过。另有既有白名单 14 项。使用 Python 3.13.12 / ib_async 2.1.0；34 项执行安全测试亦在 Python 3.10 独立环境通过。后续候选 SHA 须另做全新 clone / wheel 门禁。
- 明确检验 100 / 200 / 500：行权价 40、数量 2 的 Put 义务为 8,000 / 16,000 / 40,000 交易币种单位；足额与少 0.01 的现金边界、Call 足额与少 1 股、混合乘数持仓/部分成交挂单均有回归。
- 不一致的总量/已成交/剩余量（包括 ib_async 未初始化的 remaining=0）、非有限持仓、同步失败不按零放行。原测试中的 MagicMock 剩余量已补齐明确总量和成交量，不能用 Mock 默认值冒充券商证据。
- 卖股不得使 Call 失去覆盖，也不得借用另一个 conId 的正股；已知 option underlying conId 与覆盖持仓矛盾时停止。仅这些字段不能证实非标合约完整交割篮子。
- 存量与挂单 Put 义务跨币种折算；特别补测「以源现金币种新增订单」仍扣除其他币种义务，避免只修复 FX 回退路径。该阶段对无法确认的多现金池保持保守拒绝，公共 Decimal 风控层另行验收。
- 备兑差额碎股不自动发出；股腿须有有限且不超批准量的成交证明，随后阻塞刷新账户与挂单、再核验覆盖才考虑期权腿。外部卖股占用和 NaN 成交值均停止第二腿。
- `run` 对原始计划数量/行权价/权利金校验，不先把 bool、字符串或小数强转为整数。旧计划缺合约身份仍须重新生成。
- 复核码原子认领、账户隔离、崩溃恢复与每日限额仍属 B/C；不能用本批测试通过宣称这些功能已上线。`readonly=True` 是库的启动同步选项，并非服务端订单 ACL。

## 十六、P0 修复：未决账户出口与审计断流（2026-11-08，离线）

本节与前文历史记录分开：**没有连接 Gateway，没有新增提交、撤单或成交证据**。

### 1. 未决预留无法解除（账户锁死）

- 缺陷：`ExecutionEngine.reconcile_positive` 与 `SubmissionLedger.resolve_unknown` 在生产路径上零调用者。任何一笔回执不明的委托都会让 `has_unresolved_submissions` 永久为真，`confirm` 之后一律抛错，而文档给出的补救（对账、勿删库）无法执行。
- 新增 `thetamill/resolve.py`：`unresolved_summary` / `resolve_reservation` / `release_never_sent` / `fill_from_row` / `live_observations` / `open_ledger`。`confirmed` 要求每一条未决腿都有正向结构化证据——新鲜且严格同账户/环境的 `Snapshot` 活动挂单，或券商成交流水中同一 `orderRef` 的成交行；身份校验复用 `execution.py` 的同一套函数（`verify_observed_working_order`、`verify_fill_against_approved`、`approved_leg_canonical`），没有第二份实现。
- 新增 `ExecutionEngine.reconcile_filled` 与 `execution.PositiveFill`：Filled 单没有活动挂单，此前根本无法被正向交接；现在由执行行（账户、conId、方向、**精确**成交数量、时间）逐项核对后放行，部分成交仍拒绝。
- 新增 CLI `thetamill resolve --list / --reservation <id> [--state confirmed|released] [--why ...]`：只读打开账本进行列举与取证，只有真正解除时才以可写方式打开。
- `thetamill cancel` 不再静默 `except: pass`：撤单成功但账本更新失败时会明确提示账户可能仍被拦并指向 `resolve --list`。
- 回归：`tests/test_resolve.py` 22 项（含「仅不在挂单中不构成证据」「他账户/他 ref/他 conId 不解除」「两条成交视为歧义拒绝」「快照过期/范围不符/issues 拒绝」「解除不返还已消耗提交次数」）。

### 1b. 回归缺陷：`resolve --list` 的假放行（CI 暴露）

- 现象：`682b6aa` 在 GitHub Actions 的 3.10 与 3.13 两个矩阵均失败于 `test_list_reports_the_blocked_reservation`，实际输出是「账户 DU1234567（live）没有未决预留。」，而账本里有一笔 `paper` 未决预留。本地能通过，是因为本机 4002 端口恰好开着——`resolve_port` 探到纸账户端口就返回 `paper`；干净 runner 上两个端口都关，于是回落到 `live`。
- 定性：这不是 flaky 测试，而是产品缺陷。`resolve --list` 是账户锁的唯一出口，「没有未决预留」在任何情况下都是最高风险的输出；它却由「哪个网关在跑」来推断「该查哪个环境」，在最需要看见预留的离线/断线场景下给出假放行。同一根因还会让 `--state confirmed` 连错环境，把「查错了账户」呈现成「没有成交」。
- 修复：`--list` 的账户范围改为**来自账本**——新增 `state.SubmissionLedger.list_outstanding_scopes()`，`resolve.unresolved_summary` 的 `environment` 改为可选，`None` 表示列出该账户全部环境的未决预留并在每行标注环境；只有显式 `--paper`/`--port` 才收窄。`BlockedReservation` 增加 `account`/`environment` 字段。`--state confirmed` 先从预留的 `scope` 取出账户与环境，与将要连接的端口环境和实际账户逐一比对，不符直接返回 2，不再去错误的环境里查询成交。
- 回归：`tests/test_resolve.py` 新增 5 项——无网关时仍能找到 paper 预留、未指定环境时两个环境都列出、`--paper` 只收窄到 paper、环境不符时拒绝、账户不符时拒绝；测试夹具固定 `resolve_port` 返回值，避免测试再次依赖本机是否开着网关。已确认这 5 项在 `682b6aa` 上为**真红**（其中一项的错误文本与 CI 日志逐字一致）。
- 顺带修复 CI 另外两步在 `682b6aa` 上本会失败的问题（此前这两步从未真正执行，因为测试步先失败就把它们跳过了）：`ruff check --select E9,F` 报 4 项（`tests/test_resolve.py` 两个未使用导入、`thetamill/resolve.py` 未使用的 `field`、`thetamill/execution.py` 两处循环变量遮蔽 `dataclasses.field` 的 F402）；已全部修掉，现为 `All checks passed!`。

### 2. 审计断流

- 缺陷：`cli._write_audit` 自 `04eb77e` 起零调用者，真实提交不再写入 `orders.jsonl`；同时旧单元测试路径用 `default=str` 把 `MagicMock` 的 repr 写进日志，`audit` 又把它们当作券商订单打印，而真正提交过的 #133/#140 只存在于 SQLite 账本中，一条都看不到。
- 新增 `thetamill/audit.py`：`append_record` 严格按类型净化（丢弃非 JSON 安全值、NaN/Infinity、含 mock repr 的字符串，并记入 `dropped_fields`；关键字段全丢则整条不写），`is_real_record` / `is_synthetic_record` 供读取端判定。
- `submission_cli.execute_prepared` 成为**唯一**写入点：仅当某腿处于 attempting/submitting/submitted/unknown（可能已发出）时写入；预检、迁移冻结、发单前拒绝均不写。`cli._write_audit` 已删除，避免出现第二个会漂移的实现。
- `cmd_audit` 隐藏含 mock repr 的合成行并显示条数，同时以只读方式并入 SQLite 账本（`来源=账本`），状态优先取 `confirm_leg` 新记录的券商正向状态。旧复核码导入（`_ledger` 的 legacy import、`_is_code_consumed`）同样跳过合成行：一条 mock 记录不能占用真实复核码。
- `SubmissionLedger.confirm_leg` 现在把「用哪个券商正向状态放行」写入 `reconciliation`（幂等），否则 `confirmed` 会掩盖 `Submitted` 与 `Filled` 的区别。
- 回归：`tests/test_audit.py` 19 项（真实提交写且只写一条、预检/冻结不写、mock 字段被丢弃且文件里不出现 `MagicMock`、合成行不与真实行混淆、账本独有条目可见、mock 行不占用复核码）。
- 对真实数据只做了只读 smoke：`thetamill audit --account DUR339329` 显示隐藏 94 条合成记录、仅 2 条真实条目（#133 `PreSubmitted`、#140），并标注两条都只存在于账本。历史日志文件未修改、未删除。

### 3. 覆盖率门禁（P2）

- 动因：此前「测试全绿」只有一个总数，无法区分「风控/账本被测到多少」和「帮助文本被测到多少」。800+ 项测试对齐的是流程，不是每次改动后的覆盖面。
- 新增 `scripts/coverage_gate.py`：一个全局下限（83%）加 9 个按**资金/风险影响**挑选的模块下限（`execution`/`submission_cli`/`resolve`/`state`/`risk`/`audit`/`broker_state`/`planning`/`cli`）。每个下限都是**当前实测值**的回归保护，不是目标值；下调必须在提交说明里论证。报告文件缺失时返回 2，不会被读成「没有覆盖率问题」。
- `pyproject.toml` 增加 `[tool.coverage.*]`，`dev` 依赖改为 `coverage[toml]==7.16.2`——7.17.x 要求 Python ≥3.11，而本项目支持 3.10，固定 7.17.2 会让 3.10 矩阵装不上。
- CI 新增「Coverage floors for the money-path modules」一步：`coverage run` 跑全量 → `coverage json` → 跑门禁脚本。`ruff --select E9,F` 的扫描范围同时扩到 `scripts`。
- 补测的两个空白：`resolve.py` 从 74% → **100%**（新增 32 项：`live_observations` 的 fills/executions 合并与 `execId` 去重、券商读取异常绝不退化成「没有成交」、非 list 载荷拒绝、`fill_from_row` 的每一种不可读字段、`open_ledger` 的只读/状态错误包装、malformed scope、`release_never_sent` 的全部拒绝分支）；函数级（只算函数体内语句）`freshness_gate` 91.1%、`_independent_requirements` 97.0%，分别比补测前的 49.4% 与 66.2% 大幅提高。
- 门禁本身已证明会失败，并且这个结论被固化成回归：新增 `tests/test_coverage_gate.py` 11 项，通过真实 `main()` 覆盖每条失败路径——模块跌破下限返回 1、恰好等于下限返回 0、全局总数不足返回 1、被守护模块从报告里消失返回 1、报告缺失/JSON 损坏/JSON 形状不对返回 2、条目没有百分比返回 1、缺少参数返回 2，另有一项断言每个下限指向的文件仍然存在（文件被改名或删除时下限不该静默失效）。手工复核同样成立：把报告里 `resolve.py` 改成 50% → rc 1 并打印 `thetamill/resolve.py 50.0% < 90%`；删掉 `state.py` 条目 → rc 1 并标注 `not measured`。
- 实测（Python 3.10.20 / 3.13.13 各自独立 venv + ib_async 2.1.0）：全局 **84.1%**，`resolve` 100%、`broker_state` 91.7%、`audit` 90.4%、`risk` 89.6%、`planning` 89.3%、`state` 89.1%、`execution` 82.0%、`submission_cli` 82.0%、`cli` 72.7%，两个版本都是 `COVERAGE GATE PASSED`。CI（Ubuntu）上同一提交测得全局 84.0%、`cli` 72.5%，见下方门禁一节的 run 记录。
- 覆盖率不等于正确性：它只保证「这些行至少被执行过」，不保证断言有意义，也不覆盖任何一条真实券商交互路径。

### 4. 门禁

- 全量离线：`python -m unittest discover -s tests` → **884 项通过**（Python 3.10 `.venv`、3.10 独立 venv、Python 3.13 独立环境三处均为 884/884）。
- 其余 CI 步骤已在本地逐步复现通过：`ruff check --select E9,F thetamill tests scripts`（`All checks passed!`）、`git diff --check`、冲突标记扫描、构建 wheel 后在无配置的干净 venv 中从 site-packages 运行全量测试，以及 `thetamill --help` / `python -I -m thetamill --help` / 17 个子命令 `--help` 全部 rc 0。
- 推送后 GitHub Actions 实际结果：run `42452430278`（提交 `daa4742`）在 3.10 与 3.13 两个矩阵**全部 9 步通过**，新增的覆盖率步首次在 CI 上真实运行并通过，两个矩阵输出一致：`TOTAL 84.0%`、`resolve 100.0%`、`broker_state 91.7%`、`audit 90.4%`、`risk 89.6%`、`planning 89.3%`、`state 89.1%`、`execution 82.0%`、`submission_cli 82.0%`、`cli 72.5%`，均 `COVERAGE GATE PASSED`（CI 上全局与 `cli` 比本地各低 0.1–0.2 个百分点，来自平台相关的少数语句不可达；两个下限都留有余量）。
- 此前 run `42451022518`（提交 `fd90768`）在 3.10 与 3.13 两个矩阵**全部 8 步通过**。更早 `682b6aa` 的两个矩阵均在「Source regression tests」失败，后续步骤被跳过从未执行——即静态检查与安装包门禁在本仓库**首次真正跑通**是在 `fd90768`。
- 仍未验证：任何真实券商证据。上述解除路径只经过 mock 与临时 SQLite 账本验证；真实账户上仍需单独授权后按只读 → 纸账户提交/撤单分层验收。
