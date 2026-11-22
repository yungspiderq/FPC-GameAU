"""GameAU supplier integration for sidor0912/FunPayCardinal, Python 3.10+."""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

NAME = "GameAU"
VERSION = "1.2.1"
DESCRIPTION = "Автовыдача GameAU: Steam, Telegram, игры, ключи и карты. /gameau"
CREDITS = "https://github.com/yungspiderq"
UUID = "7dbcd7e7-2193-43db-a5bb-072eab977041"
SETTINGS_PAGE = True
ROOT = Path("storage/plugins/gameau")
BASE_URL = "https://gameau.us/api/v1"
LOG = logging.getLogger("FPC.GameAU")
KINDS = {"steamTopup": "Пополнение Steam", "steamGifts": "Игра Steam в подарок",
         "telegramStars": "Telegram Stars", "telegramPremium": "Telegram Premium",
         "topups": "Пополнение игры", "giftcards": "Подарочная карта",
         "gamekeys": "Ключ игры", "manual": "Другой сервис"}
NAMES = {"steamLogin": "Логин Steam (не ник)", "invite_url": "Ссылка приглашения Steam",
         "telegram_username": "Username Telegram", "player_id": "ID игрока",
         "server_id": "ID сервера"}
DEFAULT = {"enabled": False, "dry_run": True, "api_key": "", "live_confirmed": False,
           "min_balance": "0", "lots": {}, "account_id": None,
           "guard_enabled": False, "guard_threshold": "0", "auto_refund": False,
           "refund_reasons": ["price_limit"]}
REFUND_REASONS = {
    "price_limit": "Цена выше лимита",
    "quantity": "Количество или сумма пополнения вне лимита",
    "recipient": "Steam не принимает пополнение получателя",
    "stock": "Недостаточно товара у GameAU",
    "balance": "Не хватает баланса GameAU или резерва",
}
PRICE_DEFAULT = {"enabled": False, "source": "funpay", "margin": "10", "withdraw_usdt": "100",
                 "usdt_per_usd": "1", "extra_percent": "0", "manual_rates": {}, "manual_lots": []}
PRICE_INTERVAL = 15 * 60
STEAM_MAX_UNITS = 10**8
STEAM_MAX_AMOUNT = Decimal("100000000")
STEAM_BILL_STEP = Decimal("0.01")
STEAM_CHARGE_STEP = Decimal("0.0001")
LOT_CURRENCIES = {"RUB", "USD", "EUR"}
SUCCESS_STATES = {"success", "completed", "complete", "delivered"}
FAIL_STATES = {"failed", "refunded", "cancelled", "canceled", "rejected"}
REMOTE_STATES = SUCCESS_STATES | FAIL_STATES | {"processing"}
ACTIVE_STATES = {"pending", "completed", "ready"}


class PluginError(Exception):
    """Only safe, locally generated messages; no credentials or supplier response bodies."""


class APIError(PluginError):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class OrderRejection(PluginError):
    """A specific, confirmed reason the order cannot be purchased."""
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def number(value, zero=False):
    try:
        if isinstance(value, bool):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or (not zero and result == 0):
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise PluginError("Нужно положительное число" + (" или ноль." if zero else ".")) from None


def integer(value, maximum=100000):
    n = number(value)
    if n != n.to_integral_value() or n > maximum:
        raise PluginError(f"Нужно целое число от 1 до {maximum}.")
    return int(n)


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def usd(value):
    n = number(value)
    if n > 1000000 or n.as_tuple().exponent < -4:
        raise PluginError("Сумма USD: не больше 1000000 и не больше 4 знаков после точки.")
    return n


def price_settings(saved):
    if not isinstance(saved, dict):
        raise PluginError("Неверные настройки автоцен.")
    cfg = copy.deepcopy(PRICE_DEFAULT)
    cfg.update(saved)
    if type(cfg["enabled"]) is not bool or cfg["source"] not in {"funpay", "manual"}:
        raise PluginError("Неверный режим автоцен или источник курса.")
    for field in ("margin", "extra_percent"):
        if number(cfg[field], zero=True) >= 100:
            raise PluginError("Процент должен быть от 0 до 99,99.")
    for field in ("withdraw_usdt", "usdt_per_usd"):
        if number(cfg[field]) > 100000:
            raise PluginError("Сумма или курс слишком велики.")
    rates = cfg["manual_rates"]
    if not isinstance(rates, dict) or set(rates) - LOT_CURRENCIES:
        raise PluginError("Ручные курсы задаются в RUB, USD или EUR за 1 USDT.")
    for rate in rates.values():
        if number(rate) > 1000000:
            raise PluginError("Ручной курс слишком велик.")
    if not isinstance(cfg["manual_lots"], list) or any(
            not isinstance(lid, str) or not lid.isdigit() for lid in cfg["manual_lots"]):
        raise PluginError("Неверный список лотов с ручной ценой.")
    return cfg


def validate_rule(rule):
    r = copy.deepcopy(rule)
    if not isinstance(r, dict) or r.get("kind") not in KINDS or not isinstance(r.get("params"), dict):
        raise PluginError("Неизвестный тип товара или параметры.")
    kind, p = r["kind"], r["params"]
    r["max_cost"] = str(usd(r.get("max_cost")))
    r["max_units"] = integer(r.get("max_units", 1), STEAM_MAX_UNITS if kind == "steamTopup" else 100)
    r["quantity"] = integer(r.get("quantity", 1), 100)
    if type(r.get("enabled", True)) is not bool:
        raise PluginError("Неверный переключатель лота.")
    if kind not in {"giftcards", "gamekeys"} and r["quantity"] != 1:
        raise PluginError("Для этого типа товара quantity должен быть равен 1.")
    if kind not in {"giftcards", "gamekeys", "steamTopup"} and r["max_units"] != 1:
        raise PluginError("Для этого типа товара разрешена одна штука лота за заказ.")
    fields = {"gamekeys": {"game_id", "key_id"}, "giftcards": {"category_id", "card_id"},
              "topups": {"category_id", "offer_id", "fields"},
              "manual": {"manual_service_id", "product_id", "fields"},
              "steamTopup": {"steamLogin", "currency", "amount"},
              "steamGifts": {"app_id", "sub_id", "region", "invite_url"},
              "telegramStars": {"telegram_username", "quantity"},
              "telegramPremium": {"telegram_username", "months"}}[kind]
    if set(p) - fields:
        raise PluginError("Параметры не соответствуют выбранному типу товара.")
    for key in fields & {"category_id", "offer_id", "manual_service_id", "product_id", "game_id", "key_id", "region"}:
        if not isinstance(p.get(key), str) or not p[key].strip() or len(p[key]) > 200:
            raise PluginError(f"Неверный идентификатор {key}.")
    if kind == "steamGifts":
        p["app_id"] = integer(p.get("app_id"), 10**12)
        p["sub_id"] = integer(p.get("sub_id"), 10**12)
    if kind == "steamTopup":
        amount = number(p.get("amount"))
        if amount > STEAM_MAX_AMOUNT or p.get("currency") not in {"USD", "RUB", "UAH", "KZT"}:
            raise PluginError("Проверьте сумму и валюту Steam.")
        p["amount"] = float(amount)
        if p["amount"] == 0:
            raise PluginError("Сумма Steam слишком мала.")
        limit = number(r.get("max_topup_amount", "0"), zero=True)
        if limit > STEAM_MAX_AMOUNT:
            raise PluginError(f"Лимит суммы пополнения должен быть не больше {STEAM_MAX_AMOUNT:f}.")
        if limit and limit < amount:
            raise PluginError("Лимит пополнения должен быть не меньше суммы за одну штуку лота.")
        r["max_topup_amount"] = str(limit)
    if kind == "telegramStars":
        p["quantity"] = integer(p.get("quantity"), 10000)
        if p["quantity"] < 50:
            raise PluginError("Минимум 50 Stars.")
    if kind == "telegramPremium":
        p["months"] = integer(p.get("months"), 12)
        if p["months"] not in {3, 6, 12}:
            raise PluginError("Premium: 3, 6 или 12 месяцев.")
    if kind not in {"steamTopup", "steamGifts"}:
        catalog = r.get("catalog")
        if not isinstance(catalog, dict) or not all(isinstance(catalog.get(k), str) and catalog[k]
                                                   for k in ("product_id", "category_id")):
            raise PluginError("Нужны product_id и category_id выбранной позиции каталога.")
    if "fields" in p and (not isinstance(p["fields"], dict) or any(not isinstance(v, str) for v in p["fields"].values())):
        raise PluginError("fields должен быть объектом со строковыми значениями.")
    inputs = r.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 10:
        raise PluginError("Допускается до 10 полей покупателя.")
    expected = {"steamTopup": "steamLogin", "steamGifts": "invite_url",
                "telegramStars": "telegram_username", "telegramPremium": "telegram_username"}.get(kind)
    paths = []
    for field in inputs:
        if (not isinstance(field, dict) or not isinstance(field.get("path"), str)
                or not re.fullmatch(r"(?:fields\.)?[A-Za-z0-9_-]{1,64}", field["path"])):
            raise PluginError("Неверное поле покупателя.")
        key = field["path"]
        if re.search(r"password|passwd|secret|token|cookie|otp|2fa", key, re.I):
            raise PluginError("Услуги с паролями, токенами и кодами входа не поддерживаются.")
        if key in paths or (expected and key != expected) or (not expected and
                (kind not in {"topups", "manual"} or not key.startswith("fields."))):
            raise PluginError("Поле покупателя не соответствует виду товара.")
        paths.append(key)
    if expected and paths != [expected]:
        raise PluginError("Получатель должен запрашиваться у покупателя.")
    r["inputs"] = inputs
    return r


def recipient_value(field, text):
    value = str(text).strip()
    if not value or len(value) > 300 or any(ord(c) < 32 for c in value):
        raise PluginError("Пустое или слишком длинное значение.")
    key = field["path"]
    if key == "steamLogin" and not re.fullmatch(r"[A-Za-z0-9_]{2,64}", value):
        raise PluginError("Нужен логин Steam, не ник или ссылка.")
    if key == "telegram_username":
        value = value.lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", value):
            raise PluginError("Нужен username Telegram.")
    if key == "invite_url":
        url = urlparse(value)
        if (url.scheme != "https" or url.netloc not in {"s.team", "steamcommunity.com"}
                or url.fragment or not url.path.startswith(("/p/", "/id/", "/profiles/", "/user/", "/f/"))):
            raise PluginError("Нужна HTTPS-ссылка профиля или приглашения Steam.")
    if field.get("choices") and value not in [str(v) for v in field["choices"]]:
        raise PluginError("Выберите: " + ", ".join(map(str, field["choices"])))
    return value


def steam_amount(rule, units):
    p = rule["params"]
    amount = number(p["amount"]) * units
    limit = number(rule.get("max_topup_amount", "0"), zero=True) or STEAM_MAX_AMOUNT
    if amount > limit:
        raise OrderRejection("quantity", f"Пополнение {amount.normalize():f} {p['currency']} "
                             f"выше лимита {limit.normalize():f} {p['currency']} на заказ.")
    return amount


def order_limits(rule):
    units = rule["max_units"]
    text = (f"Лимит штук на заказ: {units}\n" if units != STEAM_MAX_UNITS
            else "Количество берётся из заказа FunPay.\n")
    if rule["kind"] == "steamTopup":
        limit = number(rule.get("max_topup_amount", "0"), zero=True)
        if limit:
            text += f"Лимит суммы пополнения: {limit.normalize():f} {rule['params']['currency']} за заказ\n"
        else:
            text += "Лимит суммы пополнения: по правилам GameAU\n"
    return text


def make_payload(rule, units, values):
    r = validate_rule(rule)
    units = integer(units, r["max_units"])
    p = copy.deepcopy(r["params"])
    for field in r["inputs"]:
        value = recipient_value(field, values.get(field["path"], ""))
        if field["path"].startswith("fields."):
            p.setdefault("fields", {})[field["path"].split(".", 1)[1]] = value
        else:
            p[field["path"]] = value
    if r["kind"] in {"gamekeys", "giftcards"}:
        p["quantity"] = integer(r["quantity"] * units, 100)
    elif r["kind"] == "steamTopup":
        p["amount"] = float(steam_amount(r, units))
    return p


class Config:
    def __init__(self, root):
        self.path = Path(root) / "config.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        data = copy.deepcopy(DEFAULT)
        if self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict):
                raise PluginError("Повреждён config.json.")
            data.update(saved)
        data["pricing"] = price_settings(data.get("pricing", {}))
        self.save(data)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, data):
        price_settings(data.get("pricing", {}))
        for key in ("enabled", "dry_run", "live_confirmed", "guard_enabled", "auto_refund"):
            if type(data.get(key)) is not bool:
                raise PluginError("Неверные переключатели в настройках.")
        reasons = data.get("refund_reasons")
        if not isinstance(reasons, list) or any(not isinstance(r, str) or r not in REFUND_REASONS for r in reasons):
            raise PluginError("Неверные причины автовозврата.")
        number(data.get("min_balance"), zero=True)
        number(data.get("guard_threshold"), zero=True)
        if not isinstance(data.get("api_key"), str) or not isinstance(data.get("lots"), dict):
            raise PluginError("Неверный формат настроек.")
        for lot, rule in data["lots"].items():
            integer(lot, 10**15)
            validate_rule(rule)
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, self.path)
            self.data = copy.deepcopy(data)


class Ledger:
    def __init__(self, root):
        self.path = Path(root) / "orders.db"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, state TEXT, body TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS holds (id TEXT PRIMARY KEY, owned INTEGER NOT NULL)")
        interrupted = {"creating": "uncertain", "delivering": "delivery_review", "refunding": "refund_review"}
        for row in self.rows():
            if row["state"] in interrupted:
                row["state"] = interrupted[row["state"]]
                self.put(row)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def add(self, row):
        with self.connect() as db:
            return db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?)",
                              (row["id"], row["state"], json.dumps(row))).rowcount == 1

    def put(self, row):
        row["updated"] = time.time()
        with self.connect() as db:
            db.execute("UPDATE orders SET state=?,body=? WHERE id=?",
                       (row["state"], json.dumps(row, ensure_ascii=False), row["id"]))

    def get(self, oid):
        with self.connect() as db:
            result = db.execute("SELECT body FROM orders WHERE id=?", (str(oid),)).fetchone()
        return json.loads(result[0]) if result else None

    def rows(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT body FROM orders ORDER BY rowid DESC")]

    def holds(self):
        with self.connect() as db:
            return {str(key): bool(owned) for key, owned in db.execute("SELECT id,owned FROM holds")}

    def hold(self, lot_id, owned):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO holds VALUES (?,?)", (str(lot_id), int(owned)))

    def release_hold(self, lot_id):
        with self.connect() as db:
            db.execute("DELETE FROM holds WHERE id=?", (str(lot_id),))


class InstanceLock:
    def __init__(self, root):
        Path(root).mkdir(parents=True, exist_ok=True)
        self.stream = (Path(root) / "instance.lock").open("a+b")
        self.stream.seek(0)
        self.stream.write(b"0")
        self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise PluginError("Это хранилище уже использует другой процесс.") from None

    def close(self):
        self.stream.close()


class API:
    def __init__(self, config, session=None):
        self.config = config
        self.session = session or requests.Session()
        self.next_call = 0.0
        self.cooldown = 0.0

    def key(self):
        key = os.environ.get("GAMEAU_API_KEY") or self.config.snapshot()["api_key"]
        if not key or any(c.isspace() for c in key):
            raise PluginError("Укажите API-ключ в /gameau → API-ключ.")
        return key

    def call(self, endpoint, payload=None, params=None, idempotency_key=None):
        key = self.key()
        if time.monotonic() < self.cooldown:
            raise APIError("Лимит GameAU: дождитесь окончания паузы.", 429)
        time.sleep(max(0, self.next_call - time.monotonic()))
        self.next_call = time.monotonic() + 2.1
        headers = {"Authorization": "Bearer " + key}
        if endpoint.startswith("/orders/") and payload is not None:
            if not idempotency_key or str(uuid.UUID(idempotency_key)) != idempotency_key:
                raise PluginError("Для покупки нужен сохранённый UUID операции.")
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = self.session.request("GET" if payload is None else "POST", BASE_URL + endpoint,
                                     params=params, json=payload, headers=headers,
                                     timeout=(10, 40), allow_redirects=False)
        except requests.RequestException:
            raise APIError("Сеть/таймаут GameAU. Результат покупки может быть неизвестен.") from None
        if r.status_code == 429:
            try:
                delay = max(60, float(r.headers.get("Retry-After", 60)),
                            float(r.json().get("retry_after_seconds", 60)))
                if not 0 < delay <= 86400:
                    delay = 3600
            except (TypeError, ValueError, AttributeError):
                delay = 60
            self.cooldown = time.monotonic() + delay
        if r.status_code not in (200, 201):
            try:
                error = r.json()
            except ValueError:
                error = {}
            known = {"PRICE_CHANGED", "INSUFFICIENT_BALANCE", "IDEMPOTENCY_CONFLICT", "ORDER_STATUS_UNKNOWN",
                     "PRODUCT_NOT_FOUND", "STEAM_LOGIN_UNAVAILABLE", "AMOUNT_TOO_LOW", "AMOUNT_TOO_HIGH",
                     "MAX_CHARGE_REQUIRED", "IDEMPOTENCY_KEY_REQUIRED", "INVALID_ORDER_DATA", "PRICE_OUTLIER"}
            code = error.get("code") if isinstance(error, dict) and error.get("ok") is False else None
            code = code if isinstance(code, str) and code in known else None
            raise APIError(f"GameAU HTTP {r.status_code}" + (f" · {code}" if code else "")
                           + ". Проверьте ключ, баланс и журнал заказов.", r.status_code, code)
        try:
            data = r.json()
        except ValueError:
            raise APIError("Некорректный JSON GameAU.") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise APIError("GameAU не подтвердил успешный ответ.")
        return data


def authorized(c, obj):
    return bool(c.telegram and obj and obj.chat.type == "private"
                and obj.from_user.id in c.telegram.authorized_users)


class BalanceGuard:
    """Deactivate mapped lots at a low balance; restoring is an explicit admin action."""
    def __init__(self, engine):
        self.e = engine
        self.gate = threading.RLock()
        self.original_save = None
        self.wrapped_save = None
        self.last_check = 0
        self.last_notice = None

    def enabled(self):
        plugin = self.e.c.plugins.get(UUID)
        return (not self.e.stopped and bool(plugin and plugin.enabled)
                and self.e.config.snapshot()["guard_enabled"])

    def install(self):
        if self.original_save is not None:
            return
        self.original_save = self.e.c.account.save_lot
        def save_lot(fields, *args, **kwargs):
            with self.gate:
                if (self.enabled() and not self.e.config.snapshot()["dry_run"] and fields.active
                        and str(fields.lot_id) in self.e.ledger.holds()):
                    raise PluginError("Лот снят контролем баланса GameAU. Верните его через /gameau → Контроль баланса.")
                return self.original_save(fields, *args, **kwargs)
        self.wrapped_save = save_lot
        self.e.c.account.save_lot = save_lot

    def close(self):
        if self.wrapped_save is not None and self.e.c.account.save_lot is self.wrapped_save:
            self.e.c.account.save_lot = self.original_save

    def notice(self, text):
        if text != self.last_notice:
            self.last_notice = text
            self.e.notify(text)

    def check(self, force=False):
        if not self.enabled() or (not force and time.time() - self.last_check < 60):
            return
        self.last_check = time.time()
        self.install()
        try:
            self.e.bind_account()
            cfg = self.e.config.snapshot()
            balance = self.e.account()
        except Exception:
            self.notice("Не удалось проверить баланс GameAU. По ошибке API новые лоты не снимаются; прежние запреты сохранены.")
            return
        threshold = number(cfg["guard_threshold"], zero=True)
        if balance > threshold:
            if self.e.ledger.holds():
                self.notice("Баланс GameAU снова выше порога. Для возврата лотов: Контроль баланса → Вернуть лоты.")
            else:
                self.last_notice = None
            return
        if cfg["dry_run"]:
            self.notice(f"Тест контроля баланса: {balance} ≤ {threshold} USD. Лоты на FunPay не изменены.")
            return
        changed, errors = [], []
        for lot_id in cfg["lots"]:
            if not self.enabled() or self.e.config.snapshot()["dry_run"]:
                return
            try:
                with self.gate:
                    owned = self.e.ledger.holds().get(lot_id, False)
                    self.e.ledger.hold(lot_id, owned)
                    fields = self.e.c.account.get_lot_fields(int(lot_id))
                    if fields.active:
                        if not self.enabled() or self.e.config.snapshot()["dry_run"]:
                            return
                        self.e.ledger.hold(lot_id, True)  # Persist ownership before an uncertain save.
                        fields.active = False
                        self.original_save(fields)
                        if self.e.c.account.get_lot_fields(int(lot_id)).active:
                            raise PluginError("FunPay не подтвердил снятие.")
                        changed.append(lot_id)
            except Exception:
                errors.append(lot_id)
        if changed or errors:
            self.notice("Контроль баланса GameAU. Сняты лоты: " + (", ".join(changed) or "—")
                        + ". Не удалось подтвердить снятие: " + (", ".join(errors) or "—") + ".")

    def restore(self):
        cfg = self.e.config.snapshot()
        if not self.enabled() or cfg["dry_run"]:
            raise PluginError("Для возврата включите контроль баланса и реальный режим.")
        self.install()
        self.e.bind_account()
        if self.e.account() <= number(cfg["guard_threshold"], zero=True):
            raise PluginError("Баланс GameAU ещё не выше порога.")
        restored, errors = [], []
        for lot_id, owned in self.e.ledger.holds().items():
            if not self.enabled() or self.e.config.snapshot()["dry_run"]:
                break
            try:
                if lot_id not in cfg["lots"]:
                    raise PluginError("Привязка удалена: проверьте лот вручную.")
                with self.gate:
                    if owned:
                        fields = self.e.c.account.get_lot_fields(int(lot_id))
                        if not fields.active:
                            fields.active = True
                            self.original_save(fields)
                        if not self.e.c.account.get_lot_fields(int(lot_id)).active:
                            raise PluginError("FunPay не подтвердил включение.")
                        restored.append(lot_id)
                    self.e.ledger.release_hold(lot_id)
            except Exception:
                errors.append(lot_id)
        self.last_notice = None
        return restored, errors


class Pricing:
    def __init__(self, engine):
        self.e = engine
        self.rates = {}
        self.checked = {}
        self.status = {}
        self.next_run = 0

    def lot_fields(self, lid):
        self.e.bind_account()
        fields = self.e.c.account.get_lot_fields(int(lid))
        if str(fields.lot_id) != lid or fields.currency.name not in LOT_CURRENCIES:
            raise PluginError("Не удалось определить лот или валюту FunPay.")
        return fields

    def rate(self, currency, cfg):
        if cfg["source"] == "manual":
            if currency.name not in cfg["manual_rates"]:
                raise PluginError(f"Задайте ручной курс USDT для {currency.name}.")
            return number(cfg["manual_rates"][currency.name]), None
        usdt = number(cfg["withdraw_usdt"])
        key = currency.name, usdt
        cached = self.rates.get(key)
        if cached and time.monotonic() - cached[0] < 120:
            return cached[1]
        account = self.e.c.account
        try:
            response = account.method(
                "post", "withdraw/calc", {"x-requested-with": "XMLHttpRequest", "accept": "application/json"},
                {"csrf_token": account.csrf_token, "preview": "1", "currency_id": currency.code,
                 "ext_currency_id": "usdt_trc", "wallet": "", "amount_ext": format(usdt, "f")},
                raise_not_200=True,
            ).json()
            if not isinstance(response, dict) or response.get("error"):
                raise ValueError
            amount = response.get("amount_int")
            if isinstance(amount, str):
                amount = re.sub(r"[ \u00a0\u202f]", "", amount).replace(",", ".")
            debit = number(amount)
        except Exception:
            raise PluginError(
                "FunPay не вернул расчёт вывода USDT TRC20. Проверьте доступность способа вывода "
                "и сумму расчёта либо выберите ручной курс."
            ) from None
        result = debit / usdt, (debit, usdt)
        self.rates[key] = time.monotonic(), result
        return result

    def cost(self, rule):
        kind = rule["kind"]
        if kind in {"steamTopup", "steamGifts"}:
            p = rule["params"]
            keys = ("amount", "currency") if kind == "steamTopup" else ("app_id", "sub_id", "region")
            try:
                data = self.e.api.call("/" + kind + "/quote", {key: p[key] for key in keys}).get("quote")
            except APIError as exc:
                if kind == "steamTopup" and exc.status == 422 and exc.code == "AMOUNT_TOO_LOW":
                    return self.steam_batch_cost(rule)
                raise
            if not isinstance(data, dict) or data.get("charge_currency", "USD") != "USD":
                raise PluginError("Неверная цена в расчёте GameAU.")
            price = usd(data.get("charge"))
        else:
            price = self.e.catalog_cost(rule, 1)
        if price > usd(rule["max_cost"]):
            raise PluginError("Закупка за одну штуку превысила лимит GameAU для заказа. Проверьте привязку.")
        return price, None

    def steam_batch_cost(self, rule):
        rule = validate_rule(rule)
        params = rule["params"]
        unit_amount, currency = number(params["amount"]), params["currency"]
        topup = self.e.catalog("steamTopup").get("steam_topup")
        if not isinstance(topup, dict) or topup.get("available") is not True or not isinstance(topup.get("rates"), dict):
            raise PluginError("GameAU не подтвердил доступность курса пополнения Steam.")
        rate = number(topup["rates"].get(currency))
        multiplier = number(topup.get("price_multiplier"))
        minimum = usd(topup.get("min_usd"))
        maximum = usd(topup.get("max_usd"))
        if minimum > maximum or multiplier > 1:
            raise PluginError("GameAU вернул некорректные условия пополнения Steam.")

        # Allow for the provider checking either the raw USD amount or the amount rounded up to cents.
        # Below this quantity even the rounded amount is below the documented minimum.
        lower_usd = minimum.quantize(STEAM_BILL_STEP, rounding=ROUND_CEILING) - STEAM_BILL_STEP
        amount_limit = number(rule.get("max_topup_amount", "0"), zero=True) or STEAM_MAX_AMOUNT
        total_limit = min(amount_limit, maximum * rate, rule["max_units"] * unit_amount)
        if total_limit <= lower_usd * rate:
            raise PluginError("В пределах настроенных лимитов нет суммы пополнения, которую принимает GameAU.")
        lower_units = max(2, int((lower_usd * rate) // unit_amount) + 1)
        maximum_units = int(total_limit // unit_amount)
        if lower_units > maximum_units:
            raise PluginError("В пределах настроенных лимитов нет суммы пополнения, которую принимает GameAU.")

        raw_minimum_units = (min(minimum * rate, total_limit) / unit_amount).to_integral_value(rounding=ROUND_CEILING)
        candidate_units = min(maximum_units, max(lower_units, int(raw_minimum_units)))
        for quote_units in dict.fromkeys((lower_units, candidate_units)):
            quote_amount = steam_amount(rule, quote_units)
            try:
                result = self.e.api.call("/steamTopup/quote", {"amount": float(quote_amount), "currency": currency})
            except APIError as exc:
                if exc.status == 422 and exc.code == "AMOUNT_TOO_LOW":
                    continue
                raise
            break
        else:
            raise PluginError("GameAU не принял расчёт суммы пополнения. Проверьте лимиты лота и условия GameAU.")

        data = result.get("quote")
        if (not isinstance(data, dict) or data.get("charge_currency", "USD") != "USD"
                or data.get("currency", currency) != currency or number(data.get("amount", quote_amount)) != quote_amount
                or number(data.get("rate", rate)) != rate):
            raise PluginError("Котировка пополнения Steam изменилась. Повторите расчёт.")

        def charge(units):
            billed = (unit_amount * units / rate).quantize(STEAM_BILL_STEP, rounding=ROUND_CEILING)
            return (billed * multiplier).quantize(STEAM_CHARGE_STEP, rounding=ROUND_CEILING)

        quoted = usd(data.get("charge"))
        if quoted != charge(quote_units):
            raise PluginError("Котировка GameAU не совпала с условиями тарифа. Повторите расчёт.")
        if quoted > usd(rule["max_cost"]):
            raise PluginError("Расчёт допустимого пополнения превышает лимит списания USD за заказ.")

        # Check the smallest baskets where rounding costs most per unit. For larger orders,
        # the first rounding adds less than a cent and the second less than 0.0001 USD.
        last_checked = min(maximum_units, lower_units + 63)
        price = max(charge(units) / units for units in range(lower_units, last_checked + 1))
        if last_checked < maximum_units:
            tail_cost = unit_amount / rate * multiplier
            tail_cost += (STEAM_BILL_STEP * multiplier + STEAM_CHARGE_STEP) / (last_checked + 1)
            price = max(price, tail_cost)
        return price, {"units": quote_units, "amount": quote_amount, "currency": currency, "charge": quoted}

    def calculate(self, lid, fields=None):
        fields = fields or self.lot_fields(lid)
        config = self.e.config.snapshot()
        rule = config["lots"].get(lid)
        if not rule:
            raise PluginError("Сначала привяжите лот к товару GameAU.")
        cfg = config["pricing"]
        price_usd, steam_basis = self.cost(rule)
        rate, withdrawal = self.rate(fields.currency, cfg)
        needed_usdt = price_usd * number(cfg["usdt_per_usd"])
        cost = needed_usdt * rate
        fee = number(cfg["extra_percent"], zero=True) / 100
        margin = number(cfg["margin"], zero=True) / 100
        target = (cost / ((1 - fee) * (1 - margin))).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
        if target >= 10**8:
            raise PluginError("Расчётная цена выходит за допустимый диапазон.")
        net = target * (1 - fee)
        current = number(fields.price)
        old_net = current * (1 - fee)
        return {"lot": lid, "currency": fields.currency, "current": current, "target": target, "cost": cost,
                "price_usd": price_usd, "rate": rate, "usdt": needed_usdt, "withdrawal": withdrawal,
                "fee": target - net, "net": net, "profit": net - cost, "margin": (net - cost) / net * 100,
                "old_margin": (old_net - cost) / old_net * 100, "desired": cfg["margin"],
                "old_methods": getattr(getattr(fields, "calc_result", None), "methods", []),
                "methods": self.buyer_prices(fields, target), "time": time.monotonic(),
                "settings": cfg, "rule": rule, "key": fingerprint(self.e.api.key()), "steam_basis": steam_basis}

    def manual_preview(self, lid, price):
        fields = self.lot_fields(lid)
        if price >= 10**8 or price.as_tuple().exponent < -2:
            raise PluginError("Укажите цену меньше 100000000, до двух знаков после точки.")
        return {"lot": lid, "currency": fields.currency, "current": number(fields.price),
                "target": price, "methods": self.buyer_prices(fields, price), "time": time.monotonic()}

    def buyer_prices(self, fields, target):
        category = fields.subcategory
        try:
            result = self.e.c.account.calc(category.type, subcategory_id=category.id, price=float(target))
        except Exception:
            raise PluginError("FunPay не рассчитал цену для покупателя в этой категории.") from None
        if result.account_currency != fields.currency or not result.methods:
            raise PluginError("FunPay не подтвердил валюту или способы оплаты.")
        return result.methods

    def can_write(self, auto):
        cfg = self.e.config.snapshot()
        plugin = self.e.c.plugins.get(UUID)
        if self.e.stopped or not (plugin and plugin.enabled) or cfg["dry_run"]:
            raise PluginError("Для сохранения цены включите плагин Cardinal и реальный режим GameAU.")
        if auto and (not self.e.active() or not cfg["pricing"]["enabled"]):
            raise PluginError("Автообновление цен приостановлено.")
        return cfg

    def apply(self, preview, auto=False, manual=False):
        lid = preview["lot"]
        if time.monotonic() - preview["time"] > 120:
            raise PluginError("Расчёт устарел. Откройте его заново.")
        self.can_write(auto)
        with self.e.guard.gate:
            fields = self.lot_fields(lid)
            cfg = self.can_write(auto)
            if lid not in cfg["lots"] or fields.currency != preview["currency"]:
                raise PluginError("Лот или его валюта изменились. Пересчитайте цену.")
            if not manual and (cfg["pricing"] != preview["settings"] or cfg["lots"][lid] != preview["rule"]
                               or fingerprint(self.e.api.key()) != preview["key"]):
                raise PluginError("Настройки расчёта изменились. Откройте расчёт заново.")
            if auto and (not fields.active or fields.auto_delivery or lid in cfg["pricing"]["manual_lots"]
                         or not cfg["lots"][lid].get("enabled", True)):
                return False
            if number(fields.price) != preview["current"]:
                raise PluginError("Цена лота изменилась. Откройте новый расчёт.")
            if manual and lid not in cfg["pricing"]["manual_lots"]:
                cfg["pricing"]["manual_lots"].append(lid)
                self.e.config.save(cfg)
            if preview["target"] == preview["current"]:
                return False
            fields.price = float(preview["target"])
            try:
                self.e.c.account.save_lot(fields)
                saved = self.lot_fields(lid)
            except Exception:
                raise PluginError("FunPay не подтвердил сохранение. Проверьте текущую цену лота.") from None
            if saved.currency != preview["currency"] or number(saved.price) != preview["target"]:
                raise PluginError("Сохранённая цена отличается от расчёта. Проверьте лот на FunPay.")
            return True

    def tick(self):
        cfg = self.e.config.snapshot()
        pricing = cfg["pricing"]
        now = time.monotonic()
        if not pricing["enabled"] or cfg["dry_run"] or now < self.next_run:
            return
        candidates = [lid for lid, rule in cfg["lots"].items() if rule.get("enabled", True)
                      and lid not in pricing["manual_lots"] and now >= self.checked.get(lid, 0)]
        if not candidates:
            return
        lid = min(candidates, key=lambda lot_id: self.checked.get(lot_id, 0))
        self.checked[lid] = now + PRICE_INTERVAL
        self.next_run = now + 5
        try:
            fields = self.lot_fields(lid)
            if not fields.active or fields.auto_delivery:
                return
            preview = self.calculate(lid, fields)
            changed = self.apply(preview, auto=True)
            self.status[lid] = (time.time(), f"{preview['target']:.2f} {fields.currency.name}"
                                + (" · обновлено" if changed else " · без изменений"))
        except Exception as exc:
            error = str(exc) if isinstance(exc, PluginError) else "Не удалось получить данные для расчёта."
            if self.status.get(lid, (0, ""))[1] != error:
                self.e.notify(f"Автоцены GameAU, лот #{lid}. {error}")
            self.status[lid] = time.time(), error


class Engine:
    def __init__(self, cardinal, root=ROOT, api=None):
        self.c = cardinal
        self.instance = InstanceLock(root)
        try:
            self.config = Config(root)
            self.ledger = Ledger(root)
        except Exception:
            self.instance.close()
            raise
        self.api = api or API(self.config)
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="GameAU")
        self.stopped = False
        self.paused = threading.Event()
        self.exit = threading.Event()
        self.tick_pending = False
        self.tick_lock = threading.Lock()
        self.thread = None
        self.lot_cache = {}
        self.panel = Panel(self)
        self.guard = BalanceGuard(self)
        self.pricing = Pricing(self)

    def active(self):
        plugin = self.c.plugins.get(UUID)
        return (not self.stopped and not self.paused.is_set() and bool(plugin and plugin.enabled)
                and self.config.snapshot()["enabled"])

    def bind_account(self):
        cfg = self.config.snapshot()
        owner = str(self.c.account.id)
        if not owner.isdigit() or int(owner) <= 0:
            raise PluginError("FunPay ещё не авторизован.")
        if cfg["account_id"] is None:
            cfg["account_id"] = owner
            self.config.save(cfg)
        elif str(cfg["account_id"]) != owner:
            raise PluginError("Хранилище принадлежит другому аккаунту FunPay.")

    def submit(self, fn, *args):
        if self.stopped:
            return
        def run():
            with self.lock:
                if self.stopped:
                    return
                try:
                    fn(*args)
                except PluginError as exc:
                    self.notify(str(exc))
                except Exception:
                    LOG.error("GameAU: ошибка задачи; проверьте журнал /gameau. Секреты не записываются.")
                    self.notify("Ошибка задачи. Проверьте журнал /gameau; автоматической повторной покупки нет.")
        return self.pool.submit(run)

    def notify(self, text):
        if self.c.telegram:
            try:
                self.c.telegram.send_notification("<b>GameAU</b>\n" + html.escape(text))
            except Exception:
                LOG.warning("GameAU: уведомление не отправлено.")

    def tell(self, row, text):
        try:
            return bool(self.c.send_message(row["chat_id"], text, row["buyer_name"]))
        except Exception:
            return False

    def phase(self, row, state, **changes):
        row.update(state=state, **changes)
        self.ledger.put(row)

    def refresh_lots(self):
        fresh = self.c.account.get_user(self.c.account.id)
        if fresh.id != self.c.account.id or self.c.profile is None:
            raise PluginError("Не удалось загрузить профиль FunPay.")
        lots = fresh.get_sorted_lots(1)
        if not isinstance(lots, dict):
            raise PluginError("Неверный список лотов FunPay.")
        for lot in lots.values():
            self.c.profile.update_lot(lot)
        self.lot_cache = dict(lots)
        return lots

    def verify_order(self, row, statuses=("PAID",)):
        self.bind_account()
        o = self.c.account.get_order(row["id"])
        if (o.id != row["id"] or getattr(o.status, "name", "") not in statuses or o.seller_id != self.c.account.id
                or o.buyer_id != row["buyer_id"] or str(o.chat_id) != str(row["chat_id"])
                or o.short_description != row["description"] or o.amount != row["units"]
                or getattr(o, "order_secrets", None)):
            raise PluginError("Заказ FunPay не оплачен, изменён, возвращён или уже выдан самим FunPay.")
        if getattr(self.c, "bl_delivery_enabled", False) and row["buyer_name"] in self.c.blacklist:
            raise PluginError("Покупатель в чёрном списке Cardinal.")
        return o

    def new_order(self, event):
        if not self.active() or getattr(self.c, "old_mode_enabled", False):
            return
        shortcut = event.order
        if not re.fullmatch(r"[A-Z0-9]+", shortcut.id) or shortcut.id == "ADTEST" or self.ledger.get(shortcut.id):
            return
        lid = str(getattr(event, "lot_id", ""))
        cfg = self.config.snapshot()
        rule = cfg["lots"].get(lid)
        if not rule or not rule.get("enabled", True):
            return
        native = getattr(event, "config_section_obj", None)
        if getattr(event, "delivered", False) or (native is not None and not native.getboolean("disable")):
            self.notify(f"#{shortcut.id}: отключите встроенную автовыдачу Cardinal у лота {lid}.")
            return
        try:
            lot = getattr(event, "lot_shortcut", None)
            o = self.c.account.get_order(shortcut.id)
            if not lot or getattr(lot, "auto", False) or o.id != shortcut.id or o.buyer_id != shortcut.buyer_id:
                raise PluginError("Не удалось проверить лот или включена автовыдача FunPay.")
            candidates = [candidate for candidate in self.c.profile.get_sorted_lots(1).values()
                          if candidate.description == o.short_description
                          and getattr(candidate.subcategory, "id", None) == getattr(o.subcategory, "id", None)
                          and (candidate.server or "") == (getattr(o.server, "name", None) or "")
                          and (candidate.side or "") == (getattr(o.side, "name", None) or "")]
            if len(candidates) != 1 or str(candidates[0].id) != lid:
                raise PluginError("Неоднозначный лот. Используйте уникальные названия в категории/сервере.")
            rule = validate_rule(rule)
            units = integer(o.amount, 10**15)
            if units != o.amount:
                raise PluginError("FunPay не подтвердил количество в заказе.")
            row = {"id": o.id, "state": "collecting", "lot_id": lid, "rule": rule,
                   "units": units, "description": o.short_description, "buyer_id": o.buyer_id,
                   "buyer_name": o.buyer_username, "chat_id": o.chat_id, "values": {},
                   "created": time.time(), "dry_run": cfg["dry_run"], "key_hash": fingerprint(self.api.key()),
                   "idempotency_key": str(uuid.uuid4()),
                   "last_poll": 0, "error": ""}
            self.verify_order(row)
            if not self.ledger.add(row):
                return
            if units > rule["max_units"]:
                self.reject(row, OrderRejection("quantity",
                            f"Куплено {units} шт. Принимается до {rule['max_units']} шт. за заказ."))
                return
            if rule["kind"] == "steamTopup":
                try:
                    steam_amount(rule, units)
                except OrderRejection as exc:
                    self.reject(row, exc)
                    return
            if rule["inputs"]:
                self.ask(row)
            else:
                self.phase(row, "ready")
                self.process(row)
        except PluginError as exc:
            self.notify(f"#{shortcut.id}: {exc}")

    def ask(self, row):
        missing = [f for f in row["rule"]["inputs"] if f["path"] not in row["values"]]
        if missing:
            field = missing[0]
            title = field.get("label") or NAMES.get(field["path"], field["path"])
            if field.get("choices"):
                title += " (варианты: " + ", ".join(map(str, field["choices"])) + ")"
            self.tell(row, f"Заказ #{row['id']}. Укажите: {title}.\n"
                      f"Отправьте: !gameau {row['id']} ЗНАЧЕНИЕ\nПароль от аккаунта не нужен.")
        else:
            self.phase(row, "confirm", nonce=secrets.token_hex(3), confirm_until=time.time() + 900)
            info = "; ".join(row["values"].values())
            summary = ""
            if row["rule"]["kind"] == "steamTopup":
                params = row["rule"]["params"]
                amount = steam_amount(row["rule"], row["units"])
                summary = f"Пополнение Steam: {amount.normalize():f} {params['currency']} за {row['units']} шт.\n"
            self.tell(row, f"Заказ #{row['id']}. Проверьте получателя: {info}.\n"
                      f"{summary}"
                      f"Подтвердите: !gameau {row['id']} да {row['nonce']}\n"
                      f"Для исправления: !gameau {row['id']} заново")

    def message(self, msg):
        if not self.active() or getattr(msg, "by_bot", False) or getattr(msg, "badge", None):
            return
        parts = (getattr(msg, "text", None) or "").strip().split(maxsplit=2)
        if len(parts) != 3 or parts[0].lower() != "!gameau":
            return
        row = self.ledger.get(parts[1].lstrip("#").upper())
        if (not row or row["state"] not in {"collecting", "confirm"}
                or msg.author_id != row["buyer_id"] or str(msg.chat_id) != str(row["chat_id"])):
            return
        try:
            self.verify_order(row)
            value = parts[2].strip()
            if value.lower() == "заново":
                self.phase(row, "collecting", values={})
                self.ask(row)
            elif row["state"] == "confirm":
                if value != "да " + row["nonce"] or time.time() > row["confirm_until"]:
                    raise PluginError("Неверное/устаревшее подтверждение. Отправьте !gameau НОМЕР_ЗАКАЗА заново.")
                self.phase(row, "ready")
                self.process(row)
            else:
                field = next(f for f in row["rule"]["inputs"] if f["path"] not in row["values"])
                row["values"][field["path"]] = recipient_value(field, value)
                self.ledger.put(row)
                self.ask(row)
        except PluginError as exc:
            self.tell(row, str(exc))

    def same_key(self, row):
        if fingerprint(self.api.key()) != row["key_hash"]:
            raise PluginError("API-ключ изменился. Верните прежний ключ для обработки этого заказа.")

    def account(self):
        account = self.api.call("/account").get("account")
        if not isinstance(account, dict) or account.get("status") != "active" or account.get("currency") != "USD":
            raise PluginError("GameAU не подтвердил активный аккаунт с балансом USD.")
        return number(account.get("balance"), zero=True)

    def catalog(self, kind, search="", offset=0, limit=100):
        data = self.api.call("/catalog", params={"type": kind, "q": search[:100], "limit": limit, "offset": offset})
        catalog = data.get("catalog")
        if not isinstance(catalog, dict) or catalog.get("currency") != "USD" or not isinstance(catalog.get("items"), list):
            raise PluginError("Неверный ответ каталога GameAU.")
        if catalog.get("stale") is not False:
            raise PluginError("Каталог GameAU устарел или его актуальность не подтверждена. Попробуйте позже.")
        return catalog

    def current_item(self, rule):
        saved = rule["catalog"]
        offset, found = 0, []
        for _ in range(20):
            catalog = self.catalog(rule["kind"], saved["product_id"], offset)
            found += [item for item in catalog["items"] if isinstance(item, dict)
                      and item.get("kind") == rule["kind"]
                      and item.get("product_id") == saved["product_id"]
                      and item.get("category_id") == saved["category_id"]
                      and item.get("region_code") == saved.get("region_code")]
            nxt = catalog.get("next_offset")
            if nxt is None:
                break
            if type(nxt) is not int or nxt <= offset:
                raise PluginError("Некорректная пагинация каталога.")
            offset = nxt
        else:
            raise PluginError("Поиск товара слишком широкий. Нужна ручная сверка каталога.")
        if len(found) != 1:
            raise PluginError("Товар не найден однозначно в свежем каталоге GameAU.")
        return found[0]

    def prepare(self, row):
        rule = row["rule"]
        kind = rule["kind"]
        payload = make_payload(rule, row["units"], row["values"])
        if kind in {"steamTopup", "steamGifts"}:
            try:
                data = self.api.call("/" + kind + "/quote", payload).get("quote")
            except APIError as exc:
                if kind == "steamTopup" and exc.status == 422 and exc.code == "STEAM_LOGIN_UNAVAILABLE":
                    raise OrderRejection("recipient", "GameAU отклонил пополнение этого логина Steam.") from None
                if kind == "steamTopup" and exc.status == 422 and exc.code in {"AMOUNT_TOO_LOW", "AMOUNT_TOO_HIGH"}:
                    raise OrderRejection("quantity", "Сумма пополнения вне допустимого диапазона GameAU.") from None
                raise
            if not isinstance(data, dict) or data.get("charge_currency", "USD") != "USD":
                raise PluginError("Не удалось проверить расчёт GameAU.")
            if kind == "steamTopup" and any(key in data and data[key] != payload[key]
                                            for key in ("steamLogin", "currency", "amount")):
                raise PluginError("GameAU вернул расчёт для других реквизитов.")
            if kind == "steamTopup" and data.get("can_refill") is False:
                raise OrderRejection("recipient", "GameAU отклонил пополнение этого логина Steam.")
            if kind == "steamTopup" and data.get("can_refill") is not True:
                raise PluginError("GameAU не подтвердил возможность пополнения этого логина Steam.")
            order = data.get("order") or {}
            offered = copy.deepcopy(order.get("body"))
            if not isinstance(offered, dict) or order.get("endpoint") != "/api/v1/orders/" + kind:
                raise PluginError("Неверный шаблон расчёта GameAU.")
            quote_limit = usd(offered.pop("maxCharge", None))
            total = usd(data.get("charge"))
            if offered != payload or total != quote_limit:
                raise PluginError("Расчёт изменил товар, получателя или сумму. Покупка остановлена.")
        else:
            total = self.catalog_cost(rule, row["units"])
        maximum = usd(rule["max_cost"])
        if total > maximum:
            raise OrderRejection("price_limit", f"Закупка {total:f} USD выше лимита {maximum:f} USD.")
        balance = self.account()
        reserve = number(self.config.snapshot()["min_balance"], zero=True)
        available = balance - reserve
        if available < total:
            raise OrderRejection("balance", "Недостаточно баланса или будет нарушен минимальный остаток.")
        # maxCharge is enforced by GameAU. Never send more than the configured ceiling or available balance.
        ceiling = min(maximum, available, total) if kind in {"steamTopup", "steamGifts"} else min(maximum, available)
        ceiling = ceiling.quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
        payload["maxCharge"] = float(usd(ceiling))
        return payload, total

    def catalog_cost(self, rule, units):
        item = self.current_item(rule)
        fresh = catalog_rule(item)
        if (fresh["params"] != rule["params"] or fresh["quantity"] != rule.get("quantity", 1)
                or [f["path"] for f in fresh["inputs"]] != [f["path"] for f in rule["inputs"]]):
            raise PluginError("Шаблон товара или поля получателя изменились. Проверьте привязку.")
        kind = rule["kind"]
        count = rule.get("quantity", 1) * units if kind in {"gamekeys", "giftcards"} else 1
        if kind == "telegramStars":
            count = rule["params"]["quantity"]
        minimum = integer(item.get("min_quantity"), 10000)
        maximum = integer(item.get("max_quantity"), 10000)
        if minimum > maximum:
            raise PluginError("GameAU вернул неверный диапазон количества.")
        if not minimum <= count <= maximum:
            raise OrderRejection("quantity", "Количество не соответствует текущим условиям GameAU.")
        stock = item.get("stock")
        if stock is not None:
            if type(stock) is not int or stock < 0:
                raise PluginError("GameAU вернул неверный остаток товара.")
            if stock < count:
                raise OrderRejection("stock", "Недостаточно товара у GameAU.")
        return number(item.get("price")) * units

    def refund_allowed(self, row, reason):
        cfg = self.config.snapshot()
        binding = cfg["lots"].get(row["lot_id"])
        return (self.active() and cfg["live_confirmed"] and not cfg["dry_run"] and not row["dry_run"]
                and cfg["auto_refund"] and reason in cfg["refund_reasons"]
                and binding and binding.get("enabled", True) and validate_rule(binding) == validate_rule(row["rule"]))

    def reject(self, row, rejection, *, price_declined=False):
        unpurchased = (not row.get("remote_id") and
                       (price_declined or (row["state"] in {"ready", "collecting"} and "payload" not in row)))
        if not unpurchased:
            return
        self.phase(row, "purchase_rejected" if price_declined else "blocked",
                   error=str(rejection), refund_reason=rejection.reason)
        if self.refund_allowed(row, rejection.reason):
            self.auto_refund(row)
        else:
            self.notify(f"#{row['id']}: {rejection}")

    def auto_refund(self, row):
        if row.get("remote_id") or not (
                (row["state"] == "blocked" and "payload" not in row) or row["state"] == "purchase_rejected"):
            return
        try:
            self.same_key(row)
            lot = self.c.account.get_lot_fields(int(row["lot_id"]))
            if lot.auto_delivery:
                raise PluginError("У лота включена штатная автовыдача FunPay.")
            self.verify_order(row)
            if not self.refund_allowed(row, row["refund_reason"]):
                raise PluginError("Автовозврат выключен для этой причины.")
        except Exception as exc:
            error = str(exc) if isinstance(exc, PluginError) else "Не удалось повторно проверить заказ FunPay."
            self.notify(f"#{row['id']}: автовозврат не отправлен. {row['error']} {error}")
            return

        # Persist the request before calling FunPay so a restart cannot resume procurement.
        self.phase(row, "refunding")
        try:
            self.c.account.refund(row["id"])
        except Exception:
            # Even a network error may follow an accepted refund. Confirm the order status below.
            pass
        self.check_refund(row)

    def check_refund(self, row):
        if row["state"] not in {"refunding", "refund_review"}:
            return
        try:
            self.verify_order(row, statuses=("REFUNDED",))
        except Exception:
            self.phase(row, "refund_review")
            self.notify(f"#{row['id']}: FunPay пока не подтвердил полный возврат. Сверьте заказ в журнале /gameau.")
            return
        self.phase(row, "refunded")
        self.notify(f"#{row['id']}: FunPay подтвердил возврат. {row['error']}")

    def process(self, row):
        if row["state"] != "ready" or not self.active():
            return
        try:
            self.same_key(row)
            self.verify_order(row)
            payload, total = self.prepare(row)
            cfg = self.config.snapshot()
            row["quoted_usd"] = str(total)
            if row["dry_run"] or cfg["dry_run"]:
                self.phase(row, "dry_run")
                self.notify(f"#{row['id']}: тест пройден, расчёт {total} USD. Покупка НЕ выполнена.")
                return
            if not cfg["live_confirmed"]:
                raise PluginError("Включите реальный режим в /gameau.")
            self.verify_order(row)
            if not self.active():
                return
            first_attempt = "payload" not in row
            self.phase(row, "creating", payload=payload)
            self.create(row, first_attempt=first_attempt)
        except OrderRejection as exc:
            if row["state"] == "ready":
                self.reject(row, exc)
        except PluginError as exc:
            if row["state"] == "ready":
                self.phase(row, "blocked", error=str(exc))
                self.notify(f"#{row['id']}: {exc}")

    def validate_remote(self, order, row, expected_id=None):
        if (not isinstance(order, dict) or not isinstance(order.get("id"), str)
                or not re.fullmatch(r"ord-[0-9]+", order["id"]) or order.get("status") not in REMOTE_STATES
                or (expected_id is not None and order["id"] != expected_id)):
            raise PluginError("Не удалось подтвердить ID или статус заказа GameAU.")
        for name in ("kind", "type"):
            if name in order and order[name] != row["rule"]["kind"]:
                raise PluginError("Тип покупки не соответствует заказу.")
        request = order.get("request")
        if request is not None:
            if not isinstance(request, dict):
                raise PluginError("Неверный формат копии заказа.")
            # API may omit recipient fields from its safe request copy; validate all returned purchase fields.
            for key, value in request.items():
                if key in row["payload"] and value != row["payload"][key]:
                    raise PluginError("GameAU вернул другой товар или параметры покупки.")

    def create(self, row, first_attempt=False):
        if row["state"] not in {"creating", "uncertain"}:
            return
        try:
            self.same_key(row)
            self.verify_order(row)
            if not self.active() or self.config.snapshot()["dry_run"] or row["dry_run"]:
                raise PluginError("Новые покупки остановлены.")
            try:
                data = self.api.call("/orders/" + row["rule"]["kind"], copy.deepcopy(row["payload"]),
                                     idempotency_key=row["idempotency_key"])
            except APIError as exc:
                if (first_attempt and not row.get("remote_id") and exc.status == 409 and exc.code == "PRICE_CHANGED"
                        and self.refund_allowed(row, "price_limit")):
                    reason = OrderRejection("price_limit", "GameAU отклонил покупку из-за цены выше лимита списания.")
                    self.reject(row, reason, price_declined=True)
                    return
                raise
            order = data.get("order")
            self.validate_remote(order, row)
            self.phase(row, "pending", remote_id=order["id"], last_poll=0, error="")
            self.poll(row)
        except Exception as exc:
            error = str(exc) if isinstance(exc, PluginError) else "Не удалось разобрать ответ GameAU."
            if row["state"] in {"refunding", "refund_review", "refunded", "purchase_rejected"}:
                raise
            if not row.get("remote_id"):
                self.phase(row, "uncertain", error=error)
                self.notify(f"#{row['id']}: запрос покупки требует сверки в журнале /gameau. {error}")
            else:
                row["error"] = error
                self.ledger.put(row)

    def poll(self, row):
        self.same_key(row)
        row["last_poll"] = time.time()
        self.ledger.put(row)
        order = self.api.call("/orders/status/" + quote(row["remote_id"], safe="")).get("order")
        self.validate_remote(order, row, row["remote_id"])
        status = order["status"]
        if status in FAIL_STATES:
            self.phase(row, "failed", remote_status=status)
            self.notify(f"#{row['id']}: статус GameAU {status}. Проверьте баланс и заказ FunPay; возврат FunPay не выполнялся.")
        elif status in SUCCESS_STATES:
            charged = order.get("chargedAmount", order.get("amount"))
            cost = str(number(charged, zero=True)) if charged is not None else "не указан API"
            self.phase(row, "completed", remote_status=status, cost=cost,
                       codes=order.get("keys", []), cards=order.get("cards", []), filenames=order.get("filenames", []))
            if charged is not None and number(charged, zero=True) > number(row["payload"]["maxCharge"]):
                self.phase(row, "delivery_review", error="GameAU сообщил списание выше maxCharge. Нужна сверка.")
                self.notify(f"#{row['id']}: GameAU сообщил списание выше maxCharge. Проверьте кабинет.")
                return
            self.deliver(row)
        else:
            row.update(remote_status=status, error="")
            if time.time() - row["created"] > 1800 and not row.get("slow_notice"):
                row["slow_notice"] = True
                self.notify(f"#{row['id']}: GameAU обрабатывает заказ больше 30 минут. Новая покупка не создаётся.")
            self.ledger.put(row)


    def deliver(self, row):
        try:
            self.verify_order(row)
            if row.get("filenames"):
                raise PluginError("Заказ содержит файлы. Передайте их вручную из кабинета GameAU.")
            codes = row.get("codes")
            if not isinstance(codes, list) or any(not isinstance(s, str) for s in codes):
                raise PluginError("Неверный формат выданных кодов.")
            if codes:
                normalized = [code.strip() for code in codes]
                if not all(normalized) or len(set(normalized)) != len(normalized):
                    raise PluginError("Поставщик вернул пустые или повторяющиеся коды. Нужна сверка.")
                if row["rule"]["kind"] in {"giftcards", "gamekeys"}:
                    used = {code.strip() for other in self.ledger.rows() if other["id"] != row["id"]
                            for code in (other.get("codes") or []) if isinstance(code, str)}
                    if used.intersection(normalized):
                        raise PluginError("Этот код уже встречался в другом заказе. Автовыдача остановлена.")
            content = "\n".join(codes) if codes else ""
            if row.get("cards"):
                if not isinstance(row["cards"], list) or any(not isinstance(card, dict) or not card for card in row["cards"]):
                    raise PluginError("Неверный формат выданных карт.")
                serialized = [json.dumps(card, ensure_ascii=False, sort_keys=True) for card in row["cards"]]
                used = {json.dumps(card, ensure_ascii=False, sort_keys=True)
                        for other in self.ledger.rows() if other["id"] != row["id"]
                        for card in (other.get("cards") or []) if isinstance(card, dict)}
                if len(set(serialized)) != len(serialized) or used.intersection(serialized):
                    raise PluginError("Повторяющиеся карты. Нужна сверка поставщика.")
                content += ("\n" if content else "") + "\n".join(serialized)
            if row["rule"]["kind"] in {"giftcards", "gamekeys"}:
                count = max(len(codes), len(row.get("cards") or []))
                if count != row["payload"]["quantity"]:
                    raise PluginError("Число кодов/карт не совпадает с покупкой. Нужна ручная проверка.")
            if not content and row["rule"]["kind"] in {"giftcards", "gamekeys", "manual"}:
                raise PluginError("Поставщик завершил заказ без данных для выдачи. Нужна ручная проверка.")
            if not content:
                content = "Поставщик подтвердил выполнение заказа. Проверьте пополнение или получение подарка."
            if len(content) > 40000:
                raise PluginError("Слишком большой ответ для автоматической выдачи.")
            chunks = row.get("chunks") or [f"Заказ #{row['id']} — результат:\n" + content[i:i+2500]
                                            for i in range(0, len(content), 2500)]
            self.phase(row, "completed", chunks=chunks)
            for idx in range(row.get("sent", 0), len(chunks)):
                self.phase(row, "delivering", sent=idx)
                if not self.tell(row, chunks[idx]):
                    raise PluginError("Доставка сообщения не подтверждена. Проверьте чат перед повтором.")
                self.phase(row, "completed", sent=idx + 1)
            self.phase(row, "done", error="")
            self.notify(f"#{row['id']}: выполнен, {row.get('cost', '?')} USD.")
        except PluginError as exc:
            self.phase(row, "delivery_review", error=str(exc))
            self.notify(f"#{row['id']}: товар куплен, но выдача требует проверки. {exc}")

    def tick(self):
        self.guard.check()
        if not self.active():
            return
        candidates = [r for r in self.ledger.rows() if r["state"] in ACTIVE_STATES
                      and time.time() - r.get("last_poll", 0) >= (60 if r.get("slow_notice") else 10)]
        if not candidates:
            self.pricing.tick()
            return
        row = min(candidates, key=lambda r: r.get("last_poll", 0))
        try:
            if row["state"] == "pending":
                self.poll(row)
            elif row["state"] == "completed":
                self.deliver(row)
            else:
                self.process(row)
        except PluginError as exc:
            if row.get("error") != str(exc):
                self.notify(f"#{row['id']}: {exc}")
            row.update(error=str(exc), last_poll=time.time())
            self.ledger.put(row)

    def start(self):
        if self.thread:
            return
        self.guard.install()
        def loop():
            while not self.exit.wait(2):
                with self.tick_lock:
                    if self.tick_pending or self.stopped:
                        continue
                    self.tick_pending = True
                def task():
                    try:
                        self.tick()
                    finally:
                        with self.tick_lock:
                            self.tick_pending = False
                self.submit(task)
        self.thread = threading.Thread(target=loop, name="GameAU-poller", daemon=True)
        self.thread.start()

    def close(self):
        self.stopped = True
        self.exit.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        self.guard.close()
        self.instance.close()


def catalog_rule(item):
    if (not isinstance(item, dict) or item.get("kind") not in KINDS or item.get("currency") != "USD"
            or item.get("available") is not True or item.get("orderable") is not True):
        raise PluginError("Позиция каталога недоступна для покупки.")
    kind = item["kind"]
    order = item.get("order") or {}
    if order.get("endpoint") != "/api/v1/orders/" + kind or not isinstance(order.get("body"), dict):
        raise PluginError("Каталог не содержит ожидаемый шаблон заказа.")
    params = copy.deepcopy(order["body"])
    params.pop("maxCharge", None)
    qty = params.pop("quantity", 1) if kind in {"gamekeys", "giftcards"} else 1
    r = {"kind": kind, "params": params, "quantity": qty, "max_units": 1, "enabled": True,
         "inputs": [], "catalog": {key: item.get(key) for key in ("category_id", "product_id", "region_code")},
         "title": str(item.get("name") or item.get("product_id"))[:150]}
    recipient = {"telegramStars": "telegram_username", "telegramPremium": "telegram_username"}.get(kind)
    if recipient:
        params.pop(recipient, None)
        r["inputs"] = [{"path": recipient, "label": NAMES[recipient]}]
    if kind in {"topups", "manual"}:
        definitions = item.get("fields")
        if not isinstance(definitions, list):
            raise PluginError("Каталог не объявил поля получателя.")
        params["fields"] = {}
        for field in definitions:
            if not isinstance(field, dict) or not isinstance(field.get("key"), str):
                raise PluginError("Некорректное поле получателя в каталоге.")
            if field.get("required") is False:
                continue
            if field.get("type") in {"password", "file"}:
                raise PluginError("Услуги с паролями или файлами не поддерживаются.")
            key = field["key"]
            if key.startswith("fields."):
                key = key[len("fields."):]
            r["inputs"].append({"path": "fields." + key, "label": str(field.get("label") or key)})
    # Validate before showing an offer; max_cost is provided by the owner at the next step.
    check = dict(r, max_cost="1")
    valid = validate_rule(check)
    valid.pop("max_cost")
    return valid


STATES = {"collecting": "Ждём данные покупателя", "confirm": "Ждём подтверждение",
          "ready": "Готов к обработке", "creating": "Запрос покупки", "uncertain": "Покупка требует сверки",
          "pending": "Обрабатывается поставщиком", "completed": "Куплен, ожидает выдачи",
          "delivering": "Отправка сообщения", "delivery_review": "Проверьте выдачу в чате",
          "done": "Выдан", "dry_run": "Тест: покупки не было", "blocked": "Остановлен до покупки",
          "failed": "Не выполнен поставщиком", "cancelled": "Отменён до покупки",
          "purchase_rejected": "GameAU отклонил покупку", "refunding": "Возврат отправляется",
          "refund_review": "Проверьте возврат FunPay", "refunded": "Возвращён на FunPay"}


def rule_summary(rule):
    p = rule["params"]
    kind = rule["kind"]
    if kind == "steamTopup":
        return f"Пополнение Steam: {number(p['amount']).normalize():f} {p['currency']}"
    if kind == "telegramStars":
        return f"Покупатель получит: {p['quantity']} Stars"
    if kind == "telegramPremium":
        return f"Покупатель получит Premium на {p['months']} мес."
    if kind == "steamGifts":
        return f"Игра Steam {p['app_id']}, издание {p['sub_id']}, регион {p['region']}"
    if kind in {"giftcards", "gamekeys"}:
        item = p.get("card_id", p.get("key_id"))
        return f"Покупатель получит кодов: {rule.get('quantity', 1)} · товар {item}"
    return f"Услуга: {rule.get('title') or p.get('offer_id', p.get('product_id'))}"


class Panel:
    def __init__(self, engine):
        self.e = engine
        self.sessions = {}
        self.pending = {}
        self.drafts = {}
        self.choices = {}
        self.price_previews = {}

    @staticmethod
    def context(message, user=None, edit=False):
        return {"chat": message.chat.id, "user": user or message.from_user.id,
                "mid": message.id if edit else None}

    @staticmethod
    def key(ctx):
        return ctx["chat"], ctx["user"]

    def show(self, ctx, text, rows=()):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        token = secrets.token_hex(4)
        keyboard = InlineKeyboardMarkup()
        for row in rows:
            buttons = []
            for label, action in row:
                data = action[1:] if action.startswith("=") else f"gameau:{token}:{action}"
                if len(data.encode()) > 64:
                    raise PluginError("Слишком длинный идентификатор кнопки.")
                buttons.append(InlineKeyboardButton(label, callback_data=data))
            keyboard.row(*buttons)
        bot = self.e.c.telegram.bot
        body = html.escape(text[:3800])
        if ctx.get("mid"):
            try:
                bot.edit_message_text(body, ctx["chat"], ctx["mid"], reply_markup=keyboard, parse_mode="HTML")
            except Exception:
                sent = bot.send_message(ctx["chat"], body, reply_markup=keyboard, parse_mode="HTML")
                ctx["mid"] = sent.message_id
        else:
            sent = bot.send_message(ctx["chat"], body, reply_markup=keyboard, parse_mode="HTML")
            ctx["mid"] = sent.message_id
        self.sessions[self.key(ctx)] = {"token": token, "mid": ctx["mid"]}

    def home(self, ctx, note=""):
        self.pending.pop(self.key(ctx), None)
        cfg = self.e.config.snapshot()
        from tg_bot import CBT
        self.show(ctx, f"🛒 GameAU v{VERSION}\n\n"
                  f"Плагин: {'🟢 ВКЛ' if cfg['enabled'] else '🔴 ВЫКЛ'}\n"
                  f"Режим: {'🧪 тест — деньги не списываются' if cfg['dry_run'] else '💳 реальные покупки'}\n"
                  f"Лотов настроено: {len(cfg['lots'])}\n"
                  f"Минимальный остаток: {cfg['min_balance']} USD\n\n{note}",
                  [[("🔴 Выключить" if cfg["enabled"] else "🟢 Включить", "toggle")],
                   [("🧪 Тест / реальные покупки", "mode")], [("🔑 API-ключ", "key"), ("💰 Баланс", "balance")],
                   [("📦 Мои лоты", "lots:0"), ("🛍 Каталог API", "catalog")],
                   [("🧾 Заказы", "orders:0"), ("🛡 Минимальный остаток", "reserve")],
                   [("↩️ Автовозврат", "refund_settings")],
                   [("📉 Отключение лотов по балансу", "guard")],
                   [("💸 Цены и комиссии", "pricing")],
                   [("❓ Как настроить", "help"), ("🔄 Обновить меню", "home")],
                   [("◀️ К плагину", f"={CBT.EDIT_PLUGIN}:{UUID}:0")]])

    def begin(self, ctx, step, prompt, **data):
        self.pending[self.key(ctx)] = {"step": step, "data": data, "expires": time.time()+900}
        self.show(ctx, prompt, [[("❌ Отмена", "home")]])

    def has_input(self, message):
        if not authorized(self.e.c, message) or (message.text or "").startswith("/"):
            return False
        p = self.pending.get((message.chat.id, message.from_user.id))
        return bool(p and p["expires"] >= time.time())

    def lots(self, ctx, page=0):
        lots = list(self.e.refresh_lots().values())
        cfg = self.e.config.snapshot()
        rows = [[(f"{'✅' if str(lot.id) in cfg['lots'] else '➕'} {lot.id} · {lot.description[:30]}", f"lot:{lot.id}")]
                for lot in lots[page*8:page*8+8]]
        # Include configured inactive lots: their settings must remain reachable.
        missing = [lid for lid in cfg["lots"] if int(lid) not in self.e.lot_cache]
        if page == 0:
            rows += [[(f"⚪ Неактивный лот {lid}", f"lot:{lid}")] for lid in missing[:8]]
        if page:
            rows.append([("⬅️", f"lots:{page-1}")])
        if len(lots) > page*8+8:
            rows.append([("➡️", f"lots:{page+1}")])
        rows += [[("🔄 Обновить с FunPay", "lots:0")], [("🏠 Меню", "home")]]
        self.show(ctx, "📦 Лоты FunPay\nСвежий список с сайта. Новые лоты доступны без перезапуска.\n"
                  "Перед настройкой отключите у лота автовыдачу FunPay и Cardinal.", rows)

    def lot(self, ctx, lid):
        r = self.e.config.snapshot()["lots"].get(lid)
        text = f"📦 Лот {lid}\n"
        if r:
            text += (f"{KINDS[r['kind']]}\n{'✅ Включён' if r.get('enabled', True) else '⛔ Выключен'}\n"
                     f"За 1 штуку лота:\n{rule_summary(r)}\n"
                     f"Максимальное списание за весь заказ: {r['max_cost']} USD\n"
                     f"{order_limits(r)}")
        else:
            text += "Не настроен."
        rows = [[("⚙️ Настроить по шагам", f"setup:{lid}")], [("📝 Настроить JSON", f"json:{lid}")]]
        if r:
            if r["kind"] == "steamTopup":
                rows.append([("📦 Лимит штук", f"steam_units:{lid}"), ("💵 Лимит пополнения", f"steam_cap:{lid}")])
            rows.append([("💸 Цена и комиссии", f"price:{lid}")])
            rows += [[("✅ / ⛔ Переключить", f"switch:{lid}")], [("🗑 Убрать привязку", f"unbind:{lid}")]]
        self.show(ctx, text, rows + [[("◀️ Лоты", "lots:0")]])

    def select_kind(self, ctx, lid):
        self.drafts[self.key(ctx)] = {"lot_id": lid}
        self.show(ctx, "Что выдаёт этот лот?", [[(name, f"kind:{kind}")] for kind, name in KINDS.items()]
                  + [[("❌ Отмена", "home")]])

    def products(self, ctx, search="", page=0):
        d = self.drafts[self.key(ctx)]
        result = self.e.catalog(d["kind"], search, page * 8, limit=8)
        items = result["items"]
        self.choices[self.key(ctx)] = items
        d.update(search=search, page=page)
        rows = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            available = "✅" if item.get("orderable") and item.get("available") else "⛔"
            label = f"{available} {item.get('name', '?')[:25]} · {item.get('region_code') or '—'} · {item.get('price')} $"
            rows.append([(label[:60], f"product:{i}")])
        rows += [[("🔎 Поиск по названию или ID", "search")]]
        if page:
            rows.append([("⬅️", f"products:{page-1}")])
        if result.get("next_offset") is not None:
            rows.append([("➡️", f"products:{page+1}")])
        self.show(ctx, f"🛍 {KINDS[d['kind']]}\nВыберите товар и проверьте регион.\n"
                  "⛔ — недоступен. Цена из каталога учитывает тариф GameAU.",
                  rows + [[("❌ Отмена", "home")]])

    def offers(self, ctx, product):
        app_id = integer(product, 10**12)
        game = self.e.api.call("/steamGifts/games/" + str(app_id)).get("game")
        if not isinstance(game, dict) or game.get("app_id") != app_id or game.get("currency") != "USD":
            raise PluginError("Не удалось проверить игру Steam.")
        offers = []
        for offer in game.get("offers", []):
            for region in offer.get("regions", []):
                if region.get("available") is True and region.get("price") is not None:
                    offers.append({"app_id": app_id, "sub_id": integer(offer.get("sub_id"), 10**12),
                                   "region": region["region"], "price": str(number(region["price"])),
                                   "name": str(offer.get("name") or game.get("name") or app_id)})
        if not offers:
            raise PluginError("Для этой игры сейчас нет доступных Steam-гифтов.")
        self.drafts[self.key(ctx)]["offers"] = offers
        self.show_offers(ctx, 0)

    def show_offers(self, ctx, page):
        offers = self.drafts[self.key(ctx)]["offers"]
        rows = [[(f"{o['name'][:25]} · {o['region']} · {o['price']} $"[:60], f"offer:{i}")]
                for i, o in enumerate(offers[page*8:page*8+8], start=page*8)]
        if page:
            rows.append([("⬅️", f"offers:{page-1}")])
        if len(offers) > page*8+8:
            rows.append([("➡️", f"offers:{page+1}")])
        self.show(ctx, "Выберите издание и регион получателя.\nПеред покупкой будет новый расчёт цены.",
                  rows + [[("❌ Отмена", "home")]])

    def cost_prompt(self, ctx):
        rule = self.drafts[self.key(ctx)]["rule"]
        region = (rule.get("catalog") or {}).get("region_code") or rule["params"].get("region") or "—"
        self.begin(ctx, "cost", f"{rule.get('title', KINDS[rule['kind']])}\n{rule_summary(rule)}\nРегион: {region}\n\n"
                   "🛡 Введите максимальное списание за ВЕСЬ заказ в USD, например 5.50.\n"
                   "Это закупочная стоимость, а цену продажи вы задаёте на FunPay. "
                   "Лимит maxCharge передаётся GameAU: если цена выше, покупка отклоняется.")

    def preview(self, ctx):
        d = self.drafts[self.key(ctx)]
        r = validate_rule(d["rule"])
        rows = []
        if r["kind"] == "steamTopup":
            rows.append([("📦 Лимит штук", "steam_units:draft"), ("💵 Лимит пополнения", "steam_cap:draft")])
        self.show(ctx, f"Проверьте привязку лота {d['lot_id']}\n\n{KINDS[r['kind']]}\n"
                  f"За 1 штуку лота:\n{rule_summary(r)}\n"
                  f"Максимальное списание за заказ: {r['max_cost']} USD\n"
                  f"{order_limits(r)}"
                  f"Данные от покупателя: {', '.join(f.get('label', f['path']) for f in r['inputs']) or 'не нужны'}\n\n"
                  "Проверьте регион товара. Сохранение не выполняет покупку.",
                  rows + [[("💾 Сохранить", "save")], [("❌ Отмена", "home")]])

    def steam_limit(self, ctx, step, target, text):
        cfg = self.e.config.snapshot()
        draft = self.drafts.get(self.key(ctx), {})
        r = copy.deepcopy(draft.get("rule") if target == "draft" else cfg["lots"].get(target))
        if not r or r["kind"] != "steamTopup":
            raise PluginError("Сначала настройте пополнение Steam.")
        if step == "steam_units":
            r["max_units"] = STEAM_MAX_UNITS if text == "0" else integer(text, STEAM_MAX_UNITS)
        else:
            r["max_topup_amount"] = str(number(text.replace(",", "."), zero=True))
        validated = validate_rule(r)
        self.pending.pop(self.key(ctx), None)
        if target == "draft":
            draft["rule"] = validated
            self.preview(ctx)
        else:
            cfg["lots"][target] = validated
            self.e.config.save(cfg)
            self.e.pricing.checked.pop(target, None)
            self.lot(ctx, target)

    def pricing_menu(self, ctx):
        cfg = self.e.config.snapshot()
        p = cfg["pricing"]
        source = "предварительный расчёт вывода FunPay USDT TRC20" if p["source"] == "funpay" else "ручной курс"
        rates = ", ".join(f"{v} {k}" for k, v in sorted(p["manual_rates"].items())) or "не заданы"
        state = "выключены"
        if p["enabled"]:
            state = "включены" if self.e.active() and not cfg["dry_run"] else "на паузе"
        self.show(ctx, f"💸 Автоцены {state}\n"
                  f"Источник курса: {source}\nРучные курсы за 1 USDT: {rates}\n"
                  f"Сумма для расчёта вывода: {p['withdraw_usdt']} USDT\n"
                  f"Пополнение 1 USD GameAU: {p['usdt_per_usd']} USDT\n"
                  f"Целевая чистая маржа: {p['margin']}%\nДоп. расходы от выручки: {p['extra_percent']}%\n\n"
                  "Расчёт по каждому лоту — в разделе «Цены лотов».",
                  [[("🔴 Выключить автоцены" if p["enabled"] else "🟢 Включить автоцены", "price_toggle")],
                   [("📈 Маржа и расходы", "price_profit"), ("🔁 Источник курса", "price_source")],
                   [("💱 Ручной курс USDT", "price_rate"), ("🧮 Сумма вывода", "price_withdraw")],
                   [("💳 USDT за 1 USD GameAU", "price_fund")],
                   [("📦 Цены лотов", "price_list:0"), ("🏠 Меню", "home")]])

    def pricing_list(self, ctx, page):
        cfg = self.e.config.snapshot()
        lots = list(cfg["lots"])
        rows = []
        lines = ["📦 Цены привязанных лотов"]
        for lid in lots[page * 8:page * 8 + 8]:
            mode = "ручная" if lid in cfg["pricing"]["manual_lots"] else "авто"
            _, status = self.e.pricing.status.get(lid, (0, "ещё не проверен"))
            rows.append([(f"#{lid} · {mode}", f"price:{lid}")])
            lines.append(f"#{lid} · {mode} · {status}")
        nav = []
        if page > 0:
            nav.append(("⬅️", f"price_list:{page - 1}"))
        if len(lots) > (page + 1) * 8:
            nav.append(("➡️", f"price_list:{page + 1}"))
        if nav:
            rows.append(nav)
        self.show(ctx, "\n".join(lines), rows + [[("◀️ Цены и комиссии", "pricing")]])

    def price_preview(self, ctx, preview, manual=False):
        lid = preview["lot"]
        currency = preview["currency"]
        unit = currency.name
        current, target = preview["current"], preview["target"]
        self.price_previews[self.key(ctx)] = (preview, manual)
        text = f"💸 Лот #{lid}, за одну штуку\nТекущая цена продавца: {current:f} {unit}\n"
        text += f"{'Ручная' if manual else 'Расчётная'} цена продавца: {target:.2f} {unit}\n"
        if not manual:
            text += (f"Закупка GameAU за штуку: {preview['price_usd']:.6f} USD = {preview['usdt']:.6f} USDT\n"
                     f"Эффективный курс: {preview['rate']:.4f} {unit}/USDT\n")
            basis = preview["steam_basis"]
            if basis:
                text += (f"Котировка GameAU: {basis['units']} шт. = {basis['amount'].normalize():f} "
                         f"{basis['currency']} за {basis['charge']:f} USD.\n"
                         "Себестоимость за штуку учитывает округление закупки для разных объёмов.\n")
            if preview["withdrawal"]:
                debit, receive = preview["withdrawal"]
                text += (f"Расчёт вывода с комиссией и конвертацией: {debit:f} {unit} → {receive:f} USDT\n"
                         f"Эквивалент поступления после вывода: {target / preview['rate']:.4f} USDT\n")
            else:
                text += "Источник курса: вручную, с учётом расходов, включённых в ваш курс.\n"
            text += (f"Себестоимость: {preview['cost']:.4f} {unit}\n"
                     f"Доп. расходы на продажу: {preview['fee']:.4f} {unit}\n"
                     f"Выручка после доп. расходов: {preview['net']:.4f} {unit}\n"
                     f"Чистая прибыль: {preview['profit']:.4f} {unit}\n"
                     f"Маржа: {preview['margin']:.2f}% · цель {preview['desired']}%\n"
                     f"Маржа при текущей цене: {preview['old_margin']:.2f}%\n")
        text += "\nПокупатель платит с комиссиями FunPay и способа оплаты\n"
        previous = {(m.name, m.currency.name): number(m.price) for m in preview.get("old_methods", [])}
        for method in preview["methods"]:
            total = number(method.price)
            old = previous.get((method.name, method.currency.name))
            start = f"{old:.2f} → " if old is not None else ""
            fee = f" (+{total - target:.2f} {unit})" if method.currency == currency and total >= target else ""
            text += f"• {str(method.name or 'Способ оплаты')[:45]}: {start}{total:.2f} {method.currency.name}{fee}\n"
        if manual:
            text += "\nСохранение переведёт этот лот на ручную цену."
        self.show(ctx, text, [[("💾 Сохранить цену на FunPay", f"price_save:{lid}")],
                              [("◀️ К лоту", f"price:{lid}")]])

    def price_input(self, ctx, step, text, data):
        cfg = self.e.config.snapshot()
        p = cfg["pricing"]
        values = text.replace(",", ".").split()
        if step == "price_manual":
            preview = self.e.pricing.manual_preview(data["lot_id"], number(text.replace(",", ".")))
            self.pending.pop(self.key(ctx), None)
            self.price_preview(ctx, preview, manual=True)
            return
        if step == "price_rate":
            if len(values) != 2 or values[0].upper() not in LOT_CURRENCIES:
                raise PluginError("Формат: RUB 94.50, USD 1.05 или EUR 0.95.")
            p["manual_rates"][values[0].upper()] = str(number(values[1]))
            p["source"] = "manual"
        elif step == "price_profit":
            if len(values) not in {1, 2}:
                raise PluginError("Введите маржу и доп. расходы через пробел, например 12 1.5.")
            p["margin"] = str(number(values[0], zero=True))
            if len(values) == 2:
                p["extra_percent"] = str(number(values[1], zero=True))
        else:
            p[{"price_withdraw": "withdraw_usdt", "price_fund": "usdt_per_usd"}[step]] = str(number(text.replace(",", ".")))
        self.e.config.save(cfg)
        self.e.pricing.checked.clear()
        self.pending.pop(self.key(ctx), None)
        self.pricing_menu(ctx)

    def input(self, message):
        if not self.has_input(message):
            return
        ctx = self.context(message)
        p = self.pending[self.key(ctx)]
        text = message.text.strip()
        step = p["step"]
        try:
            cfg = self.e.config.snapshot()
            d = self.drafts.get(self.key(ctx), {})
            if step.startswith("price_"):
                self.price_input(ctx, step, text, p["data"])
            elif step in {"steam_units", "steam_cap"}:
                self.steam_limit(ctx, step, p["data"]["target"], text)
            elif step == "key":
                if len(text) < 8 or len(text) > 1024 or any(c.isspace() for c in text):
                    raise PluginError("Неверный формат API-ключа.")
                if os.environ.get("GAMEAU_API_KEY"):
                    raise PluginError("Задана GAMEAU_API_KEY. Измените её в окружении сервера: она имеет приоритет.")
                old = copy.deepcopy(cfg)
                cfg["api_key"] = text
                self.e.config.save(cfg)
                try:
                    self.e.c.telegram.bot.delete_message(message.chat.id, message.id)
                except Exception:
                    pass
                try:
                    self.e.account()
                except Exception:
                    self.e.config.save(old)
                    raise
                self.pending.pop(self.key(ctx), None)
                self.home(ctx, "✅ Ключ проверен и сохранён. Значение ключа не показывается.")
            elif step == "reserve":
                cfg["min_balance"] = str(number(text.replace(",", "."), zero=True))
                self.e.config.save(cfg)
                self.home(ctx, "Минимальный остаток сохранён.")
            elif step == "guard_threshold":
                cfg["guard_threshold"] = str(number(text.replace(",", "."), zero=True))
                self.e.config.save(cfg)
                self.pending.pop(self.key(ctx), None)
                self.route(ctx, "guard")
            elif step == "search":
                self.products(ctx, text)
                self.pending.pop(self.key(ctx), None)
            elif step == "gift_app":
                self.offers(ctx, text)
                self.pending.pop(self.key(ctx), None)
            elif step == "steam":
                pair = text.split()
                if len(pair) != 2:
                    raise PluginError("Введите сумму и валюту: 100 RUB или 5 USD.")
                r = {"kind": "steamTopup", "quantity": 1, "max_units": 1, "enabled": True,
                     "params": {"amount": float(number(pair[0].replace(",", "."))), "currency": pair[1].upper()},
                     "inputs": [{"path": "steamLogin", "label": NAMES["steamLogin"]}]}
                validate_rule(dict(r, max_cost="1"))
                d["rule"] = r
                self.cost_prompt(ctx)

            elif step == "cost":
                d["rule"]["max_cost"] = str(usd(text.replace(",", ".")))
                if d["kind"] in {"gamekeys", "giftcards"}:
                    self.begin(ctx, "units", "Сколько штук лота покупатель может купить одним заказом?\n"
                               f"Введите число 1–100. Одна штука лота = {d['rule']['quantity']} код(ов). Для старта: 1.")
                else:
                    self.pending.pop(self.key(ctx), None)
                    self.preview(ctx)
            elif step == "units":
                d["rule"]["max_units"] = integer(text, 100)
                self.pending.pop(self.key(ctx), None)
                self.preview(ctx)
            elif step == "json":
                r = validate_rule(json.loads(text))
                self.drafts[self.key(ctx)] = {"lot_id": p["data"]["lot_id"], "kind": r["kind"], "rule": r}
                self.pending.pop(self.key(ctx), None)
                self.preview(ctx)
        except (PluginError, ValueError, TypeError) as exc:
            text = str(exc) if isinstance(exc, PluginError) else "Неверный формат. Проверьте ввод и повторите."
            self.show(ctx, text, [[("🏠 Меню", "home")]])

    def orders(self, ctx, page=0):
        rows = self.e.ledger.rows()
        buttons = [[(f"#{r['id']} · {STATES.get(r['state'], r['state'])}"[:60], f"order:{r['id']}")]
                   for r in rows[page*8:page*8+8]]
        if page:
            buttons.append([("⬅️", f"orders:{page-1}")])
        if len(rows) > page*8+8:
            buttons.append([("➡️", f"orders:{page+1}")])
        self.show(ctx, "🧾 Журнал заказов\nКоды и данные покупателей в Telegram не выводятся.",
                  buttons + [[("🏠 Меню", "home")]])

    def order(self, ctx, oid):
        row = self.e.ledger.get(oid)
        if not row:
            raise PluginError("Заказ не найден.")
        buttons = []
        if row["state"] == "blocked":
            buttons.append([("🔄 Повторить проверку", f"retry:{oid}")])
        if row["state"] == "refund_review":
            buttons.append([("🔄 Сверить возврат на FunPay", f"refund_status:{oid}")])
        if row["state"] == "uncertain":
            buttons.append([("🔎 Восстановить запрос", f"recoverask:{oid}")])
        if row["state"] == "pending":
            buttons.append([("🔄 Проверить статус", f"poll:{oid}")])
        if row["state"] == "delivery_review":
            buttons.append([("💬 Повторить выдачу", f"resendask:{oid}")])
        if row["state"] in {"collecting", "confirm"}:
            buttons.append([("💬 Повторить инструкцию", f"prompt:{oid}")])
        self.show(ctx, f"Заказ #{oid}\n{STATES.get(row['state'], row['state'])}\n"
                  f"Лот: {row['lot_id']}\nGameAU: {row.get('remote_id', 'ещё не известен')}\n"
                  f"Расчёт: {row.get('quoted_usd', '—')} USD\nСписано: {row.get('cost', '—')} USD\n"
                  f"{row.get('error', '')}", buttons + [[("◀️ Заказы", "orders:0")]])

    def callback(self, call):
        if not call.message or not authorized(self.e.c, type("Sender", (), {"chat": call.message.chat, "from_user": call.from_user})()):
            return
        ctx = self.context(call.message, call.from_user.id, True)
        parts = call.data.split(":", 2)
        session = self.sessions.get(self.key(ctx), {})
        if len(parts) != 3 or session.get("token") != parts[1] or session.get("mid") != call.message.id:
            return
        self.pending.pop(self.key(ctx), None)
        try:
            self.route(ctx, parts[2])
        except PluginError as exc:
            self.show(ctx, str(exc), [[("🏠 Меню", "home")]])
        except Exception:
            self.show(ctx, "Операция не завершена. Проверьте доступ к API и формат каталога. Покупки из меню настройки не выполняются.",
                      [[("🏠 Меню", "home")]])

    def route(self, ctx, action):
        name, _, arg = action.partition(":")
        cfg = self.e.config.snapshot()
        key = self.key(ctx)
        if name == "home":
            self.home(ctx)
        elif name in {"steam_units", "steam_cap"}:
            r = self.drafts.get(key, {}).get("rule") if arg == "draft" else cfg["lots"].get(arg)
            if not r or r["kind"] != "steamTopup":
                raise PluginError("Сначала настройте пополнение Steam.")
            if name == "steam_units":
                prompt = ("Введите максимум штук в одном заказе.\n"
                          "0 — брать количество из заказа FunPay в пределах суммы пополнения и правил GameAU.\n"
                          "Например, если одна штука = 1 UAH, заказ на 100 штук даст пополнение на 100 UAH.")
            else:
                unit = r["params"]["currency"]
                prompt = (f"Введите максимальную сумму одного пополнения в {unit}.\n"
                          f"Например, 10000 — не более 10 000 {unit} за заказ.\n"
                          "0 — убрать отдельный лимит суммы. Лимит списания USD задаётся отдельно.")
            self.begin(ctx, name, prompt, target=arg)
        elif name in {"refund_settings", "refund_toggle", "refund_reason"}:
            if name == "refund_toggle":
                cfg["auto_refund"] = not cfg["auto_refund"]
                self.e.config.save(cfg)
            elif name == "refund_reason" and arg in REFUND_REASONS:
                if arg in cfg["refund_reasons"]:
                    cfg["refund_reasons"].remove(arg)
                else:
                    cfg["refund_reasons"].append(arg)
                self.e.config.save(cfg)
            on = cfg["auto_refund"]
            rows = [[(f"{'✅' if reason in cfg['refund_reasons'] else '⬜️'} {label}", f"refund_reason:{reason}")]
                    for reason, label in REFUND_REASONS.items()]
            self.show(ctx, f"↩️ Автовозврат: {'ВКЛ' if on else 'ВЫКЛ'}\n\n"
                      "Полный возврат на FunPay для выбранных причин, если закупки в GameAU не было.\n"
                      "Работает при включённом плагине в реальном режиме.",
                      [[("Выключить автовозврат" if on else "Включить автовозврат", "refund_toggle")],
                       *rows, [("🏠 Меню", "home")]])
        elif name == "refund_status":
            row = self.e.ledger.get(arg)
            if not row:
                raise PluginError("Заказ не найден.")
            self.e.check_refund(row)
            self.order(ctx, arg)
        elif name == "pricing":
            self.pricing_menu(ctx)
        elif name == "price_list":
            self.pricing_list(ctx, int(arg))
        elif name == "price_toggle":
            p = cfg["pricing"]
            p["enabled"] = not p["enabled"]
            self.e.config.save(cfg)
            self.e.pricing.checked.clear()
            self.pricing_menu(ctx)
        elif name == "price_source":
            self.show(ctx, "Выберите источник стоимости USDT для расчёта маржи.",
                      [[("Вывод FunPay в USDT TRC20", "price_use:funpay")],
                       [("Мой курс USDT", "price_use:manual")], [("◀️ Цены и комиссии", "pricing")]])
        elif name == "price_use":
            cfg["pricing"]["source"] = arg
            self.e.config.save(cfg)
            self.e.pricing.checked.clear()
            self.pricing_menu(ctx)
        elif name in {"price_profit", "price_rate", "price_withdraw", "price_fund", "price_manual"}:
            prompt = {
                "price_profit": "Введите целевую маржу в процентах. Можно через пробел добавить собственные расходы "
                                "в % от выручки продавца. Пример: 12 1.5. Маржа = прибыль / чистая выручка.",
                "price_rate": "Укажите валюту лота и фактическую стоимость 1 USDT с комиссиями вашего обменника. "
                              "Пример: RUB 94.50. Сохранение включает ручной источник курса.",
                "price_withdraw": "Сколько USDT вы обычно получаете за один вывод? Введите сумму, например 100. "
                                  "От суммы зависит доля фиксированной комиссии вывода.",
                "price_fund": "Сколько USDT вы тратите, чтобы получить 1 USD на балансе GameAU? "
                              "Например, 1.02 учитывает расходы пополнения в размере 2%.",
                "price_manual": "Введите цену продавца за одну штуку в валюте лота. Проверьте вкладку и подтвердите сохранение.",
            }[name]
            self.begin(ctx, name, prompt, lot_id=arg)
        elif name == "price":
            fields = self.e.pricing.lot_fields(arg)
            manual = arg in cfg["pricing"]["manual_lots"]
            text = (f"💸 Лот #{arg}\nЦена продавца: {fields.price} {fields.currency.name}\n"
                    + ("Ручная цена" if manual else "Следует общей настройке автоцен"))
            last = self.e.pricing.status.get(arg)
            if last:
                text += f"\nПоследняя проверка автоцены: {last[1]}"
            self.show(ctx, text, [[("🧮 Свежий расчёт и комиссии", f"price_calc:{arg}")],
                                  [("✍️ Ввести цену вручную", f"price_manual:{arg}")],
                                  [("🟢 Разрешить автоцену" if manual else "⏸ Приостановить автоцену", f"price_lot:{arg}")],
                                  [("◀️ Цены лотов", "price_list:0")]])
        elif name == "price_lot":
            if arg not in cfg["lots"]:
                raise PluginError("Лот больше не привязан к GameAU.")
            manual_lots = cfg["pricing"]["manual_lots"]
            if arg in manual_lots:
                manual_lots.remove(arg)
            else:
                manual_lots.append(arg)
            self.e.config.save(cfg)
            self.e.pricing.checked.pop(arg, None)
            self.route(ctx, f"price:{arg}")
        elif name == "price_calc":
            self.price_preview(ctx, self.e.pricing.calculate(arg))
        elif name == "price_save":
            preview, manual = self.price_previews.pop(key, ({}, False))
            if preview.get("lot") != arg:
                raise PluginError("Сначала откройте свежий расчёт.")
            changed = self.e.pricing.apply(preview, manual=manual)
            self.show(ctx, "Цена сохранена на FunPay." if changed else "На FunPay уже установлена эта цена.",
                      [[("◀️ К лоту", f"price:{arg}")]])
        elif name == "toggle":
            self.e.bind_account()
            cfg = self.e.config.snapshot()
            cfg["enabled"] = not cfg["enabled"]
            self.e.config.save(cfg)
            self.e.paused.clear() if cfg["enabled"] else self.e.paused.set()
            self.home(ctx)
        elif name == "mode":
            self.show(ctx, "Режим работы\n\n🧪 Тест: проверка заказа и расчёт цены, без покупки и выдачи.\n"
                      "💳 Реальный: деньги списываются с GameAU.\n"
                      "Каждой покупке передаётся maxCharge — предел списания в USD. "
                      "Если цена выросла выше него, GameAU отклоняет покупку.\n"
                      "Тестовые заказы никогда не становятся реальными автоматически.",
                      [[("🧪 Включить тест", "dry")], [("💳 Принимаю условия, реальный режим", "live")], [("🏠 Меню", "home")]])
        elif name in {"dry", "live"}:
            cfg["dry_run"] = name == "dry"
            cfg["live_confirmed"] = name == "live"
            self.e.config.save(cfg)
            self.home(ctx)
        elif name == "key":
            self.begin(ctx, "key", "🔑 Отправьте API-ключ GameAU следующим сообщением.\n"
                       "Сообщение с ключом будет удалено, если Telegram разрешит. Ключ сохранится только локально.\n"
                       "Альтернатива: переменная окружения GAMEAU_API_KEY (приоритетнее настройки).")
        elif name == "balance":
            amount = self.e.account()
            self.show(ctx, f"💰 Баланс GameAU: {amount:.4f} USD\nПополнение выполняется в кабинете GameAU.",
                      [[("🏠 Меню", "home")]])
        elif name == "reserve":
            self.begin(ctx, "reserve", "Минимальный остаток USD после закупки.\nВведите 0 для отключения резерва.\n"
                       "Лимит закупки уменьшается так, чтобы сохранить этот остаток по текущему балансу.")
        elif name == "guard":
            holds = self.e.ledger.holds()
            self.show(ctx, f"📉 Контроль баланса: {'ВКЛ' if cfg['guard_enabled'] else 'ВЫКЛ'}\n"
                      f"Снять лоты при балансе ≤ {cfg['guard_threshold']} USD\n"
                      f"Лотов под запретом включения: {len(holds)}\n\n"
                      "Проверка примерно раз в минуту. Снимаются все привязанные лоты. "
                      "В тестовом режиме FunPay не изменяется. После пополнения верните лоты кнопкой ниже. "
                      "Контроль работает отдельно от переключателя закупок. При отключении контроля запрет включения снимается.",
                      [[("⚙️ Настроить порог USD", "guardlimit")],
                       [("🔴 Выключить контроль" if cfg["guard_enabled"] else "🟢 Включить контроль", "guardtoggle")],
                       [("🔄 Проверить сейчас", "guardcheck")], [("⬆️ Вернуть лоты", "guardrestoreask")], [("🏠 Меню", "home")]])
        elif name == "guardlimit":
            self.begin(ctx, "guard_threshold", "При каком балансе USD снимать лоты с продажи?\n"
                       "Например: 5 — лоты отключатся при балансе 5 USD и ниже. 0 — при нулевом балансе.")
        elif name == "guardtoggle":
            self.e.bind_account()
            cfg = self.e.config.snapshot()
            cfg["guard_enabled"] = not cfg["guard_enabled"]
            self.e.config.save(cfg)
            self.e.guard.check(force=True)
            self.route(ctx, "guard")
        elif name == "guardcheck":
            self.e.guard.check(force=True)
            self.route(ctx, "guard")
        elif name == "guardrestoreask":
            self.show(ctx, "Вернуть на продажу лоты, снятые этим плагином?\n"
                      "Проверьте цены, наличие и актуальность привязок. Баланс должен быть выше порога.",
                      [[("✅ Вернуть лоты", "guardrestore")], [("❌ Отмена", "guard")]])
        elif name == "guardrestore":
            restored, errors = self.e.guard.restore()
            self.show(ctx, "Возвращены лоты: " + (", ".join(restored) or "—")
                      + "\nНужна ручная проверка: " + (", ".join(errors) or "—"), [[("◀️ Контроль баланса", "guard")]])
        elif name == "catalog":
            self.show(ctx, "🛍 Поддерживаемые товары GameAU\n" + "\n".join(KINDS.values())
                      + "\n\nКаталог с ценами доступен через Мои лоты → лот → Настроить по шагам. "
                      "Steam-гифты выбираются по app_id игры. Админ-товары с файлами не поддерживаются.",
                      [[("📦 Мои лоты", "lots:0")], [("🏠 Меню", "home")]])

        elif name == "lots":
            self.lots(ctx, int(arg))
        elif name == "lot":
            self.lot(ctx, arg)
        elif name == "setup":
            if int(arg) not in self.e.refresh_lots():
                raise PluginError("Сначала опубликуйте лот FunPay и обновите список.")
            fields = self.e.c.account.get_lot_fields(int(arg))
            if getattr(fields, "auto_delivery", False):
                raise PluginError("Сначала отключите встроенную автовыдачу FunPay у этого лота.")
            self.select_kind(ctx, arg)
        elif name == "kind":
            if arg not in KINDS:
                raise PluginError("Неизвестный вид товара.")
            d = self.drafts[key]
            d["kind"] = arg
            if arg == "steamTopup":
                catalog = self.e.catalog(arg)
                if (catalog.get("steam_topup") or {}).get("available") is not True:
                    raise PluginError("Пополнение Steam временно недоступно.")
                self.begin(ctx, "steam", "Сколько получает покупатель за ОДНУ штуку лота?\n"
                           "Пример: 1 UAH за штуку. При покупке 100 штук плагин запросит одно пополнение на 100 UAH.\n"
                           "Валюты: USD, RUB, UAH, KZT. Списываются USD с GameAU.")
            elif arg == "steamGifts":
                self.begin(ctx, "gift_app", "Введите app_id игры Steam.\n"
                           "Например, для страницы store.steampowered.com/app/1245620/ это 1245620.")
            else:
                self.products(ctx)
        elif name == "search":
            self.begin(ctx, "search", "Введите название или ID товара для поиска в каталоге GameAU.")
        elif name == "products":
            self.products(ctx, self.drafts[key].get("search", ""), int(arg))
        elif name == "product":
            item = self.choices[key][int(arg)]
            d = self.drafts[key]
            if item.get("kind") != d["kind"]:
                raise PluginError("Каталог вернул другой вид товара.")
            d["rule"] = catalog_rule(item)
            self.cost_prompt(ctx)
        elif name == "offers":
            self.show_offers(ctx, int(arg))
        elif name == "offer":
            d = self.drafts[key]
            offer = d["offers"][int(arg)]
            d["rule"] = {"kind": "steamGifts", "quantity": 1, "max_units": 1, "enabled": True,
                         "title": offer["name"], "params": {k: offer[k] for k in ("app_id", "sub_id", "region")},
                         "inputs": [{"path": "invite_url", "label": NAMES["invite_url"]}]}
            self.cost_prompt(ctx)

        elif name == "save":
            d = self.drafts[key]
            if int(d["lot_id"]) not in self.e.refresh_lots():
                raise PluginError("Лот больше не виден в профиле. Проверьте публикацию.")
            fields = self.e.c.account.get_lot_fields(int(d["lot_id"]))
            if getattr(fields, "auto_delivery", False):
                raise PluginError("Отключите выдачу самого FunPay.")
            cfg["lots"][d["lot_id"]] = validate_rule(d["rule"])
            self.e.config.save(cfg)
            self.lot(ctx, d["lot_id"])
        elif name == "json":
            self.begin(ctx, "json", "Вставьте JSON привязки из примеров README.\n"
                       "Поля: kind, catalog, params, inputs, max_cost, max_units.\n"
                       "Используйте реальные ID из API. Ключ API сюда вставлять не нужно.", lot_id=arg)
        elif name == "switch":
            cfg["lots"][arg]["enabled"] = not cfg["lots"][arg].get("enabled", True)
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif name == "unbind":
            cfg["lots"].pop(arg, None)
            if arg in cfg["pricing"]["manual_lots"]:
                cfg["pricing"]["manual_lots"].remove(arg)
            self.e.config.save(cfg)
            self.lot(ctx, arg)
        elif name == "orders":
            self.orders(ctx, int(arg))
        elif name == "order":
            self.order(ctx, arg)
        elif name in {"recoverask", "resendask"}:
            text = ("Восстановить неизвестный запрос покупки?\nСначала проверьте заказ в кабинете GameAU. "
                    "Будет отправлено сохранённое тело с ТЕМ ЖЕ ключом идемпотентности. "
                    "Если первый запрос не дошёл, это выполнит покупку. Не используйте после ручной закупки/возврата."
                    if name == "recoverask" else "Проверьте чат покупателя. Повтор может продублировать сообщение, "
                    "если FunPay принял его, но ответ потерялся. Повторной покупки у поставщика не будет.")
            self.show(ctx, text, [[("✅ Подтверждаю", f"{'recover' if name == 'recoverask' else 'resend'}:{arg}")],
                                  [("❌ Отмена", f"order:{arg}")]])
        elif name in {"retry", "recover", "resend", "poll", "prompt"}:
            row = self.e.ledger.get(arg)
            if not row or not self.e.active():
                raise PluginError("Включите плагин, чтобы обработать заказ.")
            if name == "retry" and row["state"] == "blocked":
                self.e.phase(row, "ready", error="")
                self.e.process(row)
            elif name == "recover" and row["state"] == "uncertain":
                if cfg["dry_run"] or row["dry_run"] or not cfg["live_confirmed"]:
                    raise PluginError("В тестовом режиме запрос покупки запрещён.")
                self.e.verify_order(row)
                self.e.same_key(row)
                self.e.phase(row, "creating")
                self.e.create(row)
            elif name == "resend" and row["state"] == "delivery_review":
                self.e.deliver(row)
            elif name == "poll" and row["state"] == "pending":
                self.e.poll(row)
            elif name == "prompt" and row["state"] in {"collecting", "confirm"}:
                self.e.ask(row)
            self.order(ctx, arg)
        elif name == "help":
            self.show(ctx, "1. Укажите ключ GameAU.\n2. Откройте Мои лоты → Настроить по шагам.\n"
                      "3. Отключите штатную автовыдачу FunPay и Cardinal у этих лотов.\n"
                      "4. Проверьте тестовый режим на заказе. Тест не выполняет выдачу!\n"
                      "5. Включите реальный режим и плагин.\n\n"
                      "При необходимости покупатель получит инструкции !gameau и подтвердит реквизиты. "
                      "Ключи/карты выдаются после успешного статуса GameAU. Условия возврата задаются в меню «Автовозврат».\n"
                      "Не удаляйте orders.db: это защита от повторных покупок.", [[("🏠 Меню", "home")]])


_engine = None


def init(cardinal, *args):
    global _engine
    if _engine is not None:
        return
    try:
        _engine = Engine(cardinal)
    except Exception:
        LOG.error("GameAU: ошибка конфигурации/блокировки/журнала. Плагин не запущен, данные не сброшены.")
        return
    if cardinal.telegram:
        cardinal.add_telegram_commands(UUID, [("gameau", "GameAU: автовыдача", True)])
        from tg_bot import CBT

        def command(message):
            if authorized(cardinal, message):
                if message.text.split()[0].split("@")[0] == "/gameau_off":
                    _engine.paused.set()
                    def disable():
                        cfg = _engine.config.snapshot()
                        cfg["enabled"] = False
                        _engine.config.save(cfg)
                        _engine.panel.home(_engine.panel.context(message))
                    _engine.submit(disable)
                else:
                    _engine.submit(_engine.panel.home, _engine.panel.context(message))

        def callback(call):
            if (not call.message or call.message.chat.type != "private"
                    or call.from_user.id not in cardinal.telegram.authorized_users):
                return
            try:
                cardinal.telegram.bot.answer_callback_query(call.id)
            except Exception:
                pass
            session = _engine.panel.sessions.get((call.message.chat.id, call.from_user.id), {})
            parts = call.data.split(":", 2)
            if (len(parts) == 3 and parts[1] == session.get("token") and call.message.id == session.get("mid")
                    and parts[2] == "toggle" and _engine.config.snapshot()["enabled"]):
                _engine.paused.set()
            if call.data.startswith("gameau:"):
                _engine.submit(_engine.panel.callback, call)
            else:
                _engine.submit(_engine.panel.home, _engine.panel.context(call.message, call.from_user.id, True))

        cardinal.telegram.msg_handler(command, commands=["gameau", "gameau_off"])
        cardinal.telegram.cbq_handler(callback, lambda c: bool(c.data) and (c.data.startswith("gameau:")
                                      or c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}")))
        cardinal.telegram.msg_handler(lambda m: _engine.submit(_engine.panel.input, m),
                                      func=_engine.panel.has_input, content_types=["text"])
    LOG.info("GameAU %s initialized", VERSION)


def new_order(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.new_order, event)


def new_message(cardinal, event, *args):
    if _engine:
        _engine.submit(_engine.message, event.message)


def start(cardinal, *args):
    if _engine:
        _engine.start()


def stop(*args):
    if _engine and not _engine.stopped:
        _engine.stopped = True
        _engine.exit.set()
        # Retain instance lock until in-flight work has finished.
        threading.Thread(target=_engine.close, name="GameAU-shutdown", daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = stop
