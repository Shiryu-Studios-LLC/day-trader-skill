"""Focused fail-closed tests for the local paper trading launcher.

All broker calls are mocked; tests NEVER submit orders or require credentials.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("trader_orchestrator", ROOT / "orchestrate.py")
orch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(orch)


def write_json(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


class PaperWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        data = Path(self.temp.name) / "trader"
        self.patch_data = mock.patch.object(orch, "DATA", data)
        self.patch_state = mock.patch.object(orch, "STATE", data / "state")
        self.patch_data.start()
        self.patch_state.start()
        self.addCleanup(self.patch_data.stop)
        self.addCleanup(self.patch_state.stop)
        self.data = data
        write_json(data / "config.json", {"paper": True, "budget": 5000})

    def test_portfolio_sync_replaces_unverified_local_holdings(self):
        write_json(self.data / "portfolio.json", {"cash": 5000, "holdings": {"MSFT": 7}})
        orch.sync_paper_portfolio({"cash": "100000", "equity": "100000"}, [], [])
        current = json.loads((self.data / "portfolio.json").read_text())
        self.assertEqual(current, {"cash": 5000.0, "holdings": {}})
        audit = json.loads((self.data / "state/alpaca-paper-snapshot.json").read_text())
        self.assertEqual(audit["open_order_count"], 0)
        self.assertEqual(audit["strategy_budget"], 5000)

    def test_existing_holdings_reduce_strategy_cash(self):
        orch.sync_paper_portfolio({"cash": "100000", "equity": "100000"},
                                  [{"symbol": "MSFT", "qty": "7", "market_value": "3700"}], [])
        current = json.loads((self.data / "portfolio.json").read_text())
        self.assertEqual(current, {"cash": 1300.0, "holdings": {"MSFT": 7}})

    def test_pending_broker_order_blocks_new_entries(self):
        with self.assertRaisesRegex(orch.TradingError, "Open paper orders"):
            orch.sync_paper_portfolio({"cash": "100000", "equity": "100000"}, [],
                                      [{"symbol": "MSFT", "status": "new", "qty": "7"}])

    def test_no_live_mode_credentials(self):
        with mock.patch.object(orch, "read_json", return_value={"mcpServers": {
                "alpaca": {"env": {"ALPACA_PAPER_TRADE": "false", "ALPACA_API_KEY": "x",
                                   "ALPACA_SECRET_KEY": "y"}}}}):
            with self.assertRaisesRegex(orch.TradingError, "PAPER"):
                orch.paper_keys()

    def test_stale_proposals_refused(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
        with self.assertRaisesRegex(orch.TradingError, "stale"):
            orch.require_fresh(old, "Test proposal")

    def test_historical_overrides_cannot_reach_order_review(self):
        self.assertEqual(orch.main(["--ticket", "--now", "2026-10-19 10:15"]), 2)
        self.assertEqual(orch.main(["--risk", "--offline", "some-fixture"]), 2)

    def test_old_plan_cannot_be_reused_for_new_scan(self):
        stamp = datetime.now(timezone.utc).isoformat()
        write_json(self.data / "watchlist.json", {"created_at": stamp, "scanned": 1,
                                                   "hits": [{"symbol": "MSFT"}]})
        write_json(self.data / "plan.json", {"symbol": "MSFT", "entry": 100})
        with self.assertRaisesRegex(orch.TradingError, "not generated from this fresh scan"):
            orch.live_plan_guard()

    def test_live_scan_has_no_hardcoded_historical_timestamp(self):
        stamp = datetime.now(timezone.utc).isoformat()
        write_json(self.data / "watchlist.json", {"created_at": stamp, "scanned": 1,
                                                   "universe": 10, "hits": []})
        with mock.patch.object(orch, "run_skill") as runner:
            orch.scan(type("Args", (), {"now": None, "offline": None})())
        args = runner.call_args.args
        self.assertNotIn("--now", args)
        self.assertIn("--universe", args)

    def test_ticket_is_review_only_and_requires_recent_risk(self):
        now = datetime.now(timezone.utc).isoformat()
        write_json(self.data / "orders.json", {"created_at": now,
                                                "orders": [{"symbol": "MSFT"}]})
        write_json(self.data / "state/risk.json", {
            "date": datetime.now(orch.NY).date().isoformat(),
            "results": [{"verdict": "PASS", "symbol": "MSFT"}]})
        with mock.patch.object(orch, "sync_from_broker") as sync, \
             mock.patch.object(orch, "run_skill") as runner:
            orch.ticket(type("Args", (), {"now": None, "offline": None})())
        sync.assert_called_once()
        self.assertEqual(runner.call_args.args, ("trader-broker/ticket.py", "show"))


if __name__ == "__main__":
    unittest.main()
