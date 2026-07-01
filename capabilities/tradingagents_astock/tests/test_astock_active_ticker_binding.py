from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import _bind_astock_ticker


def test_astock_stock_tools_bind_first_ticker_arg_to_active_ticker():
    config = {"active_ticker": "000988.SZ"}
    args = ("Ƽ", "2026-05-01", "2026-05-17")

    routed = _bind_astock_ticker("get_stock_data", "a_stock", args, config)

    assert routed == ("000988.SZ", "2026-05-01", "2026-05-17")


def test_astock_global_tools_do_not_bind_first_arg():
    config = {"active_ticker": "000988.SZ"}
    args = ("2026-05-17", 7, 5)

    routed = _bind_astock_ticker("get_global_news", "a_stock", args, config)

    assert routed == args


def test_unknown_vendors_do_not_bind_ticker():
    config = {"active_ticker": "000988.SZ"}
    args = ("AAPL", "2026-05-01", "2026-05-17")

    routed = _bind_astock_ticker("get_stock_data", "other_vendor", args, config)

    assert routed == args
