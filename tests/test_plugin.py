import copy
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock

import pytest
import requests

spec = importlib.util.spec_from_file_location("gameau", Path(__file__).parents[1] / "gameau.py")
p = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = p
spec.loader.exec_module(p)


def rule(kind="giftcards"):
    params = {
        "giftcards": {"category_id": "cat", "card_id": "card"},
        "gamekeys": {"game_id": "game", "key_id": "key"},
        "topups": {"category_id": "game", "offer_id": "pack", "fields": {}},
        "manual": {"manual_service_id": "service", "product_id": "product", "fields": {}},
        "steamTopup": {"amount": 100, "currency": "RUB"},
        "steamGifts": {"app_id": "123", "sub_id": "456", "region": "RU"},
        "telegramStars": {"quantity": 50},
        "telegramPremium": {"months": 3},
    }[kind]
    path = {"topups": "fields.player_id", "manual": "fields.player_id", "steamTopup": "steamLogin",
            "steamGifts": "invite_url", "telegramStars": "telegram_username",
            "telegramPremium": "telegram_username"}.get(kind)
    return {"kind": kind, "catalog": {"product_id": "sku-" + kind, "category_id": "category", "region_code": "GLOBAL"},
            "params": params, "max_cost": "10", "max_units": 1, "quantity": 1,
            "inputs": [{"path": path}] if path else [], "enabled": True}



def catalog_item(kind="giftcards"):
    r = rule(kind)
    body = copy.deepcopy(r["params"])
    if kind in {"giftcards", "gamekeys"}:
        body["quantity"] = 1
    if kind in {"telegramStars", "telegramPremium"}:
        body["telegram_username"] = "TELEGRAM_USERNAME"
    return {"kind": kind, **r["catalog"], "name": "Test product", "currency": "USD", "available": True,
            "orderable": True, "stock": 100, "min_quantity": 1, "max_quantity": 100,
            "fields": [{"key": "player_id", "label": "ID игрока", "type": "text", "required": True}]
                      if kind in {"topups", "manual"} else [],
            "price": 2, "order": {"endpoint": "/api/v1/orders/" + kind, "body": body}}


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.orders = {}
        self.status = "processing"
        self.total = 2
        self.balance = 100
        self.key_value = "TEST-KEY-NOT-REAL"
        self.fail_create = False
        self.fail_poll = False
        self.on_create = None
        self.catalog_override = None
        self.can_refill = True

    def key(self):
        return self.key_value

    def call(self, path, payload=None, params=None, idempotency_key=None):
        self.calls.append((path, copy.deepcopy(payload), copy.deepcopy(params)))
        if path.endswith("/quote"):
            kind = path.split("/")[1]
            return {"ok": True, "quote": {"charge": self.total, "charge_currency": "USD", "can_refill": self.can_refill,
                    "order": {"endpoint": "/api/v1/orders/" + kind, "body": {**payload, "maxCharge": self.total}}}}
        if path.startswith("/orders/") and payload is not None:
            assert idempotency_key
            key = idempotency_key
            if key in self.orders:
                assert self.orders[key]["request"] == payload
            self.orders.setdefault(key, {"id": "ord-" + str(len(self.orders) + 1), "kind": path.rsplit("/", 1)[1],
                                        "request": copy.deepcopy(payload), "status": "processing",
                                        "chargedAmount": self.total, "keys": []})
            if self.on_create:
                self.on_create()
            if self.fail_create:
                raise p.APIError("Сеть/таймаут")
            return {"ok": True, "order": copy.deepcopy(self.orders[key])}
        if path.startswith("/orders/status/"):
            if self.fail_poll:
                raise p.APIError("Сбой чтения", 503)
            row = next(copy.deepcopy(o) for o in self.orders.values() if path.endswith(o["id"]))
            row.update(status=self.status, keys=["SECRET-CODE-A"] if self.status in p.SUCCESS_STATES else [])
            return {"ok": True, "order": row}
        if path == "/account":
            return {"ok": True, "account": {"balance": self.balance, "currency": "USD", "status": "active", "username": "test"}}
        if path == "/catalog":
            item = catalog_item(params["type"])
            item["price"] = self.total
            return {"ok": True, "catalog": {"currency": "USD", "stale": False, "total": 1, "offset": 0,
                    "limit": 100, "next_offset": None, "items": self.catalog_override if self.catalog_override is not None else [item],
                    "steam_topup": {"available": True}}}
        if path.startswith("/steamGifts/games/"):
            return {"ok": True, "game": {"app_id": int(path.rsplit("/", 1)[1]), "currency": "USD", "name": "Game",
                    "offers": [{"sub_id": 456, "name": "Edition", "regions": [{"region": "RU", "price": 2, "available": True}]}]}}
        raise AssertionError("Unexpected endpoint " + path)



class Bot:
    def __init__(self):
        self.messages = []
        self.deleted = []

    def send_message(self, chat, text, **kwargs):
        mid = len(self.messages) + 1
        self.messages.append(NS(chat=chat, text=text, mid=mid, **kwargs))
        return NS(message_id=mid)

    def edit_message_text(self, text, chat, mid, **kwargs):
        self.messages.append(NS(chat=chat, text=text, mid=mid, **kwargs))

    def delete_message(self, chat, mid):
        self.deleted.append((chat, mid))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("GAMEAU_API_KEY", raising=False)
    tg_bot = ModuleType("tg_bot")
    tg_bot.CBT = NS(EDIT_PLUGIN="45", PLUGIN_SETTINGS="47")
    monkeypatch.setitem(sys.modules, "tg_bot", tg_bot)
    cat = NS(id=3)
    lot = NS(id=123, description="Unique card", subcategory=cat, server=None, side=None, auto=False)
    lots = {123: lot}
    profile = NS(get_sorted_lots=lambda _: lots, update_lot=lambda item: lots.update({item.id: item}))
    full = NS(id="ORDER1", short_description=lot.description, subcategory=cat, server=None, side=None,
              buyer_id=5, buyer_username="buyer", seller_id=9, status=NS(name="PAID"), chat_id=10,
              amount=1, order_secrets=[])
    notifications, sent = [], []
    c = NS(account=NS(id=9, get_order=lambda _: full,
                      get_user=lambda _: NS(id=9, get_sorted_lots=lambda _: lots),
                      get_lot_fields=lambda _: NS(auto_delivery=False)),
           profile=profile, plugins={p.UUID: NS(enabled=True)}, blacklist=[], bl_delivery_enabled=True,
           old_mode_enabled=False, telegram=NS(bot=Bot(), authorized_users=[1], send_notification=notifications.append))
    c.send_message = lambda chat, text, buyer: sent.append((chat, text, buyer)) or True
    api = FakeAPI()
    e = p.Engine(c, tmp_path, api)
    cfg = e.config.snapshot()
    cfg.update(enabled=True, dry_run=False, live_confirmed=True, lots={"123": rule()})
    e.config.save(cfg)
    event = NS(order=NS(id="ORDER1", buyer_id=5), lot_id=123, lot_shortcut=lot, delivered=False, config_section_obj=None)
    yield NS(e=e, c=c, api=api, full=full, event=event, sent=sent, notifications=notifications, root=tmp_path, lots=lots)
    e.close()


@pytest.mark.parametrize("kind", list(p.KINDS))
def test_all_documented_product_kinds_build_payloads(kind):
    r = rule(kind)
    values = {f["path"]: ("https://s.team/p/abc/def" if f["path"] == "invite_url" else "player123") for f in r["inputs"]}
    body = p.make_payload(r, 1, values)
    assert "kind" not in body and "product_key" not in body
    if kind in {"gamekeys", "giftcards"}:
        assert body["quantity"] == 1
    assert "idempotency_key" not in body and "maxCharge" not in body



@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0", True, None, "nonsense"])
def test_bad_money_rejected(value):
    with pytest.raises(p.PluginError):
        p.number(value)


@pytest.mark.parametrize("kind", ["steamGifts", "telegramStars", "telegramPremium", "topups", "manual"])
def test_service_does_not_silently_ignore_funpay_quantity(kind):
    r = rule(kind)
    r["max_units"] = 2
    with pytest.raises(p.PluginError):
        p.validate_rule(r)


@pytest.mark.parametrize("path", ["amount", "quantity", "region", "card_id", "fields.password", "fields.api_token"])
def test_buyer_cannot_choose_price_product_or_secrets(path):
    r = rule("topups")
    r["inputs"] = [{"path": path}]
    with pytest.raises(p.PluginError):
        p.validate_rule(r)


def test_duplicate_event_and_pending_response_do_not_deliver(setup):
    s = setup
    s.e.new_order(s.event)
    s.e.new_order(s.event)
    assert len(s.api.orders) == 1
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "pending"
    assert sum((path.startswith("/orders/") and not path.startswith("/orders/status/")) for path, _, _ in s.api.calls) == 1


def test_completed_order_delivered_once(setup):
    s = setup
    s.api.status = "completed"
    s.e.new_order(s.event)
    s.e.new_order(s.event)
    s.e.tick()
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    assert len(s.sent) == 1 and "SECRET-CODE-A" in s.sent[0][1]
    assert not any("SECRET-CODE-A" in n for n in s.notifications)


@pytest.mark.parametrize("status", ["processing", "failed", "cancelled", "refunded", "canceled", "rejected"])
def test_non_completed_status_never_delivers(setup, status):
    setup.api.status = status
    setup.e.new_order(setup.event)
    assert not setup.sent
    assert setup.e.ledger.get("ORDER1")["state"] == ("pending" if status == "processing" else "failed")


def test_timeout_saved_body_and_key_survive_restart(setup):
    s = setup
    s.api.fail_create = True
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "uncertain" and len(s.api.orders) == 1
    payload = copy.deepcopy(row["payload"])
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        other.tick()  # never automatically buy again after an uncertain POST
        assert len([c for c in s.api.calls if (c[0].startswith("/orders/") and not c[0].startswith("/orders/status/"))]) == 1
        s.api.fail_create = False
        other.create(other.ledger.get("ORDER1"))
        assert len(s.api.orders) == 1
        assert [b for path, b, _ in s.api.calls if (path.startswith("/orders/") and not path.startswith("/orders/status/"))] == [payload, payload]
    finally:
        other.close()


def test_disk_record_exists_before_paid_request(setup):
    s = setup
    def inspect():
        row = p.Ledger(s.root).get("ORDER1")
        assert row["idempotency_key"] in s.api.orders
        assert row["state"] == "uncertain"  # simulated restart turns creating into uncertain
    s.api.on_create = inspect
    s.e.new_order(s.event)


def test_pending_orders_resume_polling_after_restart(setup):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    row["last_poll"] = 0
    s.e.ledger.put(row)
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        s.api.status = "completed"
        other.tick()
        assert other.ledger.get("ORDER1")["state"] == "done"
        assert len(s.api.orders) == 1 and len(s.sent) == 1
    finally:
        other.close()


def test_delivery_ambiguity_requires_manual_retry_without_buying(setup):
    s = setup
    s.api.status = "completed"
    s.c.send_message = lambda *args: False
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"
    s.e.tick()
    assert len(s.api.orders) == 1
    s.c.send_message = lambda *args: True
    s.e.deliver(s.e.ledger.get("ORDER1"))
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    assert len(s.api.orders) == 1


def test_dry_run_never_buys_and_cannot_be_promoted(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["dry_run"] = True
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "dry_run"
    cfg["dry_run"] = False
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    s.e.tick()
    assert not s.api.orders and not s.sent


@pytest.mark.parametrize("reason", ["price", "funds", "reserve", "risk", "refunded", "seller", "quantity", "blacklist", "secrets"])
def test_payment_guards(setup, reason):
    s = setup
    cfg = s.e.config.snapshot()
    if reason == "price":
        s.api.total = 11
    elif reason == "funds":
        s.api.balance = 1
    elif reason == "reserve":
        cfg["min_balance"] = "99"
    elif reason == "risk":
        cfg["live_confirmed"] = False
    elif reason == "refunded":
        s.full.status.name = "REFUNDED"
    elif reason == "seller":
        s.full.seller_id = 88
    elif reason == "quantity":
        s.full.amount = 2
    elif reason == "blacklist":
        s.c.blacklist = ["buyer"]
    elif reason == "secrets":
        s.full.order_secrets = ["native"]
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    assert not s.api.orders


def test_native_cardinal_delivery_is_not_duplicated(setup):
    setup.event.delivered = True
    setup.e.new_order(setup.event)
    assert not setup.api.calls


def test_ambiguous_lot_is_not_guessed(setup):
    setup.lots[124] = copy.copy(setup.lots[123])
    setup.lots[124].id = 124
    setup.e.new_order(setup.event)
    assert not setup.api.orders


@pytest.mark.parametrize("hint", [None, 123])
def test_detailed_order_description_identifies_the_right_lot(setup, hint):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["124"] = cfg["lots"].pop("123")
    s.e.config.save(cfg)
    s.lots[124] = copy.copy(s.lots[123])
    s.lots[124].id = 124
    s.event.lot_id = hint
    s.event.order.subcategory = s.full.subcategory
    s.full.full_description = "Steam  Украина\n1 UAH за штуку"
    details = {123: "Steam Казахстан 1 KZT за штуку", 124: "Steam Украина 1\u00a0UAH за штуку"}
    s.c.account.get_lot_fields = lambda lid: NS(lot_id=lid, description_ru=details[lid], auto_delivery=False)
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    assert row["lot_id"] == "124" and row["state"] == "pending"
    assert len(s.api.orders) == 1


@pytest.mark.parametrize("problem", ["same_description", "unreadable", "native_delivery"])
def test_detailed_description_cannot_override_ambiguous_or_native_lot(setup, problem):
    s = setup
    s.lots[124] = copy.copy(s.lots[123])
    s.lots[124].id = 124
    s.full.full_description = "Steam Украина"

    def fields(lid):
        if lid == 124 and problem == "unreadable":
            raise TimeoutError
        text = s.full.full_description if lid == 123 or problem == "same_description" else "Steam Казахстан"
        return NS(lot_id=lid, description_ru=text, auto_delivery=lid == 123 and problem == "native_delivery")

    s.c.account.get_lot_fields = fields
    s.e.new_order(s.event)
    assert not s.api.orders
    assert s.e.ledger.get("ORDER1") is None
    if problem == "same_description":
        assert "#123, #124" in s.notifications[-1]


def test_order_description_can_be_verified_in_another_locale(setup):
    s = setup
    s.full.short_description = "Steam balance"
    s.full.full_description = "Ukraine, one UAH per item"
    translations = {("summary", "ru"): s.lots[123].description, ("desc", "ru"): "Украина, 1 UAH за штуку"}
    s.full.get_field_value = lambda field, locale: translations.get((field, locale))
    s.c.account.get_lot_fields = lambda lid: NS(lot_id=lid, description_ru=translations[("desc", "ru")],
                                                auto_delivery=False)
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["lot_id"] == "123"


def test_buyer_identity_and_confirmation(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.e.new_order(s.event)
    wrong = NS(text="!gameau ORDER1 player123", chat_id=10, author_id=6)
    s.e.message(wrong)
    assert s.e.ledger.get("ORDER1")["values"] == {}
    right = NS(text="!gameau ORDER1 player123", chat_id=10, author_id=5)
    s.e.message(right)
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "confirm" and not s.api.orders
    right.text = "!gameau ORDER1 да wrong"
    s.e.message(right)
    assert not s.api.orders
    right.text = "!gameau ORDER1 да " + row["nonce"]
    s.e.message(right)
    assert len(s.api.orders) == 1
    payload = next(b for path, b, _ in s.api.calls if (path.startswith("/orders/") and not path.startswith("/orders/status/")))
    assert payload["amount"] == 100 and payload["steamLogin"] == "player123"


def test_api_key_change_stops_recovery(setup):
    s = setup
    s.api.fail_create = True
    s.e.new_order(s.event)
    s.api.key_value = "OTHER-ACCOUNT"
    s.api.fail_create = False
    s.e.create(s.e.ledger.get("ORDER1"))
    assert len([c for c in s.api.calls if (c[0].startswith("/orders/") and not c[0].startswith("/orders/status/"))]) == 1


def test_dynamic_topup_fields_use_exact_names():
    item = catalog_item("topups")
    item["fields"] = [{"key": "userId", "label": "Player ID", "type": "text", "required": True},
                      {"key": "zone", "label": "Zone", "type": "text", "required": True}]
    r = p.catalog_rule(item)
    r["max_cost"] = "10"
    body = p.make_payload(r, 1, {"fields.userId": "00123", "fields.zone": "EU"})
    assert body["fields"] == {"userId": "00123", "zone": "EU"}


def test_missing_topup_fields_stops_wizard():
    item = catalog_item("topups")
    item.pop("fields")
    with pytest.raises(p.PluginError):
        p.catalog_rule(item)


def test_corrupt_settings_do_not_reset_to_defaults(tmp_path):
    (tmp_path / "config.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(ValueError):
        p.Config(tmp_path)
    assert (tmp_path / "config.json").read_text() == "{bad"


def test_atomic_write_failure_preserves_config(setup, monkeypatch):
    cfg = setup.e.config.snapshot()
    cfg["enabled"] = False
    monkeypatch.setattr(p.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        setup.e.config.save(cfg)
    assert setup.e.config.snapshot()["enabled"] is True
    assert json.loads((setup.root / "config.json").read_text())["enabled"] is True


def test_second_instance_rejected(setup):
    with pytest.raises(p.PluginError):
        p.Engine(setup.c, setup.root, setup.api)


def test_refund_while_supplier_processes_stops_delivery(setup):
    s = setup
    s.api.status = "completed"
    s.api.on_create = lambda: setattr(s.full.status, "name", "REFUNDED")
    s.e.new_order(s.event)
    assert len(s.api.orders) == 1
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"


@pytest.mark.parametrize("codes", [[], [""], [" "], ["SAME", "SAME"], [None]])
def test_missing_or_duplicate_codes_require_review(setup, codes):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    row.update(codes=codes, delivery=None)
    s.e.deliver(row)
    assert not s.sent
    assert s.e.ledger.get("ORDER1")["state"] == "delivery_review"


def test_same_supplier_code_in_second_order_is_not_delivered(setup):
    s = setup
    s.api.status = "completed"
    s.e.new_order(s.event)
    assert s.e.ledger.get("ORDER1")["state"] == "done"
    s.full.id = "ORDER2"
    s.event.order.id = "ORDER2"
    s.e.new_order(s.event)
    assert len(s.api.orders) == 2  # Provider incorrectly reused a code across distinct purchases.
    assert len(s.sent) == 1
    assert s.e.ledger.get("ORDER2")["state"] == "delivery_review"


def test_crash_during_message_send_requires_review_after_restart(setup):
    s = setup
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    s.e.phase(row, "delivering", codes=["CODE"], sent=0)
    s.e.close()
    other = p.Engine(s.c, s.root, s.api)
    try:
        assert other.ledger.get("ORDER1")["state"] == "delivery_review"
        other.tick()
        assert not s.sent and len(s.api.orders) == 1
    finally:
        other.close()


def test_pause_during_quote_prevents_purchase(setup):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == "/catalog":
            s.e.paused.set()
        return result
    s.api.call = call
    s.e.new_order(s.event)
    assert not s.api.orders


def test_account_change_prevents_purchase(setup):
    s = setup
    s.e.bind_account()
    s.c.account.id = 999
    s.full.seller_id = 999
    s.e.new_order(s.event)
    assert not s.api.orders


@pytest.mark.parametrize("bad", [{}, {"order": None}, {"order": {"id": "other", "kind": "wrong", "status": "completed"}}])
def test_malformed_paid_response_is_not_retried(setup, bad):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        return bad if (path.startswith("/orders/") and not path.startswith("/orders/status/")) else result
    s.api.call = call
    s.e.new_order(s.event)
    s.e.tick()
    assert s.e.ledger.get("ORDER1")["state"] == "uncertain"
    assert not s.sent and len(s.api.orders) == 1


def test_multiple_giftcards_use_total_quantity_and_total_price(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"].update(max_units=3, quantity=2)
    s.e.config.save(cfg)
    item = catalog_item()
    item["order"]["body"]["quantity"] = 2
    s.api.catalog_override = [item]
    s.full.amount = 3
    s.e.new_order(s.event)
    body = next(body for path, body, _ in s.api.calls if (path.startswith("/orders/") and not path.startswith("/orders/status/")))
    assert body["quantity"] == 6


def test_quote_mismatched_product_prevents_spending(setup):
    s = setup
    original = s.api.call
    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == "/catalog":
            result["catalog"]["items"][0]["product_id"] = "wrong-product"
        return result
    s.api.call = call
    s.e.new_order(s.event)
    assert not s.api.orders
    assert s.e.ledger.get("ORDER1")["state"] == "blocked"


@pytest.mark.parametrize("code", [200, 201, 401, 402, 403, 422, 429, 500, 302])
def test_http_contract_no_hidden_retries(tmp_path, code):
    cfg = p.Config(tmp_path)
    data = cfg.snapshot()
    data["api_key"] = "PRIVATE-KEY"
    cfg.save(data)
    response = NS(status_code=code, headers={"Retry-After": "120"}, json=lambda: {"ok": True})
    session = NS(request=Mock(return_value=response))
    api = p.API(cfg, session)
    if code in {200, 201}:
        assert api.call("/account") == {"ok": True}
    else:
        with pytest.raises(p.APIError) as error:
            api.call("/orders/giftcards", {"maxCharge": 1}, idempotency_key="02d8b48d-611d-46e9-b041-7bad5b482fa8")
        assert "PRIVATE" not in str(error.value)
    assert session.request.call_count == 1
    assert session.request.call_args.kwargs["allow_redirects"] is False
    if code == 429:
        assert api.cooldown > time.monotonic() + 100
        with pytest.raises(p.APIError):
            api.call("/account")
        assert session.request.call_count == 1


def test_network_exception_does_not_leak_credentials(tmp_path):
    cfg = p.Config(tmp_path)
    data = cfg.snapshot()
    data["api_key"] = "PRIVATE-KEY"
    cfg.save(data)
    session = NS(request=Mock(side_effect=requests.Timeout("PRIVATE-KEY")))
    with pytest.raises(p.APIError) as exc:
        p.API(cfg, session).call("/orders/giftcards", {"maxCharge": 1}, idempotency_key="02d8b48d-611d-46e9-b041-7bad5b482fa8")
    assert "PRIVATE-KEY" not in str(exc.value)


def message(text="/gameau", user=1, chat_type="private"):
    return NS(id=4, text=text, chat=NS(id=1, type=chat_type), from_user=NS(id=user))


def click(e, action, user=1, token=None):
    session = e.panel.sessions[(1, 1)]
    call = NS(message=NS(id=session["mid"], chat=NS(id=1, type="private")), from_user=NS(id=user),
              data=f"gameau:{token or session['token']}:{action}")
    e.panel.callback(call)


def test_telegram_giftcard_wizard(setup):
    s = setup
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "lots:0")
    click(s.e, "lot:123")
    click(s.e, "setup:123")
    click(s.e, "kind:giftcards")
    click(s.e, "product:0")
    panel.input(message("4.50"))
    panel.input(message("2"))
    click(s.e, "save")
    r = s.e.config.snapshot()["lots"]["123"]
    assert r["params"]["card_id"] == "card" and r["max_cost"] == "4.50" and r["max_units"] == 2
    assert not s.api.orders


def test_steam_wizard(setup):
    s = setup
    panel = s.e.panel
    panel.home(panel.context(message()))
    click(s.e, "setup:123")
    click(s.e, "kind:steamTopup")
    panel.input(message("100 RUB"))
    panel.input(message("4.50"))
    click(s.e, "save")
    assert s.e.config.snapshot()["lots"]["123"]["params"] == {"amount": 100.0, "currency": "RUB"}
    assert not s.api.orders


def test_unauthorized_and_stale_buttons_do_not_spend_or_change_settings(setup):
    e = setup.e
    e.panel.home(e.panel.context(message()))
    before = e.config.snapshot()
    click(e, "toggle", user=2)
    click(e, "toggle", token="stale")
    assert e.config.snapshot() == before


def test_secret_input_is_not_echoed(setup):
    e = setup.e
    e.panel.home(e.panel.context(message()))
    click(e, "key")
    e.panel.input(message("MY-VERY-PRIVATE-KEY"))
    assert e.config.snapshot()["api_key"] == "MY-VERY-PRIVATE-KEY"
    assert not any("MY-VERY-PRIVATE-KEY" in m.text for m in e.c.telegram.bot.messages)
    assert e.c.telegram.bot.deleted == [(1, 4)]


def test_new_funpay_lot_available_without_restart(setup):
    s = setup
    new = copy.copy(s.lots[123])
    new.id, new.description = 456, "Fresh lot"
    s.c.account.get_user = lambda _: NS(id=9, get_sorted_lots=lambda _: {123: s.lots[123], 456: new})
    assert 456 in s.e.refresh_lots() and s.lots[456] is new


def test_serial_worker_handles_simultaneous_duplicate_events(setup):
    e = setup.e
    threads = [threading.Thread(target=lambda: e.submit(e.new_order, setup.event)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    e.pool.submit(lambda: None).result(5)
    assert len(setup.api.orders) == 1
