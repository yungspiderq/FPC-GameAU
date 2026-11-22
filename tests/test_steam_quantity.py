from types import SimpleNamespace as NS

import pytest

from test_plugin import click, message, p, rule
from test_plugin import setup as setup


@pytest.mark.parametrize("units", [100, 10000])
def test_checkout_amount_is_the_sum_of_all_purchased_units(setup, units):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1},
                              max_units=p.STEAM_MAX_UNITS, max_topup_amount="10000")
    s.e.config.save(cfg)
    s.full.amount = units
    s.e.new_order(s.event)
    s.e.message(NS(text="!gameau ORDER1 steam_user", chat_id=10, author_id=5))
    row = s.e.ledger.get("ORDER1")
    assert f"{units} UAH" in s.sent[-1][1]
    assert not s.api.orders
    confirmation = NS(text=f"!gameau ORDER1 да {row['nonce']}", chat_id=10, author_id=5)
    s.e.message(confirmation)
    s.e.message(confirmation)
    requests = [body for path, body, _ in s.api.calls if path == "/orders/steamTopup"]
    assert len(requests) == 1
    assert requests[0]["amount"] == units and requests[0]["currency"] == "UAH"
    assert requests[0]["steamLogin"] == "steam_user"
    quote = next(body for path, body, _ in s.api.calls if path == "/steamTopup/quote")
    assert quote["amount"] == units


@pytest.mark.parametrize("use_player", [True, False])
def test_paid_checkout_login_is_used_without_chat_confirmation(setup, use_player):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1}, max_units=100)
    s.e.config.save(cfg)
    s.full.amount = 100
    s.event.order.description = "Steam, Пополнение, UAH, 100 шт., other_login"
    if use_player:
        s.full.player = " 001steam_user "
    else:
        s.full.fields = {"custom": NS(name="Логин Steam", value={"ru": "001steam_user", "en": "001steam_user"})}
    s.e.new_order(s.event)
    s.e.new_order(s.event)

    quote, purchase = [body for path, body, _ in s.api.calls
                       if path in {"/steamTopup/quote", "/orders/steamTopup"}]
    for payload in (quote, purchase):
        assert (payload["steamLogin"], payload["amount"], payload["currency"]) == ("001steam_user", 100, "UAH")
    assert len(s.api.orders) == 1 and not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "pending"


@pytest.mark.parametrize(("player", "field_value"), [(None, None), ("https://steamcommunity.com/id/test", None),
                                                     ("first_user", "other_user")])
def test_unusable_checkout_login_falls_back_to_buyer_chat(setup, player, field_value):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = player
    s.full.fields = {"steam_login": NS(name="Логин Steam", value=field_value)}
    s.event.order.description = "Steam, Пополнение, UAH, 1 шт., decoy_user"
    s.e.new_order(s.event)

    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "collecting" and not row["values"] and not s.api.calls
    assert "!gameau ORDER1 ЗНАЧЕНИЕ" in s.sent[-1][1]


def test_checkout_login_must_still_match_before_purchase(setup, monkeypatch):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "first_user"
    original = s.api.call

    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == "/steamTopup/quote":
            s.full.player = "other_user"
        return result

    monkeypatch.setattr(s.api, "call", call)
    s.e.new_order(s.event)

    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "blocked" and "логин Steam" in row["error"]
    assert not s.api.orders


@pytest.mark.parametrize("refund_enabled", [False, True])
def test_exceeding_uah_cap_stops_before_collecting_recipient(setup, refund_enabled):
    s = setup
    cfg = s.e.config.snapshot()
    cfg.update(auto_refund=refund_enabled, refund_reasons=["quantity"])
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1},
                              max_units=p.STEAM_MAX_UNITS, max_topup_amount="10000")
    s.e.config.save(cfg)
    s.full.amount = 10001
    s.full.player = "steam_user"
    s.c.account.refund = lambda _: setattr(s.full.status, "name", "REFUNDED")
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == ("refunded" if refund_enabled else "blocked")
    assert "10000 UAH" in row["error"]
    assert not s.api.calls and not s.sent


def test_fractional_nominal_multiplies_without_float_noise():
    configured = dict(rule("steamTopup"), params={"currency": "UAH", "amount": "0.1"}, max_units=100)
    payload = p.make_payload(configured, 7, {"steamLogin": "steam_user"})
    assert str(payload["amount"]) == "0.7"


def test_update_existing_steam_lot_limits_through_telegram(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1})
    s.e.config.save(cfg)
    s.e.panel.home(s.e.panel.context(message()))
    click(s.e, "lot:123")
    click(s.e, "steam_units:123")
    s.e.panel.input(message("0"))
    click(s.e, "steam_cap:123")
    s.e.panel.input(message("10000"))
    current = s.e.config.snapshot()["lots"]["123"]
    assert current["max_units"] == p.STEAM_MAX_UNITS
    assert current["max_topup_amount"] == "10000"
    assert current["params"]["amount"] == 1 and current["params"]["currency"] == "UAH"
    assert current["max_cost"] == cfg["lots"]["123"]["max_cost"]
    assert not s.api.calls
