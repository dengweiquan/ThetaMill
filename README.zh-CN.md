# ThetaMill

盈透（IBKR）低频期权卖方工具箱，只做三件事：**备兑看涨（covered call）**、**卖出备兑 Call（sell call）**、**现金担保卖 Put（cash-secured put）**。除此之外一概不做。

命令行工具，每交易日跑一两次，**任何委托都必须人工二次确认才会发出**。

> English: [README.md](README.md) ｜ 设计文档与踩坑记录：[docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)

---

## 是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 基于 IBKR TWS API 的 12 个命令行子命令 | 无人值守的自动交易机器人 |
| `plan` 只读生成当日建议 | 回测框架 |
| 三道串联安全闸的执行层 | 策略全家桶——价差、跨式、铁鹰是**刻意排除**的 |
| 尽力写入的本地委托审计日志 | 投资建议 |

Theta 是期权 Greeks 里「每天因时间流逝自动损耗的价值」，卖方赚的就是它。Mill 是磨坊：慢、重复、每天磨一点；但磨盘会磨损——卖方尾部风险真实存在，所以覆盖校验不是装饰。

---

## 前置条件

- Python 3.10+
- 本机运行 **IB Gateway**（或 TWS）并已登录。IBKR 强制每日 2FA，**无法无人值守**
- 网关里开启 API：`配置 → 设置 → API → 设置`
  - 勾选 `Enable ActiveX and Socket Clients`
  - `Socket Port`：实盘 `4001`，纸账户 `4002`
  - `Trusted IPs` 加 `127.0.0.1`
  - **取消** `Read-Only API`，否则一张单也发不出去
- 行情是否可用取决于市场、权限与会话状态；可能延迟或缺失。能查询账户或提交委托，不代表报价有效或能够成交

---

## 安装

```bash
git clone https://github.com/dengweiquan/ThetaMill.git
cd ThetaMill
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

建配置（示例默认锁死实盘）：

```bash
cp config.example.json config.json      # 项目根目录，任意工作目录都能找到
# 或：mkdir -p ~/.config/thetamill && cp config.example.json ~/.config/thetamill/config.json
# 或：export THETAMILL_CONFIG=/path/to/config.json
```

配置文件查找顺序：`$THETAMILL_CONFIG` → `./config.json` → 项目根 `config.json` → `~/.config/thetamill/config.json`
审计日志：`$THETAMILL_DATA` → `~/.local/share/thetamill/orders.jsonl`

---

## 快速上手

> 不写 `--paper` 时会自动识别当前登录的网关，输出首行标明用的是纸账户还是实盘；两个账户都开着时默认实盘，建议显式指定。

```bash
# 1. 网关通了吗
thetamill --paper status

# 2. 只读出建议：DTE 窗口、OTM 选价、成本价下限
thetamill --paper plan 700 --currency HKD
#   #  策略      标的   到期日      DTE  行权价  距现价  参考权利金   年化  担保
#   1  cash-put  700   20261127    20   400.00  -7.1%     6.77     30.9%  40,000.00 HKD

# 3. 写入计划文件，分两步执行
thetamill --paper plan AAPL --json plan.json
thetamill --paper run --file plan.json --i 1                 # 只出复核码，不发单
thetamill --paper run --file plan.json --i 1 --code <复核码>  # 真正发出

# 4. 核对留痕
thetamill --paper audit --today
```

---

## 命令一览

| 命令 | 用途 | 写权限 |
|---|---|---|
| `status` | 探测哪条网关端口通了 | 只读 |
| `account` | 资金、保证金、缓冲 | 只读 |
| `positions` | 持仓与浮盈 | 只读 |
| `orders` | 未成交挂单 | 只读 |
| `quote` | 正股报价 | 只读 |
| `chain` | 期权链、报价、Greeks、DTE | 只读 |
| `plan` | 当日建议（DTE 窗口 + OTM 选价 + 成本价下限） | 只读 |
| `audit` | 审计日志 | 只读 |
| `report` | 盘中风险快照（终端 / JSON / Markdown） | 只读 |
| `reconcile` | 只读券商对账；不重发、不撤单、不自动解除未知预留 | 只读 |
| `roll-plan` | 展期建议；独立 schema，`run` 拒绝执行 | 只读 |
| `order` | 股票/期权单腿下单 | 需复核码 |
| `strat`（别名 `combo`） | 三种策略；备兑正股与期权腿顺序执行 | 需复核码 |
| `run` | 执行 `plan` 生成的建议 | 需复核码 |
| `cancel` | 撤单 | — |

全局参数：`--paper`、`--port N`、`--delayed`，写在子命令前后都生效（`thetamill --paper account` 与 `thetamill account --paper` 等价）。
下单参数：`order` 与 `strat`（`combo` 是同一实现的兼容别名）支持 `--multiplier`（如 100/200/500）与 `--trading-class` 以区分合约。乘数不是完整交割条款：含现金、篮子或无法确认交割结构的调整合约不属于自动支持范围。

**可选外部准入白名单**：`plan --universe <file.json>` 读取上游研究工具产出的人工签字白名单，只做减法——不在白名单的标的不出建议，现金担保卖 Put 的行权价不得超过 `max_entry`（愿意接货价）。默认关闭，不传时行为不变；本工具不评估标的质地、不因白名单自动发单。契约格式见 `thetamill/universe.py`，历史验证及证据边界见 `docs/VERIFICATION.zh-CN.md`。

---

## 安全模型

三道闸串联，缺一个都发不出去：

1. **`live_enabled`**（`config.json`，默认 `false`）——关闭时任何实盘委托在连网关之前就被拒绝
2. **`--live-ok`**——总开关打开后，漏了这个参数照样拒绝
3. **复核码**——同一条命令跑一次只出码，带 `--code` 复跑才发；码里含账户环境，纸账户的码在实盘无效

再加覆盖校验与合约身份绑定，让三种策略名副其实：

- `sell-call` 必须持有足够股数（严格按期权合约真实乘数计算）；裸卖 Call 不属于支持行为，检查依赖可确认的券商状态
- `cash-put` 必须备足真实硬现金（按 `行权价 × 合约乘数 × 张数` 计量并核减在途挂单与存量空头，不静默默认 100 股）
- `covered-call` 已有足够股数时拒绝再买，缺少股数时仅差额补买
- **合约身份闭环锁定**：`plan` 导出确切合约身份（`conId`、`mult`、`tradingClass`、`exchange`）；`run` 校验这些身份字段并拒绝缺少身份元数据的旧计划；这不等于验证完整交割结构。


需要券商连接的只读命令使用 `ib_async` 的 `readonly=True` 参数；这不是服务端订单权限或 ACL，不能据此宣称服务端拒绝该会话发单。这些命令自身不主动提交委托；Gateway/TWS 的 Read-Only API 配置是另一个控制。

B 阶段已落地权威本地 SQLite 账本（`thetamill/state.py`），每次 `BEGIN IMMEDIATE` 事务原子完成 intent 认领、风险预留与状态迁移；`thetamill/execution.py` 在发单前刷新券商状态并复核有效期/配额/合约身份，网络发送前持久化 `submitting` 与稳定 `orderRef`，回执不明进入 `unknown` 交由 `reconcile` 只读对账而非重发。账户与环境范围是账本键的一部分。

仍需注意的边界：该账本只协调同一主机、同一账本的本工具进程，不控制人工 TWS、其他机器或第三方客户端，也不提供券商端 exactly-once 语义。`readonly=True` 是 `ib_async` 客户端参数，不是服务端订单 ACL。旧 JSON 复核码消费记录仅作兼容保留，不再承担并发与配额职责。旧计划应重新运行 `plan` 生成，不能手改补齐缺失或不兼容的合约身份。

离线 Mock、离线真实 `ib_async` 合约对象、Gateway 只读、纸账户提交撤单、成交是不同证据等级。历史 76 项测试通过不代表 100% 覆盖率或当前版本验收通过；B–F 离线套件不代表新增券商验证，2026-11-08 纸账户实跑仅覆盖提交与撤单，不含成交。详见[验证记录](docs/VERIFICATION.zh-CN.md)。

---

## 已知限制

- **无法无人值守**：IBKR 每天断开网关并要求 2FA，需人工登录
- **行情可能延迟或缺失**：历史会话美股报 `10167`、港股报 `354`；期权可能没有 Greeks 或报价，报价/FX 缺失会影响计划与风控检查
- **提交不等于成交**：历史纸账户记录只证明休市期间部分委托提交与撤单，未证明成交或生成持仓。成交需在交易时段、合适权限与数据条件下另行验证
- **月度与周度期权阶梯不同**：`reqSecDefOptParams` 返回的是所有到期日的行权价并集，单月只含子集，所以按距离就近试探真实存在的行权价
- **IB 用 `-1` 表示无行情**：取价时显式排除 ≤ 0 的值（这个坑会让行权价算到离谱的位置）

---

## 风险声明

本工具只是**执行器**，不构成任何投资建议。卖方策略的亏损可能超过收到的权利金；裸卖 Call 风险无限（本工具不支持）。所有委托默认需要人工确认——请保持这个设计。使用风险自负。

## 许可

MIT，见 [LICENSE](LICENSE)。
