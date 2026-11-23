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
def test_paid_checkout_login_waits_for_buyer_confirmation(setup, use_player):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1}, max_units=100)
    s.e.config.save(cfg)
    s.full.amount = 100
    s.full.chat_id = "users-5-9"
    s.event.order.description = "Steam, Пополнение, UAH, 100 шт., other_login"
    if use_player:
        s.full.player = " 001steam_user "
    else:
        s.full.fields = {"custom": NS(name="Логин Steam", value={"ru": "001steam_user", "en": "001steam_user"})}
    s.e.new_order(s.event)
    s.e.new_order(s.event)
    s.e.tick()

    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "confirm" and not s.api.calls
    assert len(s.sent) == 1
    prompt = s.sent[0][1]
    assert "Это ваш логин Steam?" in prompt and "Логин Steam: 001steam_user" in prompt and "100 UAH" in prompt
    command = f"!gameau ORDER1 да {row['nonce']}"
    assert command in prompt
    s.e.message(NS(text=command, chat_id=10, author_id=6, interlocutor_id=5))
    s.e.message(NS(text=command, chat_id=11, author_id=5, interlocutor_id=None))
    s.e.message(NS(text="!gameau ORDER1 да", chat_id=10, author_id=5, interlocutor_id=5))
    assert "Скопируйте команду" in s.sent[-1][1]
    assert not s.api.calls

    confirmation = NS(text=command, chat_id=10, author_id=5, interlocutor_id=5)
    s.e.message(confirmation)
    s.e.message(confirmation)

    quote, purchase = [body for path, body, _ in s.api.calls
                       if path in {"/steamTopup/quote", "/orders/steamTopup"}]
    for payload in (quote, purchase):
        assert (payload["steamLogin"], payload["amount"], payload["currency"]) == ("001steam_user", 100, "UAH")
    assert len(s.api.orders) == 1
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
    s.e.message(NS(text=f"!gameau ORDER1 да {row['nonce']}", chat_id=10, author_id=5))

    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "blocked" and "Логин Steam" in row["error"]
    assert not s.api.orders


def test_buyer_can_replace_checkout_login_then_confirm(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "mistyped_user"
    s.full.chat_id = "users-5-9"
    s.e.new_order(s.event)
    s.full.player = "different_user"
    s.e.message(NS(text="!gameau ORDER1 заново", chat_id=10, author_id=5, interlocutor_id=5))
    assert "!gameau ORDER1 ЗНАЧЕНИЕ" in s.sent[-1][1]
    s.e.message(NS(text="!gameau ORDER1 correct_user", chat_id=10, author_id=5, interlocutor_id=5))
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == "confirm" and "Логин Steam: correct_user" in s.sent[-1][1]
    assert not s.api.calls
    s.e.message(NS(text=f"!gameau ORDER1 да {row['nonce']}", chat_id=10, author_id=5, interlocutor_id=5))
    purchase = next(body for path, body, _ in s.api.calls if path == "/orders/steamTopup")
    assert purchase["steamLogin"] == "correct_user"


@pytest.mark.parametrize("phrase", ["Да", "Всё верно"])
def test_short_steam_reply_confirms_one_recent_prompt_in_private_chat(setup, phrase):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.e.panel.home(s.e.panel.context(message()))
    click(s.e, "steam_messages")
    click(s.e, "steam_reply")
    s.e.panel.input(message(phrase))
    assert p.Config(s.root).snapshot()["steam_confirm_phrase"] == phrase

    s.full.player = "steam_user"
    s.full.chat_id = "users-5-9"
    original = s.c.send_message

    def send(chat, text, buyer):
        original(chat, text, buyer)
        return [NS(id=120)]

    s.c.send_message = send
    s.e.new_order(s.event)
    assert f"ответьте: {phrase}" in s.sent[-1][1]
    reply = f" {phrase.upper()} "
    s.e.message(NS(text=reply, chat_id=10, id=119, author_id=5, interlocutor_id=5))
    s.e.message(NS(text=reply, chat_id=11, id=121, author_id=5, interlocutor_id=None))
    s.e.message(NS(text=reply, chat_id=10, id=122, author_id=6, interlocutor_id=5))
    assert not s.api.calls
    buyer = NS(text=reply, chat_id=10, id=123, author_id=5, interlocutor_id=5)
    s.e.message(buyer)
    s.e.message(buyer)
    assert s.e.ledger.get("ORDER1")["state"] == "pending"
    assert "Логин Steam подтверждён" in s.sent[-1][1] and len(s.api.orders) == 1


def test_short_reply_asks_for_order_number_when_two_orders_are_waiting(setup):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "first_user"
    s.full.chat_id = "users-5-9"
    other = NS(**(vars(s.full) | {"id": "ORDER2", "player": "second_user"}))
    s.c.account.get_order = {"ORDER1": s.full, "ORDER2": other}.__getitem__
    s.c.send_message = lambda *args: s.sent.append(args) or [NS(id=120 + len(s.sent))]
    s.e.new_order(s.event)
    s.event.order.id = "ORDER2"
    s.e.new_order(s.event)
    ambiguous = NS(text="Да", chat_id=10, id=128, author_id=5, interlocutor_id=5)
    s.e.message(ambiguous)
    assert "несколько заказов" in s.sent[-1][1] and not s.api.calls

    row = s.e.ledger.get("ORDER2")
    s.e.message(NS(text=f"!gameau ORDER2 Да {row['nonce']}", chat_id=10, id=129, author_id=5, interlocutor_id=5))
    s.e.message(ambiguous)
    assert s.e.ledger.get("ORDER1")["state"] == "confirm"
    assert s.e.ledger.get("ORDER2")["state"] == "pending" and len(s.api.orders) == 1


@pytest.mark.parametrize("old_state", ["legacy", "expired", "CLOSED", "REFUNDED"])
def test_old_order_does_not_block_short_reply_for_current_steam_order(setup, old_state):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1}, max_units=100)
    s.e.config.save(cfg)
    s.full.player = "first_user"
    s.full.chat_id = "users-5-9"
    other = NS(**(vars(s.full) | {"id": "ORDER2", "player": "second_user", "amount": 7, "status": NS(name="PAID")}))
    s.c.account.get_order = {"ORDER1": s.full, "ORDER2": other}.__getitem__
    s.c.send_message = lambda *args: s.sent.append(args) or [NS(id=120 + len(s.sent))]
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    if old_state == "legacy":
        row.pop("short_reply_after")
    elif old_state == "expired":
        row["confirm_until"] = 0
    else:
        s.full.status.name = old_state
    s.e.ledger.put(row)

    s.event.order.id = "ORDER2"
    s.e.new_order(s.event)
    s.e.message(NS(text="Да", chat_id=10, id=128, author_id=5, interlocutor_id=5))

    purchase = next(body for path, body, _ in s.api.calls if path == "/orders/steamTopup")
    assert (purchase["steamLogin"], purchase["amount"], purchase["currency"]) == ("second_user", 7, "UAH")
    assert s.e.ledger.get("ORDER1")["state"] == "confirm"
    assert s.e.ledger.get("ORDER2")["state"] == "pending" and len(s.api.orders) == 1


def test_expired_checkout_confirmation_requires_a_fresh_code(setup, monkeypatch):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "steam_user"
    codes = iter(("aaaaaa", "bbbbbb"))
    monkeypatch.setattr(p.secrets, "token_hex", lambda _: next(codes))
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    s.e.phase(row, "confirm", confirm_until=0)
    expired = NS(text="!gameau ORDER1 да aaaaaa", chat_id=10, author_id=5)
    s.e.message(expired)
    assert "!gameau ORDER1 да bbbbbb" in s.sent[-1][1]
    s.e.message(expired)
    assert s.e.ledger.get("ORDER1")["state"] == "confirm" and not s.api.calls
    s.e.message(NS(text="!gameau ORDER1 да bbbbbb", chat_id=10, author_id=5))
    assert len(s.api.orders) == 1


def test_ready_order_from_an_earlier_version_asks_for_confirmation(setup, monkeypatch):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "steam_user"
    with monkeypatch.context() as patch:
        patch.setattr(s.e, "process", lambda row: None)
        s.e.new_order(s.event)

    assert s.e.ledger.get("ORDER1")["state"] == "ready"
    s.e.tick()
    assert s.e.ledger.get("ORDER1")["state"] == "confirm"
    assert "Логин Steam: steam_user" in s.sent[-1][1] and not s.api.calls


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_steam_chat_uses_saved_templates_and_reports_only_success(setup, monkeypatch, status):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = dict(rule("steamTopup"), params={"currency": "UAH", "amount": 1}, max_units=100)
    s.e.config.save(cfg)
    s.e.panel.home(s.e.panel.context(message()))
    click(s.e, "steam_messages")
    click(s.e, "steam_msg_edit:confirm")
    s.e.panel.input(message("Сверьте данные заказа {order_id}."))
    click(s.e, "steam_msg_edit:done")
    s.e.panel.input(message("Готово #{order_id}: {login}, {amount} {currency} за {quantity} шт."))
    assert p.Config(s.root).snapshot()["steam_messages"] == s.e.config.snapshot()["steam_messages"]

    s.full.player = "steam_user"
    s.full.amount = 100
    original = s.api.call

    def call(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path.startswith("/orders/status/"):
            result["order"]["keys"] = []
        return result

    monkeypatch.setattr(s.api, "call", call)
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    text = s.sent[-1][1]
    assert text.startswith("Сверьте данные заказа ORDER1.")
    assert "Логин Steam: steam_user" in text and "100 UAH" in text
    command = f"!gameau ORDER1 да {row['nonce']}"
    assert command in text and "!gameau ORDER1 заново" in text
    s.e.message(NS(text=command, chat_id=10, author_id=5))
    assert len(s.sent) == 2 and "Логин Steam подтверждён" in s.sent[-1][1]
    assert s.e.ledger.get("ORDER1")["state"] == "pending"

    s.api.status = status
    s.e.poll(s.e.ledger.get("ORDER1"))
    s.e.tick()
    if status == "completed":
        assert s.e.ledger.get("ORDER1")["state"] == "done"
        assert [entry[1] for entry in s.sent[2:]] == ["Готово #ORDER1: steam_user, 100 UAH за 100 шт."]
    else:
        assert s.e.ledger.get("ORDER1")["state"] == "failed" and len(s.sent) == 2

    click(s.e, "steam_msg_reset")
    assert s.e.config.snapshot()["steam_messages"] == p.STEAM_MESSAGE_DEFAULTS


@pytest.mark.parametrize("result_id, result_status, state", [
    ("ord-topup-a19", " COMPLETED ", "done"),
    ("ord-topup-other", "completed", "pending"),
    ("ord-topup-a19", "mystery", "pending"),
])
def test_steam_polls_order_id_even_if_purchase_response_has_no_recognized_status(
        setup, monkeypatch, result_id, result_status, state):
    s = setup
    cfg = s.e.config.snapshot()
    cfg["lots"]["123"] = rule("steamTopup")
    s.e.config.save(cfg)
    s.full.player = "steam_user"
    original = s.api.call

    def call(path, *args, **kwargs):
        if path == "/orders/status/ord-topup-a19":
            return {"ok": True, "order": {"orderId": result_id, "status": result_status}}
        result = original(path, *args, **kwargs)
        if path == "/orders/steamTopup":
            result["order"].pop("id")
            result["order"].pop("status")
            result["order"]["orderId"] = "ord-topup-a19"
        return result

    monkeypatch.setattr(s.api, "call", call)
    s.e.new_order(s.event)
    row = s.e.ledger.get("ORDER1")
    s.e.message(NS(text=f"!gameau ORDER1 да {row['nonce']}", chat_id=10, author_id=5))
    s.e.tick()
    row = s.e.ledger.get("ORDER1")
    assert row["state"] == state and row["remote_id"] == "ord-topup-a19" and len(s.api.orders) == 1
    assert not any("запрос покупки требует сверки" in notification for notification in s.notifications)
    if state == "done":
        assert [entry[1].splitlines()[0] for entry in s.sent[2:]] == ["✅ Заказ #ORDER1 выполнен!"]
    else:
        assert len(s.sent) == 2 and row["error"]


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
