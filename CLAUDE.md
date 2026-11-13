# Day-Trader-Skill - Claude Code operating instructions

Work from `/mnt/Development/Tools/day-trader-skill` (this repository). This is
the single project root. The 11 `trader-*` skills live here; `orchestrate.py`
is at the root, live local data in `trader/`, and private reports/backups in
`reports/`. Never move Alpaca credentials into this repository.

## Start every session

1. Confirm the **official `alpaca` stdio MCP** is connected. Its launcher is
   `~/.local/bin/uvx`. Run `python3 -B orchestrate.py --verify-mcp` to
   confirm **place_stock_order is present** without placing anything. A
   separate paper HTTP plugin may also be connected. Do not interpret a
   server status as proof an order was executed.
2. Run `python3 -B orchestrate.py --broker-status` for read-only paper account,
   positions, and order status. It uses the existing local Claude MCP paper
   credentials securely, and makes no orders.
3. Run `python3 -B orchestrate.py --sync-broker` to refresh the local *strategy*
   portfolio from verified paper holdings. It preserves a **$5,000 strategy
   allocation** even if Alpaca grants $100,000 paper cash. Never treat old
   `trader/portfolio.json` holdings as broker evidence.
4. Use `python3 -B orchestrate.py --help` for scanner/budget/risk/ticket modes.
   The orchestrator is a **proposal and review CLI**. It cannot call MCP tools
   by printing JSON, and it **never places orders on its own**.

## Trading workflow - paper only

- Scan only with real current New York market time. A historical `--now` is a
  simulation and must not be traded. Inspect `scanned` versus `universe`;
  do not equate 0 hits with a completed scan when 0 symbols were evaluated.
- Select a candidate using current quotes and the `trader-entry` and
  `trader-exit` skills to write a **new** `trader/plan.json`, not a stale plan.
- Size it (`--budget`), recheck (`--risk`), show it (`--ticket`). If the actual
  paper account has open orders/positions, reconcile before proposing more.
- Before submitting ANY order: verify a fresh risk PASS for **that exact
  fingerprint on that date**, and ask the user to reply an explicit **yes for
  that one new order**. A previous "yes" is not reusable, even when local
  `state/broker.json` says `approved`.
- Only then invoke the actual **`mcp__alpaca__place_stock_order`** tool on the
  connected Alpaca MCP, with exactly the vetted broker fields from the ticket.
  Keep its native **Allow once / Deny** card (manual approval) enabled.
  Do not use Bash/HTTP/curl/Python to bypass that permission step. Never
  auto-allow or bypass broker tools. Never submit from a scheduled task.
- After submission, read the broker's actual returned **order ID and status**;
  cross-check `get_orders` / positions. Only report an order as placed after
  Alpaca acknowledges it, and record actual fills separately from approvals.
  An `approved` local record with no `broker_id` is **not** a fill.
- Do not reuse the old November 13 MSFT proposal or trade from the historical
  scan. It has an approval record but no confirmed Alpaca broker ID.
- Preserve risk limits, paper mode, and per-order approvals. No live trading,
  leverage, margin, or shorts without separate explicit authorization.

## Files / Git / safety

- `trader/` and `reports/` are private ignored local directories. Old
  `~/trader`, `/mnt/Development/Tools/trader`, and
  `~/Documents/lets-trade` paths remain compatibility links.
- Never print credentials or write them into logs, prompts, commits, or tests.
- Keep source code and tests in this Git repository; run tests before commit
  and push. Do not commit account state or private reports.
- Do not claim a READY ticket means an actual paper order exists. Every
  broker-changing operation stays behind user confirmation and tool approval.
