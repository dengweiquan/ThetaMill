"""Offline broker snapshot contracts, including real ib_async default order state."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
import unittest
from unittest.mock import MagicMock

from ib_async import IB, LimitOrder, Option, Stock

from thetamill.broker_state import SnapshotError, collect_snapshot, number, select_account


def value(tag, currency, amount, account="DU1"):
    return NS(tag=tag, currency=currency, value=str(amount), account=account)


def stock():
    c = Stock("AAPL", "SMART", "USD")
    c.conId = 10
    return c


def option(mult="200"):
    c = Option("AAPL", "20261218", 40, "P", "SMART", multiplier=mult, currency="USD")
    c.conId = 20
    return c


def trade(contract=None, **overrides):
    order = NS(account="DU1", action="SELL", totalQuantity=3, orderId=1,
               permId=2, clientId=1, orderRef="tm-intent-leg", lmtPrice=1, orderType="LMT")
    status = NS(status="Submitted", remaining=2, filled=1)
    for key, val in overrides.items():
        setattr(status if key in {"status", "remaining", "filled"} else order, key, val)
    return NS(contract=contract or stock(), order=order, orderStatus=status)


def broker():
    ib = MagicMock()
    ib.managedAccounts.return_value = ["DU1"]
    ib.accountValues.return_value = [value("Currency", "BASE", "USD"),
                                    value("TotalCashValue", "BASE", 10000),
                                    value("TotalCashValue", "USD", 10000),
                                    value("NetLiquidation", "BASE", 15000)]
    ib.portfolio.return_value = []
    ib.reqAllOpenOrders.return_value = []
    ib.openTrades.return_value = []
    ib.reqContractDetails.side_effect = lambda c: [NS(contract=c, underConId=10, underSecType="STK")]
    return ib


class BrokerStateTests(unittest.TestCase):
    def collect(self, ib, **kwargs):
        return collect_snapshot(ib, account=None, environment="paper", **kwargs)

    def test_finite_numbers_reject_unknown_and_bool(self):
        for val in (True, False, None, "NaN", "Infinity", object()):
            with self.subTest(val=val), self.assertRaises(SnapshotError):
                number(val, "test")
        self.assertEqual(number("12.5", "test", positive=True), Decimal("12.5"))

    def test_account_selection_is_explicit_for_multiple_accounts(self):
        ib = broker()
        ib.managedAccounts.return_value = ["DU1", "DU2"]
        with self.assertRaises(SnapshotError):
            select_account(ib)
        self.assertEqual(select_account(ib, "DU2"), "DU2")
        with self.assertRaises(SnapshotError):
            select_account(ib, "DU3")

    def test_invalid_account_list_is_not_coerced(self):
        for accounts in ([], [" "], [1], "DU1"):
            ib = broker()
            ib.managedAccounts.return_value = accounts
            with self.subTest(accounts=accounts), self.assertRaises(SnapshotError):
                select_account(ib)

    def test_refresh_and_no_order_side_effects(self):
        ib = broker()
        s = self.collect(ib)
        ib.reqAccountUpdates.assert_called_once_with("DU1")
        ib.reqAllOpenOrders.assert_called_once_with()
        ib.accountValues.assert_called_once_with("DU1")
        ib.portfolio.assert_called_once_with("DU1")
        self.assertEqual(s.cash, {"USD": Decimal(10000)})
        self.assertEqual(s.net_liquidation, Decimal(15000))
        self.assertEqual(s.fx, {"USD": Decimal(1)})
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_snapshot_mapping_cannot_mutate(self):
        s = self.collect(broker())
        with self.assertRaises(TypeError):
            s.cash["USD"] = Decimal(0)

    def test_refresh_failure_blocks_snapshot(self):
        for method in ("reqAccountUpdates", "reqAllOpenOrders", "portfolio", "accountValues", "openTrades"):
            ib = broker()
            getattr(ib, method).side_effect = RuntimeError("offline")
            with self.subTest(method=method), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_non_list_order_response_fails(self):
        ib = broker()
        ib.reqAllOpenOrders.return_value = None
        with self.assertRaises(SnapshotError):
            self.collect(ib)

    def test_base_total_not_added_twice(self):
        ib = broker()
        ib.accountValues.return_value.append(value("TotalCashValue", "HKD", -800))
        s = self.collect(ib, fx={"HKD": Decimal("0.125")})
        self.assertEqual(s.cash, {"USD": Decimal(10000), "HKD": Decimal(-800)})
        self.assertNotIn("BASE", s.cash)

    def test_missing_or_conflicting_currency_cash_fails(self):
        for values in ([value("Currency", "BASE", "USD"), value("TotalCashValue", "BASE", 100)],
                       [value("Currency", "BASE", "USD"), value("TotalCashValue", "USD", 100),
                        value("TotalCashValue", "USD", 99)]):
            ib = broker()
            ib.accountValues.return_value = values
            with self.subTest(values=values), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_wrong_account_data_rejected(self):
        ib = broker()
        ib.accountValues.return_value.append(value("TotalCashValue", "USD", 1, "DU2"))
        with self.assertRaises(SnapshotError):
            self.collect(ib)

    def test_actual_option_multiplier_and_underlying_identity(self):
        for mult in ("100", "200", "500"):
            ib = broker()
            ib.portfolio.return_value = [NS(account="DU1", contract=option(mult), position=-2,
                                           marketPrice=1, averageCost=100)]
            s = self.collect(ib)
            self.assertEqual(s.positions[0].instrument.multiplier, Decimal(mult))
            self.assertEqual(s.positions[0].instrument.key, "10:USD")

    def test_unknown_underlying_or_multiplier_fails(self):
        for change in ("underlying", "multiplier", "detail_multiplier"):
            ib = broker()
            c = option("") if change == "multiplier" else option()
            ib.portfolio.return_value = [NS(account="DU1", contract=c, position=-1, marketPrice=1)]
            if change == "underlying":
                ib.reqContractDetails.return_value = []
                ib.reqContractDetails.side_effect = None
            if change == "detail_multiplier":
                ib.reqContractDetails.side_effect = lambda c: [NS(contract=option("100"), underConId=10)]
            with self.subTest(change=change), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_malformed_position_not_ignored(self):
        for quantity in (float("nan"), float("inf"), True):
            ib = broker()
            ib.portfolio.return_value = [NS(account="DU1", contract=option(), position=quantity)]
            with self.subTest(quantity=quantity), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_missing_price_marks_quality_not_zero(self):
        ib = broker()
        ib.portfolio.return_value = [NS(account="DU1", contract=stock(), position=100, marketPrice=-1)]
        s = self.collect(ib)
        self.assertIsNone(s.positions[0].price)
        self.assertIn("Missing market price: 10", s.issues)

    def test_response_orders_and_cached_pending_orders_are_combined(self):
        ib = broker()
        first, second = trade(), trade(orderId=3, permId=4, status="PendingSubmit")
        ib.reqAllOpenOrders.return_value = [first]
        ib.openTrades.return_value = [first, second]
        s = self.collect(ib)
        self.assertEqual(len(s.orders), 2)
        self.assertEqual(s.orders[0].remaining, Decimal(2))

    def test_unknown_account_order_rejected_other_account_ignored(self):
        for account, rejects in (("", True), ("DU2", False)):
            ib = broker()
            ib.reqAllOpenOrders.return_value = [trade(account=account)]
            if rejects:
                with self.assertRaises(SnapshotError):
                    self.collect(ib)
            else:
                self.assertEqual(self.collect(ib).orders, ())

    def test_inconsistent_remaining_including_real_ib_default_blocks(self):
        ib_real = IB()
        order = LimitOrder("SELL", 100, 40, account="DU1", orderId=11)
        ib_real.wrapper.openOrder(11, stock(), order, NS(status="Submitted"))
        real_trade = ib_real.openTrades()[0]
        self.assertEqual(real_trade.orderStatus.remaining, 0)
        ib = broker()
        ib.reqAllOpenOrders.return_value = [real_trade]
        with self.assertRaises(SnapshotError):
            self.collect(ib)
        for remaining in (0, -1, float("nan"), 4):
            ib = broker()
            ib.reqAllOpenOrders.return_value = [trade(remaining=remaining)]
            with self.subTest(remaining=remaining), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_duplicate_identity_cannot_hide_exposure(self):
        ib = broker()
        ib.reqAllOpenOrders.return_value = [trade(), trade()]
        with self.assertRaises(SnapshotError):
            self.collect(ib)

    def test_model_values_do_not_override_whole_account(self):
        ib = broker()
        ib.accountValues.return_value.append(NS(account="DU1", modelCode="MODEL1",
                                                tag="NetLiquidation", currency="BASE", value="1500000"))
        self.assertEqual(self.collect(ib).net_liquidation, Decimal(15000))

    def test_conflicting_net_liquidation_rejected(self):
        ib = broker()
        ib.accountValues.return_value.append(value("NetLiquidation", "BASE", 1500000))
        with self.assertRaises(SnapshotError):
            self.collect(ib)

    def test_economic_detail_identity_conflicts_rejected(self):
        for field, changed in (("strike", 400), ("right", "C"),
                               ("currency", "HKD"), ("lastTradeDateOrContractMonth", "20270115"),
                               ("symbol", "OTHER"), ("tradingClass", "OTHER"), ("secType", "FOP")):
            ib = broker()
            original, qualified = option(), option()
            setattr(qualified, field, changed)
            ib.portfolio.return_value = [NS(account="DU1", contract=original, position=-1, marketPrice=1)]
            ib.reqContractDetails.side_effect = lambda c: [NS(contract=qualified, underConId=10)]
            with self.subTest(field=field), self.assertRaises(SnapshotError):
                self.collect(ib)

    def test_missing_account_scope_and_malformed_identifiers_rejected(self):
        ib = broker()
        ib.portfolio.return_value = [NS(contract=stock(), position=100, marketPrice=30)]
        with self.assertRaises(SnapshotError):
            self.collect(ib)
        ib = broker()
        ib.accountValues.return_value.append(NS(tag="TotalCashValue", currency="USD", value="100"))
        with self.assertRaises(SnapshotError):
            self.collect(ib)
        for field in ("symbol", "currency"):
            for bad in (None, " ", 1):
                ib = broker()
                c = stock()
                setattr(c, field, bad)
                ib.portfolio.return_value = [NS(account="DU1", contract=c, position=100, marketPrice=30)]
                with self.subTest(field=field, bad=bad), self.assertRaises(SnapshotError):
                    self.collect(ib)

    def test_invalid_fx_and_naive_clock_rejected(self):
        for kwargs in ({"fx": {"USD": 2}}, {"fx": {"HKD": 0}},
                       {"fx": {"HKD": True}}, {"now": datetime(2026, 11, 7)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(SnapshotError):
                self.collect(broker(), **kwargs)
        when = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.assertEqual(self.collect(broker(), now=when).captured_at, when)


if __name__ == "__main__":
    unittest.main()
