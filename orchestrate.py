#!/usr/bin/env python3
"""Paper-trading proposal workflow (never a broker order executor).

All paths are relative to this repository, whose trader/ and reports/
directories are local, private, and untracked. The official Alpaca MCP
place_stock_order tool must be invoked by Claude Code only after a fresh
risk PASS, a new user yes, and the native Allow once approval card.

Examples:
  python3 -B orchestrate.py --broker-status        # paper account, read only
  python3 -B orchestrate.py --sync-broker          # reconcile local portfolio
  python3 -B orchestrate.py --scan                 # live NY market session
  python3 -B orchestrate.py --budget               # new plan -> proposed order
  python3 -B orchestrate.py --risk                 # check a fresh proposed order
  python3 -B orchestrate.py --ticket               # review only, never submits
  python3 -B orchestrate.py --full                 # orchestrate, fail closed
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "trader"
STATE = DATA / "state"
REPORTS = ROOT / "reports"
SKILL = ROOT
NY = ZoneInfo("America/New_York")
PAPER_API = "https://paper-api.alpaca.markets/v2"
OPEN_STATUSES = {"new", "accepted", "pending_new", "partially_filled",
                 "accepted_for_bidding", "held", "pending_replace", "pending_cancel"}
MAX_PROPOSAL_AGE = timedelta(minutes=15)


class TradingError(Exception):
    """Expected failure; no orders have been placed."""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TradingError(f"Required file is missing or invalid: {path}") from exc
    if not isinstance(value, dict):
        raise TradingError(f"Expected JSON object: {path}")
    return value


def atomic_json(path: Path, obj: dict) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def paper_keys() -> tuple[str, str]:
    """Read, never print, the credentials already stored by 'claude mcp add'.

    No keys are stored inside the repo, reports, subprocess argv, or logs.
    """
    cfg = read_json(Path.home() / ".claude.json")
    server = cfg.get("mcpServers", {}).get("alpaca", {})
    env = server.get("env", {}) if isinstance(server, dict) else {}
    if str(env.get("ALPACA_PAPER_TRADE", "")).lower() != "true":
        raise TradingError("Official Alpaca MCP is not explicitly configured for PAPER trading.")
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise TradingError("Paper credentials missing from Claude's local Alpaca MCP configuration.")
    return key, secret


def verify_alpaca_mcp() -> None:
    """Read the real server's tool registry; never call any broker tool."""
    cfg = read_json(Path.home() / ".claude.json")
    server = cfg.get("mcpServers", {}).get("alpaca", {})
    if not isinstance(server, dict) or not isinstance(server.get("args"), list):
        raise TradingError("The official 'alpaca' MCP server is not configured.")
    if not server.get("command") or not Path(server["command"]).is_file():
        raise TradingError("Alpaca MCP launcher does not exist; check the uvx absolute path.")
    # Only allow the paper credentials; never inspect, print, or persist them.
    paper_keys()
    env = os.environ.copy()
    env.update(server.get("env", {}))
    try:
        process = subprocess.Popen([server["command"], *server["args"]],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, bufsize=0)
    except OSError:
        raise TradingError("Could not start the official Alpaca MCP launcher.") from None

    try:
        calls = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "trader-mcp-diagnostic", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        for message in calls:
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        buffer = bytearray()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if not selector.select(timeout=1):
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                try:
                    response = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    continue
                if response.get("id") != 2:
                    continue
                names = {tool.get("name") for tool in response.get("result", {}).get("tools", [])}
                required = {"place_stock_order", "get_orders", "get_all_positions", "get_account_info"}
                if not required.issubset(names):
                    raise TradingError("Alpaca MCP responded but required account/order tools are missing.")
                print(f"Official Alpaca MCP is healthy: {len(names)} tools available, "
                      "including place_stock_order and read-only account tools.")
                print("Tool registry inspection only: NO order was placed or approved.")
                return
        raise TradingError("Alpaca MCP tools/list did not respond; restart Claude Code and check /mcp.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def paper_get(path: str):
    key, secret = paper_keys()
    request = Request(
        PAPER_API + path,
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                 "Accept": "application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=15) as reply:
            return json.load(reply)
    except HTTPError as exc:
        # Never interpolate the remote body or authentication headers.
        raise TradingError(f"Alpaca paper API returned HTTP {exc.code} on read-only request.") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise TradingError("Alpaca paper API could not be reached or returned invalid data.") from None


def paper_snapshot() -> tuple[dict, list, list]:
    """Only GET endpoints; no state changing broker action exists in this CLI."""
    account = paper_get("/account")
    positions = paper_get("/positions")
    orders = paper_get("/orders?status=all&limit=500&nested=true")
    if not isinstance(account, dict) or not isinstance(positions, list) or not isinstance(orders, list):
        raise TradingError("Alpaca returned an unexpected account/positions/orders format.")
    if account.get("status") != "ACTIVE" or account.get("trading_blocked") is True:
        raise TradingError("Paper account is not active for trading.")
    return account, positions, orders


def print_broker(account: dict, positions: list, orders: list) -> None:
    print("Alpaca Trading API: PAPER account (authenticated read-only)")
    print(f"  Cash: ${account.get('cash')}   Buying power: ${account.get('buying_power')}")
    print(f"  Equity: ${account.get('equity')}   Positions: {len(positions)}   Recent orders: {len(orders)}")
    for p in positions:
        print(f"  HOLD {p.get('symbol')} qty={p.get('qty')}")
    for o in orders[:10]:
        print(f"  ORDER {o.get('symbol')} {o.get('side')} qty={o.get('qty')} "
              f"status={o.get('status')} broker_id={o.get('id')}")
    print("No broker order was submitted by this command.")


def sync_paper_portfolio(account: dict, positions: list, orders: list) -> None:
    """Conservatively map a paper account to the configured strategy allocation."""
    cfg = read_json(DATA / "config.json")
    if cfg.get("paper") is not True:
        raise TradingError("Config paper=true is required; refusing to sync.")
    budget = float(cfg.get("budget", 0))
    if not 0 < budget < float("inf"):
        raise TradingError("Set a positive explicit 'budget' in trader/config.json.")
    if len(orders) == 500:
        raise TradingError("Alpaca returned 500 orders; paginate before relying on incomplete history.")
    holdings = {}
    exposure = 0.0
    for p in positions:
        sym = p.get("symbol")
        qty = float(p.get("qty", 0))
        if not isinstance(sym, str) or not sym or qty < 0 or qty == float("inf"):
            raise TradingError("Invalid/short paper holding; manual review required.")
        if p.get("market_value") is None:
            raise TradingError("Paper position lacks market_value; cannot safely size new entries.")
        exposure += abs(float(p["market_value"]))
        holdings[sym] = int(qty) if qty.is_integer() else qty
    cash = max(0.0, min(float(account.get("cash", 0)), budget - exposure))
    pending = [o for o in orders if o.get("status") in OPEN_STATUSES]
    snapshot = {
        "source": "Alpaca Trading API PAPER (read-only GET)",
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "account_cash": account.get("cash"),
        "account_equity": account.get("equity"),
        "strategy_budget": budget,
        "strategy_exposure": round(exposure, 2),
        "open_order_count": len(pending),
        "recent_order_count": len(orders),
        "positions": [{"symbol": p.get("symbol"), "qty": p.get("qty")} for p in positions],
        "orders": [
            {"id": o.get("id"), "symbol": o.get("symbol"), "side": o.get("side"),
             "qty": o.get("qty"), "status": o.get("status"), "filled_qty": o.get("filled_qty")}
            for o in orders
        ],
    }
    atomic_json(STATE / "alpaca-paper-snapshot.json", snapshot)
    atomic_json(DATA / "portfolio.json", {"cash": round(cash, 2), "holdings": holdings})
    print(f"Reconciled real paper holdings. Strategy allocation: ${budget:.2f}; "
          f"available strategy cash: ${cash:.2f}; open broker orders: {len(pending)}.")
    if pending:
        raise TradingError("Open paper orders exist. Review them before proposing additional entries.")


def sync_from_broker() -> None:
    account, positions, orders = paper_snapshot()
    sync_paper_portfolio(account, positions, orders)


def require_fresh(timestamp: str | None, label: str, now: datetime | None = None) -> None:
    if not timestamp:
        raise TradingError(f"{label} has no creation timestamp. Recreate it.")
    try:
        when = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError:
        raise TradingError(f"Invalid timestamp for {label}.") from None
    now = now or datetime.now(timezone.utc)
    age = now - when.astimezone(timezone.utc)
    if age < timedelta(seconds=-30) or age > MAX_PROPOSAL_AGE:
        raise TradingError(f"{label} is stale (older than 15 minutes or in future). Generate fresh data.")


def live_plan_guard() -> None:
    plan = read_json(DATA / "plan.json")
    scan = read_json(DATA / "watchlist.json")
    require_fresh(scan.get("created_at"), "Scanner results")
    if plan.get("signal_scan_id") != scan.get("created_at"):
        raise TradingError(
            "Plan was not generated from this fresh scan. Pick a current hit using "
            "trader-entry/trader-exit and set plan.signal_scan_id = watchlist.created_at."
        )
    if not any(h.get("symbol") == plan.get("symbol") for h in scan.get("hits", [])):
        raise TradingError("Planned symbol is not in the current scanner hits.")
    if scan.get("scanned", 0) <= 0:
        raise TradingError("No symbols evaluated; the scanner result is incomplete.")


def run_skill(*parts: str, timeout: int = 60) -> None:
    cmd = [sys.executable, "-B", str(ROOT / parts[0]), *parts[1:]]
    try:
        p = subprocess.run(cmd, cwd=ROOT, timeout=timeout, capture_output=True, text=True)
    except subprocess.TimeoutExpired:
        raise TradingError(f"{parts[0]} timed out; check the market data/connection.") from None
    if p.stdout:
        print(p.stdout.rstrip())
    if p.returncode != 0:
        print((p.stderr or "")[:1200], file=sys.stderr)
        raise TradingError(f"{parts[0]} exited with code {p.returncode}.")


def scan(args) -> None:
    opts = ["orb", "--universe", str(DATA / "universe.txt"),
            "--config", str(DATA / "config.json"), "--out", str(DATA / "watchlist.json")]
    if args.now:
        opts += ["--now", args.now]
        print("HISTORICAL OVERRIDE: simulation only; never submit this setup.")
    if args.offline:
        opts += ["--offline", args.offline]
        print("OFFLINE FIXTURE: simulation only; never submit this setup.")
    run_skill("trader-scanner/scan.py", *opts, timeout=600)
    result = read_json(DATA / "watchlist.json")
    print(f"Scanner: evaluated {result.get('scanned', 0)} of {result.get('universe', 0)} "
          f"symbols; {len(result.get('hits', []))} hits; {result.get('unconfirmed', 0)} unconfirmed.")
    if int(result.get("scanned", 0)) <= 0:
        raise TradingError("Incomplete scan: no evaluated symbols. Do not treat 0 hits as a market result.")


def budget(args) -> None:
    live_plan_guard()
    sync_from_broker()
    run_skill("trader-budget/size.py", "--plan", str(DATA / "plan.json"),
              "--config", str(DATA / "config.json"),
              "--portfolio", str(DATA / "portfolio.json"),
              "--write", str(DATA / "orders.json"))


def risk(args) -> None:
    order = read_json(DATA / "orders.json")
    require_fresh(order.get("created_at"), "Order proposal")
    sync_from_broker()
    run_skill("trader-risk/check.py", "--orders", str(DATA / "orders.json"),
              "--config", str(DATA / "config.json"),
              "--portfolio", str(DATA / "portfolio.json"))


def ticket(args) -> None:
    order = read_json(DATA / "orders.json")
    require_fresh(order.get("created_at"), "Order proposal")
    riskdoc = read_json(STATE / "risk.json")
    if riskdoc.get("date") != datetime.now(NY).date().isoformat():
        raise TradingError("No current-day risk verdict. Run --risk on the exact order.")
    if not riskdoc.get("results") or any(r.get("verdict") != "PASS" for r in riskdoc["results"]):
        raise TradingError("Risk verdict is not PASS. No ticket may be proposed.")
    sync_from_broker()
    run_skill("trader-broker/ticket.py", "show")
    print("\nREVIEW ONLY: no Alpaca order submitted. In Claude Code, explicitly ask")
    print("for a *new yes* for this exact fresh ticket; then call the real Alpaca")
    print("MCP place_stock_order tool, retain Allow once, and verify broker ID/status.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Paper trade scanner, risk and broker reconciliation (no order submission).")
    m = ap.add_mutually_exclusive_group(required=True)
    for opt, help_text in (
        ("verify-mcp", "Verify official Alpaca MCP tool availability without calling any tools"),
        ("scan", "Run current market ORB scan"),
        ("budget", "Size a fresh manually selected plan"),
        ("risk", "Risk-check fresh proposed orders"),
        ("ticket", "Display a risk-approved ticket; never executes"),
        ("full", "Scan then advance only with a matching fresh selected plan"),
        ("broker-status", "Read-only authenticated Alpaca paper account status"),
        ("sync-broker", "Update local strategy portfolio from actual paper account"),
    ):
        m.add_argument("--" + opt, action="store_true", help=help_text)
    ap.add_argument("--now", help="Historical time override for simulation, never live order submission")
    ap.add_argument("--offline", metavar="FIXTURE", help="Path to offline scanner fixture for simulation")
    args = ap.parse_args(argv)
    try:
        if (args.now or args.offline) and not args.scan:
            raise TradingError("--now/--offline are scanner-only simulations; never use them for live sizing, risk, tickets, or full pipeline.")
        if args.verify_mcp:
            verify_alpaca_mcp()
        elif args.broker_status:
            print_broker(*paper_snapshot())
        elif args.sync_broker:
            sync_from_broker()
        elif args.scan:
            scan(args)
        elif args.budget:
            budget(args)
        elif args.risk:
            risk(args)
        elif args.ticket:
            ticket(args)
        else:
            # Never silently carry an old trading plan forward as if it came from this scan.
            sync_from_broker()
            scan(args)
            live_plan_guard()
            budget(args)
            risk(args)
            ticket(args)
        return 0
    except TradingError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
