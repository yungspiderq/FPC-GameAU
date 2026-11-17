from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from test_plugin import click, message, p, rule
from test_plugin import setup as setup


@pytest.fixture
def shop(setup):
    engine = setup.e
    currency = NS(name="RUB", code="rub")
    card_currency = NS(name="USD")
    lot = NS(lot_id=123, price=200, currency=currency, active=True, auto_delivery=False,
             subcategory=NS(type="common", id=5), title_ru="Test card", amount=10)
    account = engine.c.account
    account.csrf_token = "test-csrf"
    account.get_lot_fields = Mock(side_effect=lambda _: NS(**vars(lot)))
    account.save_lot = Mock(side_effect=lambda fields: vars(lot).update(vars(fields)))
    account.method = Mock(return_value=NS(json=lambda: {"amount_int": "9 450,00"}))
    account.calc = Mock(side_effect=lambda *a, price, **kw: NS(account_currency=currency, methods=[
        NS(name="Карта", price=round(price * 1.12, 2), currency=currency),
        NS(name="Карта USD", price=3, currency=card_currency),
    ]))
    return NS(setup=setup, e=engine, lot=lot, account=account)


def configure(shop, **updates):
    config = shop.e.config.snapshot()
    config["pricing"].update(updates)
    shop.e.config.save(config)


def test_default_rate_uses_reverse_usdt_preview_and_margin_is_after_fees(shop):
    configure(shop, margin="20", extra_percent="5", usdt_per_usd="1.02")
    preview = shop.e.pricing.calculate("123")
    assert preview["withdrawal"] == (9450, 100)
    assert preview["cost"] == Decimal("192.78")
    assert preview["target"] == Decimal("253.66")
    assert preview["margin"] >= 20
    assert preview["methods"][0].price > float(preview["target"])
    args = shop.account.method.call_args.args
    assert args[:2] == ("post", "withdraw/calc")
    assert args[3]["amount_ext"] == "100" and args[3]["ext_currency_id"] == "usdt_trc"
    assert all(body is None for _, body, _ in shop.setup.api.calls)
    shop.account.save_lot.assert_not_called()


def test_auto_price_is_written_only_once_and_preserves_other_fields(shop):
    configure(shop, enabled=True, source="manual", manual_rates={"RUB": "90"})
    shop.e.tick()
    assert shop.lot.price == 200
    shop.account.save_lot.assert_not_called()  # Already at the rounded target.
    shop.setup.api.total = 3
    shop.e.pricing.checked.clear()
    shop.e.pricing.next_run = 0
    shop.e.tick()
    assert (shop.lot.price, shop.lot.title_ru, shop.lot.amount, shop.lot.active) == (300, "Test card", 10, True)
    shop.e.tick()
    shop.account.save_lot.assert_called_once()
    shop.account.method.assert_not_called()


@pytest.mark.parametrize("case", ["dry", "disabled", "manual", "bad_usdt_quote", "bad_buyer_quote", "inactive"])
def test_automation_never_writes_without_valid_inputs(shop, case):
    configure(shop, enabled=case != "disabled", manual_lots=["123"] if case == "manual" else [])
    if case == "dry":
        cfg = shop.e.config.snapshot()
        cfg["dry_run"] = True
        shop.e.config.save(cfg)
    elif case == "bad_usdt_quote":
        shop.account.method.return_value = NS(json=lambda: {"error": True, "amount_int": 5})
    elif case == "bad_buyer_quote":
        shop.account.calc.side_effect = TimeoutError
    elif case == "inactive":
        shop.lot.active = False
    shop.e.tick()
    shop.account.save_lot.assert_not_called()


def test_manual_flow_stays_available_when_gameau_is_down_and_excludes_lot_from_auto(shop):
    configure(shop, enabled=True)
    shop.setup.api.call = Mock(side_effect=TimeoutError)
    panel = shop.e.panel
    panel.home(panel.context(message()))
    click(shop.e, "pricing")
    click(shop.e, "price_list:0")
    click(shop.e, "price:123")
    click(shop.e, "price_manual:123")
    panel.input(message("222,90"))
    assert "249.65" in shop.e.c.telegram.bot.messages[-1].text
    click(shop.e, "price_save:123")
    assert shop.lot.price == 222.9
    assert shop.e.config.snapshot()["pricing"]["manual_lots"] == ["123"]
    shop.e.tick()
    shop.account.save_lot.assert_called_once()
    shop.setup.api.call.assert_not_called()


@pytest.mark.parametrize("change", ["settings", "price"])
def test_stale_preview_does_not_overwrite_changes(shop, change):
    preview = shop.e.pricing.calculate("123")
    if change == "settings":
        configure(shop, margin="22")
    else:
        shop.lot.price = 201
    with pytest.raises(p.PluginError):
        shop.e.pricing.apply(preview)
    shop.account.save_lot.assert_not_called()


@pytest.mark.parametrize("kind", ["steamTopup", "steamGifts"])
def test_steam_pricing_uses_quote_without_recipient(shop, kind):
    cfg = shop.e.config.snapshot()
    cfg["lots"]["123"] = rule(kind)
    shop.e.config.save(cfg)
    preview = shop.e.pricing.calculate("123")
    assert preview["price_usd"] == 2
    path, body, _ = shop.setup.api.calls[0]
    assert path == f"/{kind}/quote"
    assert set(body) == ({"amount", "currency"} if kind == "steamTopup" else {"app_id", "sub_id", "region"})
