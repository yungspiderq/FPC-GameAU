import copy
import uuid
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_plugin import catalog_item, click, message, p, rule
from test_plugin import setup as setup


def purchases(api):
    return [body for path, body, _ in api.calls if path.startswith("/orders/") and body is not None]


@pytest.mark.parametrize("kind", list(p.KINDS))
def test_end_to_end_each_supported_kind(setup, kind):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule(kind)
    s.e.config.save(cfg)
    s.api.status = "completed"
    s.e.new_order(s.event)
    for field in rule(kind)["inputs"]:
        value = "https://steamcommunity.com/id/player123" if field["path"] == "invite_url" else "player123"
        s.e.message(NS(text="!gameau ORDER1 " + value, chat_id=10, author_id=5))
    row = s.e.ledger.get("ORDER1")
    if row["state"] == "confirm":
        s.e.message(NS(text="!gameau ORDER1 да " + row["nonce"], chat_id=10, author_id=5))
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    assert len(purchases(s.api)) == 1
    assert next(iter(s.api.orders.values()))["kind"] == kind
    assert purchases(s.api)[0]["maxCharge"] <= 10
    assert len(s.sent) >= 1


@pytest.mark.parametrize("status", ["success", "completed", "complete", "delivered"])
def test_all_documented_success_statuses(setup, status):
    setup.api.status = status
    setup.e.new_order(setup.event)
    assert setup.e.ledger.get("ORDER1")["state"] == "done"


def test_minimal_documented_status_response_is_accepted(setup):
    s = setup
    s.e.new_order(s.event)
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.startswith("/orders/status/"):
            return {"ok": True, "order": {"id": "ord-1", "status": "completed", "keys": ["KEY"]}}
        return result
    s.api.call = call
    s.e.poll(s.e.ledger.get("ORDER1"))
    assert s.e.ledger.get("ORDER1")["state"] == "done"


def test_idempotency_uuid_is_header_not_body(tmp_path):
    config = p.Config(tmp_path)
    cfg = config.snapshot()
    cfg["api_key"] = "TEST-API-KEY"
    config.save(cfg)
    session = NS(request=Mock(return_value=NS(status_code=201, json=lambda: {"ok": True})))
    api = p.API(config, session)
    key = str(uuid.uuid4())
    body = {"category_id": "a", "card_id": "b", "quantity": 1, "maxCharge": 1.25}
    api.call("/orders/giftcards", body, idempotency_key=key)
    args = session.request.call_args
    assert args.args == ("POST", "https://gameau.us/api/v1/orders/giftcards")
    assert args.kwargs["headers"]["Idempotency-Key"] == key
    assert args.kwargs["json"] == body


@pytest.mark.parametrize("reason", ["stale", "stock", "unavailable", "changed_template", "duplicate", "wrong_endpoint", "currency"])
def test_catalog_fail_closed_before_charge(setup, reason):
    s = setup
    item = catalog_item()
    if reason == "stock":
        item["stock"] = 0
    elif reason == "unavailable":
        item["orderable"] = False
    elif reason == "changed_template":
        item["order"]["body"]["card_id"] = "different"
    elif reason == "wrong_endpoint":
        item["order"]["endpoint"] = "https://untrusted.invalid/orders/giftcards"
    elif reason == "currency":
        item["currency"] = "RUB"
    s.api.catalog_override = [item, copy.deepcopy(item)] if reason == "duplicate" else [item]
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if reason == "stale" and path == "/catalog":
            result["catalog"]["stale"] = True
        return result
    s.api.call = call
    s.e.new_order(s.event)
    assert not purchases(s.api)
    assert s.e.ledger.get("ORDER1")["state"] == "blocked"


def test_max_charge_is_capped_by_balance_minus_reserve(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["min_balance"] = "3"
    s.e.config.save(cfg)
    s.api.balance = 7
    s.e.new_order(s.event)
    assert purchases(s.api)[0]["maxCharge"] == 4


@pytest.mark.parametrize("reason", ["recipient", "refill", "endpoint", "price"])
def test_steam_quote_checked_before_purchase(setup, reason):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.endswith("/quote"):
            q = result["quote"]
            if reason == "recipient":
                q["order"]["body"]["steamLogin"] = "somebody_else"
            elif reason == "refill":
                q["can_refill"] = False
            elif reason == "endpoint":
                q["order"]["endpoint"] = "/api/v1/orders/gamekeys"
            else:
                q["charge"] = 9
        return result
    s.api.call = call
    s.e.new_order(s.event)
    s.e.message(NS(text="!gameau ORDER1 player123", chat_id=10, author_id=5))
    nonce = s.e.ledger.get("ORDER1")["nonce"]
    s.e.message(NS(text="!gameau ORDER1 да " + nonce, chat_id=10, author_id=5))
    assert not purchases(s.api)


def test_cards_are_delivered_and_duplicate_cards_stopped(setup):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    row.update(codes=[], cards=[{"code": "CARD123", "pin": "1234"}])
    s.e.deliver(row)
    assert "1234" in s.sent[0][1]
    assert not any("1234" in n for n in s.notifications)
    s.full.id = s.event.order.id = "ORDER2"
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER2")
    row.update(codes=[], cards=[{"pin": "1234", "code": "CARD123"}])
    s.e.deliver(row)
    assert s.e.ledger.get("ORDER2")["state"] == "delivery_review"
    assert len(s.sent) == 1


def test_partial_codes_are_not_marked_delivered(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"]["max_units"] = 2
    s.e.config.save(cfg)
    s.full.amount = 2
    s.api.status = "completed"
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"
    assert not s.sent


@pytest.mark.parametrize("kind", ["telegramStars", "telegramPremium", "topups", "manual", "gamekeys"])
def test_catalog_wizard_uses_current_template(setup, kind):
    s = setup
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "setup:123")
    click(s.e, "kind:" + kind)
    click(s.e, "product:0")
    panel.input(message("5"))
    if kind == "gamekeys":
        panel.input(message("1"))
    click(s.e, "save")
    saved = s.e.config.snapshot()["lots"]["123"]
    assert saved["kind"] == kind and saved["catalog"]["product_id"] == "sku-" + kind
    assert not purchases(s.api)


def test_steam_gift_wizard_preserves_edition_and_region(setup):
    s = setup
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "setup:123")
    click(s.e, "kind:steamGifts")
    panel.input(message("1245620"))
    click(s.e, "offer:0")
    panel.input(message("5"))
    click(s.e, "save")
    assert s.e.config.snapshot()["lots"]["123"]["params"] == {"app_id": 1245620, "sub_id": 456, "region": "RU"}


def guard_setup(s, active=True):
    state = {123: active, 456: True}
    s.c.account.get_lot_fields = lambda lid: NS(lot_id=lid, active=state[lid], auto_delivery=False)
    def save(fields):
        state[fields.lot_id] = fields.active
    s.c.account.save_lot = save
    cfg = s.e.config.snapshot()
    cfg.update(guard_enabled=True, guard_threshold="5")
    s.e.config.save(cfg)
    s.api.balance = 4
    return state


def test_low_balance_deactivates_only_mapped_lots_and_blocks_reactivation(setup):
    s = setup
    state = guard_setup(s)
    s.e.guard.check(force=True)
    assert state == {123: False, 456: True}
    assert s.e.ledger.holds() == {"123": True}
    with pytest.raises(p.PluginError):
        s.c.account.save_lot(NS(lot_id=123, active=True))
    assert state[123] is False


def test_balance_recovery_requires_explicit_restore(setup):
    s = setup
    state = guard_setup(s)
    s.e.guard.check(force=True)
    s.api.balance = 10
    s.e.guard.check(force=True)
    assert not state[123]
    assert s.e.guard.restore() == (["123"], [])
    assert state[123] and not s.e.ledger.holds()


def test_preexisting_inactive_lot_is_not_reenabled(setup):
    s = setup
    state = guard_setup(s, active=False)
    s.e.guard.check(force=True)
    assert s.e.ledger.holds() == {"123": False}
    s.api.balance = 10
    s.e.guard.restore()
    assert not state[123] and not s.e.ledger.holds()


def test_guard_test_mode_does_not_write_funpay(setup):
    s = setup
    state = guard_setup(s)
    cfg = s.e.config.snapshot()
    cfg["dry_run"] = True
    s.e.config.save(cfg)
    s.e.guard.check(force=True)
    assert state[123] and not s.e.ledger.holds()


def test_guard_does_not_treat_network_failure_as_zero_balance(setup):
    s = setup
    state = guard_setup(s)
    s.api.call = Mock(side_effect=p.APIError("timeout"))
    s.e.guard.check(force=True)
    assert state[123] and not s.e.ledger.holds()


def test_guard_hold_survives_restart_and_save_timeout(setup):
    s = setup
    state = guard_setup(s)
    def save_then_timeout(fields):
        state[fields.lot_id] = fields.active
        raise TimeoutError("accepted but response lost")
    s.c.account.save_lot = save_then_timeout
    s.e.guard.check(force=True)
    assert not state[123] and s.e.ledger.holds() == {"123": True}
    s.e.close()
    s.c.account.save_lot = lambda fields: state.update({fields.lot_id: fields.active})
    other = p.Engine(s.c, s.root, s.api)
    try:
        other.guard.install()
        with pytest.raises(p.PluginError):
            s.c.account.save_lot(NS(lot_id=123, active=True))
        s.api.balance = 10
        assert other.guard.restore() == (["123"], [])
        assert state[123]
    finally:
        other.close()


def test_guard_threshold_is_inclusive(setup):
    s = setup
    state = guard_setup(s)
    s.api.balance = 5
    s.e.guard.check(force=True)
    assert not state[123]
    with pytest.raises(p.PluginError):
        s.e.guard.restore()


def test_guard_operates_while_procurement_is_paused(setup):
    s = setup
    state = guard_setup(s)
    cfg = s.e.config.snapshot()
    cfg["enabled"] = False
    s.e.config.save(cfg)
    s.e.tick()
    assert not state[123]


def test_guard_settings_buttons(setup):
    s = setup
    guard_setup(s)
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "guard")
    click(s.e, "guardlimit")
    panel.input(message("3.50"))
    assert s.e.config.snapshot()["guard_threshold"] == "3.50"
    click(s.e, "guardtoggle")
    assert not s.e.config.snapshot()["guard_enabled"]
