# Расширенная JSON-настройка

Обычно достаточно мастера с кнопками. JSON доступен через **Мои лоты → лот → Настроить JSON**.

Пример пополнения Steam на выбранную сумму 100 RUB:

```json
{
  "enabled": true,
  "kind": "steamTopup",
  "params": {"amount": 100, "currency": "RUB"},
  "inputs": [{"path": "steamLogin", "label": "Логин Steam"}],
  "quantity": 1,
  "max_units": 1,
  "max_cost": "2.50"
}
```

`max_cost` — пример максимального списания **за весь заказ в USD**, не текущая котировка. Настройте его по цене в кабинете. Сумма допускает до четырёх знаков после точки.

Для каталожных товаров обязательны идентификаторы выбранной строки каталога. Пример карты с вымышленными ID:

```json
{
  "enabled": true,
  "kind": "giftcards",
  "catalog": {
    "category_id": "CATEGORY_ID",
    "product_id": "CARD_ID",
    "region_code": "GLOBAL"
  },
  "params": {"category_id": "CATEGORY_ID", "card_id": "CARD_ID"},
  "inputs": [],
  "quantity": 1,
  "max_units": 2,
  "max_cost": "10.00"
}
```

Используйте реальные `catalog.items[].category_id`, `product_id`, `region_code` и `order.body` из GameAU. `region_code` может быть `null`. Значения `params` должны соответствовать текущему шаблону; плагин проверяет их заново перед покупкой. Идентификатор товара каталога и ID в теле запроса не всегда называются одинаково.

| Тип | Параметры `params` | Поля `inputs` |
|---|---|---|
| `gamekeys` | `game_id`, `key_id` | `[]` |
| `giftcards` | `category_id`, `card_id` | `[]` |
| `topups` | `category_id`, `offer_id`, `fields: {}` | Поля получателя из каталога. |
| `manual` | `manual_service_id`, `product_id`, `fields: {}` | Поля конкретной услуги. |
| `telegramStars` | `quantity` — число Stars из шаблона | `[{"path":"telegram_username"}]` |
| `telegramPremium` | `months` — 3, 6 или 12 | `[{"path":"telegram_username"}]` |
| `steamTopup` | `amount`, `currency` — USD/RUB/UAH/KZT | `[{"path":"steamLogin"}]` |
| `steamGifts` | Целые `app_id`, `sub_id`; строка `region` | `[{"path":"invite_url"}]` |

Для Steam `catalog` не нужен: используется отдельный расчёт API. Для остальных видов его поля обязательны.

`quantity` на верхнем уровне привязки — сколько кодов соответствует одной штуке лота; в запрос API для карт/ключей попадёт это число, умноженное на количество штук FunPay. Для услуг верхние `quantity` и `max_units` должны быть равны 1. Число Stars находится **в `params.quantity`**, это отдельный параметр.

Пример динамического поля: `{"path":"fields.player_id","label":"ID игрока"}`. Плагин запрашивает значение и отправляет `params.fields.player_id` строкой, сохраняя ведущие нули. Названия и набор обязательных полей должны совпадать с текущим каталогом. Произвольно переименовывать их нельзя. Данные получателя для Steam и Telegram всегда запрашиваются у покупателя, а не фиксируются в привязке.

Меню JSON не принимает API-ключ — для него есть отдельная кнопка. Изменение настроек не меняет уже сохранённые заказы.
