# ThetaMill

A small, opinionated option-selling toolkit for Interactive Brokers.

ThetaMill does exactly three things — **covered call**, **sell call** (against shares you already hold), and **cash-secured put** — and refuses to do anything else. It is a command-line tool, runs once or twice a trading day, and never sends an order without an explicit human confirmation step.

> 中文说明见 [README.zh-CN.md](README.zh-CN.md) ｜ Design notes: [docs/DESIGN.zh-CN.md](docs/DESIGN.zh-CN.md)

---

## What it is / is not

| It is | It is not |
|---|---|
| A CLI with 12 subcommands over the IBKR TWS API | A trading bot that runs unattended |
| A plan generator (`plan`) that only reads | A backtesting framework |
| An execution layer with three stacked safety gates | A strategy zoo — spreads, straddles, iron condors are deliberately excluded |
| An audit trail for every order actually sent | Investment advice |

Theta is the option Greek for value lost to the passage of time. *Mill* because this is slow, repetitive work: grind a little premium out each week, and the millstone wears — tail risk on short options is real, which is why coverage checks are not decoration.

---

## Requirements

- Python 3.10+
- **IB Gateway** or TWS running locally, logged in *(no unattended login — IBKR forces a daily 2FA)*
- IBKR API enabled: `Configure → Settings → API → Settings`
  - `Enable ActiveX and Socket Clients` ✔
  - `Socket Port`: `4001` for live, `4002` for paper
  - `Trusted IPs`: add `127.0.0.1`
  - `Read-Only API` ✘ — leave unchecked, or orders can never be sent
- Market data subscriptions are **optional**. Without them you get delayed quotes, and the account/position/order paths still work correctly.

---

## Install

```bash
git clone https://github.com/dengweiquan/ThetaMill.git
cd ThetaMill
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

Create a config file (the example is safe by default — live trading is disabled):

```bash
cp config.example.json config.json      # project root — works from any working directory
# or: mkdir -p ~/.config/thetamill && cp config.example.json ~/.config/thetamill/config.json
# or: export THETAMILL_CONFIG=/path/to/config.json
```

Config lookup order: `$THETAMILL_CONFIG` → `./config.json` → project-root `config.json` → `~/.config/thetamill/config.json`.
Audit log location: `$THETAMILL_DATA` → `~/.local/share/thetamill/orders.jsonl`.

---

## Quick start

Every command below uses `--paper` (port 4002). Without it the CLI auto-detects which gateway is logged in and prints the chosen account on the first line. Global flags work before or after the subcommand.

```bash
# 1. Is the gateway reachable?
thetamill --paper status

# 2. Read-only plan: DTE window, OTM strike selection, cost-basis floor
thetamill --paper plan 700 --currency HKD
#   #  策略      标的   到期日      DTE  行权价  距现价  参考权利金   年化  担保
#   1  cash-put  700   20261127    20   400.00  -7.1%     6.77     30.9%  40,000.00 HKD

# 3. Write the plan to a file, then execute it in two steps
thetamill --paper plan AAPL --json plan.json
thetamill --paper run --file plan.json --i 1                 # prints an approval code, sends nothing
thetamill --paper run --file plan.json --i 1 --code <CODE>   # actually sends

# 4. Verify the audit trail
thetamill --paper audit --today
```

---

## Commands

| Command | Purpose | Writes? |
|---|---|---|
| `status` | Which gateway port is up | read-only |
| `account` | Cash, margin, cushion | read-only |
| `positions` | Holdings and unrealized P/L | read-only |
| `orders` | Working orders | read-only |
| `quote` | Stock quote | read-only |
| `chain` | Option chain, quotes, Greeks, DTE | read-only |
| `plan` | Today's recommendations (DTE window + OTM strike + cost-basis floor) | read-only |
| `audit` | Order audit log | read-only |
| `order` | Single-leg stock or option order | approval code |
| `strat` | One of the three strategies as a package | approval code |
| `run` | Execute a plan item | approval code |
| `cancel` | Cancel a working order | — |

Global flags: `--paper`, `--port N`, `--delayed` — accepted before or after the subcommand (e.g. `thetamill account --paper`).
Order flags: `order` and `combo` accept `--multiplier` (e.g. 10/100/500) and `--trading-class` (standard, mini, or adjusted contracts).

Optional admission allowlist: `plan --universe <file.json>` reads a human-signed allowlist produced by an upstream research tool. It only subtracts — unlisted symbols produce no suggestion, and a cash-put strike may not exceed `max_entry` (the price you are willing to be assigned at). Disabled by default; the tool neither assesses company quality nor sends orders because of the allowlist. Contract: `thetamill/universe.py`; end-to-end verification: `docs/VERIFICATION.zh-CN.md`.

---

## Safety model

Three gates in series. An order cannot leave without passing all three:

1. **`live_enabled`** in `config.json` (default `false`) — blocks any live order before the gateway is even contacted.
2. **`--live-ok`** — even with the switch on, a live order needs this flag.
3. **Approval code** — running the same command once prints a short code and exits; the order is only sent when the command is re-run with `--code`. The code hashes the account environment, so a paper-account code is worthless on a live account.

Plus coverage checks and contract identity binding that make the three strategies mean what they say:

- `sell-call` requires enough shares based on each contract's actual multiplier; there is no naked-call path in this codebase.
- `cash-put` requires enough actual cash (`strike × multiplier × qty`), deducting working orders and existing short puts to prevent under-collateralization.
- `covered-call` refuses to buy more shares if you already hold enough, buying only the exact difference needed.
- **Contract identity lock**: `plan` records exact contract metadata (`conId`, `mult`, `tradingClass`, `exchange`), which `run` strictly validates before requesting approval. Legacy or tampered plans lacking identity are rejected.


Read-only commands use `readonly=True` connections — the IBKR server itself rejects any order on those sessions.

---

## Known limitations

- **No unattended trading.** IBKR drops the gateway daily and requires 2FA. Log in manually each session.
- **Market data is usually delayed.** Without a subscription, US stocks report `10167` and HK stocks `354`; the account, position and order paths are unaffected. Options may have no Greeks or no quote at all.
- **Paper accounts rarely fill options** — matching needs a book, and unsubscribed options have no book. Paper is for verifying that orders submit and cancel, not for verifying fills.
- **Monthly and weekly option ladders differ.** `reqSecDefOptParams` returns the union of strikes across all expirations; a given month may only carry a subset, so strikes are probed nearest-first (`_resolve_option`).
- **IB uses `-1` for "no data"** on market data fields. Values ≤ 0 are rejected when reading spot prices.

---

## Disclaimer

This software is an execution tool, not investment advice. Short option positions can lose more than the premium received, and naked short calls are unbounded (this tool does not support them). Every order requires a human confirmation step by design — keep it that way. Use at your own risk.

## License

MIT — see [LICENSE](LICENSE).
