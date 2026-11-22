from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_plugin import click, message, p, rule
from test_plugin import setup as setup


def enable_refunds(s, *reasons):
    cfg = s.e.config.snapshot()
    cfg.update(auto_refund=True, refund_reasons=list(reasons))
    s.e.config.save(cfg)

    def refund(oid):
        assert oid == s.full.id
        assert s.e.ledger.get(oid)["state"] == "refunding"
        s.full.status.name = "REFUNDED"

    s.c.account.refund = Mock(side_effect=refund)


@pytest.mark.parametrize("reason", ["price_limit", "quantity", "balance", "stock", "recipient", "topup_minimum"])
def test_selected_rejection_refunds_before_purchase(setup, reason):
    s = setup
    enable_refunds(s, "quantity" if reason == "topup_minimum" else reason)
    if reason == "price_limit":
        cfg = s.e.config.snapshot()
        cfg["lots"]["123"]["max_cost"] = "100"
        s.e.config.save(cfg)
        s.api.total, s.api.balance = 110, 200
    elif reason == "quantity":
        s.full.amount = 2
    elif reason == "balance":
        s.api.balance = 1
    elif reason == "stock":
        from test_plugin import catalog_item
        s.api.catalog_override = [dict(catalog_item(), stock=0)]
    else:
        cfg = s.e.config.snapshot()
        cfg["lots"]["123"] = rule("steamTopup")
        s.e.config.save(cfg)
        s.api.can_refill = False
        if reason == "topup_minimum":
            original = s.api.call
            def below_minimum(path, *args, **kwargs):
                if path == "/steamTopup/quote":
                    raise p.APIError("Too low", 422, "AMOUNT_TOO_LOW")
                return original(path, *args, **kwargs)
            s.api.call = below_minimum

    s.e.new_order(s.event)
    if reason in {"recipient", "topup_minimum"}:
        s.e.message(NS(text="!gameau ORDER1 steam_user", chat_id=10, author_id=5))
        nonce = s.e.ledger.get("ORDER1")["nonce"]
        s.e.message(NS(text=f"!gameau ORDER1 да {nonce}", chat_id=10, author_id=5))
    s.e.new_order(s.event)
    s.e.tick()
    s.c.account.refund.assert_called_once_with("ORDER1")
    assert s.e.ledger.get("ORDER1")["state"] == "refunded"
    assert not s.api.orders


@pytest.mark.parametrize("case", ["off", "unselected", "dry", "native", "timeout"])
def test_refund_requires_opt_in_live_order_and_definite_failure(setup, case):
    s = setup
    enable_refunds(s, "quantity" if case == "unselected" else "price_limit")
    cfg = s.e.config.snapshot()
    cfg["auto_refund"] = case != "off"
    cfg["dry_run"] = case == "dry"
    s.e.config.save(cfg)
    if case == "timeout":
        s.api.call = Mock(side_effect=p.APIError("Timeout"))
    else:
        s.api.total = 11
    if case == "native":
        s.c.account.get_lot_fields = lambda _: NS(auto_delivery=True)
    s.e.new_order(s.event)
    s.c.account.refund.assert_not_called()
    assert s.e.ledger.get("ORDER1")["state"] == "blocked"
    assert not s.api.orders


@pytest.mark.parametrize("replay", [False, True])
def test_provider_price_decline_is_safe_only_on_first_attempt(setup, replay):
    s = setup
    enable_refunds(s, "price_limit")
    original = s.api.call
    def outcome(path, *args, **kwargs):
        if path == "/orders/giftcards":
            raise p.APIError("Price changed", 409, "PRICE_CHANGED")
        return original(path, *args, **kwargs)

    if replay:
        s.api.fail_create = True  # Supplier received the purchase, but the first call timed out.
        s.e.new_order(s.event)
        assert s.e.ledger.get("ORDER1")["state"] == "uncertain"
        s.api.call = outcome
        row = s.e.ledger.get("ORDER1")
        s.e.phase(row, "creating")
        s.e.create(row)
        s.c.account.refund.assert_not_called()
        assert s.e.ledger.get("ORDER1")["state"] == "uncertain"
    else:
        s.api.call = outcome
        s.e.new_order(s.event)
        s.c.account.refund.assert_called_once_with("ORDER1")
        assert s.e.ledger.get("ORDER1")["state"] == "refunded"


@pytest.mark.parametrize("accepted", [False, True])
def test_timeout_from_funpay_is_reconciled_without_resending(setup, accepted):
    s = setup
    enable_refunds(s, "price_limit")
    s.api.total = 11
    def refund(_):
        if accepted:
            s.full.status.name = "REFUNDED"
        raise TimeoutError

    s.c.account.refund.side_effect = refund
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == ("refunded" if accepted else "refund_review")
    s.e.check_refund(row)
    s.e.new_order(s.event)
    s.e.tick()
    s.c.account.refund.assert_called_once()


def test_interrupted_refund_survives_restart_and_status_button_only_reads(setup):
    s = setup
    enable_refunds(s, "price_limit")
    s.api.total = 11
    s.c.account.refund.side_effect = SystemExit  # Stop after the durable marker and before the status read.
    with pytest.raises(SystemExit):
        s.e.new_order(s.event)
    s.e.close()
    s.e = p.Engine(s.c, s.root, s.api)
    try:
        assert s.e.ledger.get("ORDER1")["state"] == "refund_review"
        s.full.status.name = "REFUNDED"
        s.e.panel.home(s.e.panel.context(message()))
        click(s.e, "order:ORDER1")
        click(s.e, "refund_status:ORDER1")
        assert s.e.ledger.get("ORDER1")["state"] == "refunded"
        s.c.account.refund.assert_called_once()
        assert not s.api.orders
    finally:
        s.e.close()


def test_refund_switch_and_reason_buttons(setup):
    s = setup
    s.e.panel.home(s.e.panel.context(message()))
    click(s.e, "refund_settings")
    assert not s.e.config.snapshot()["auto_refund"]
    click(s.e, "refund_reason:quantity")
    click(s.e, "refund_toggle")
    cfg = s.e.config.snapshot()
    assert cfg["auto_refund"] and cfg["refund_reasons"] == ["price_limit", "quantity"]
