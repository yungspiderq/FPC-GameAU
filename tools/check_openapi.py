"""Validate all eight request bodies against a downloaded official GameAU OpenAPI file.

Usage: python tools/check_openapi.py /path/to/openapi.yaml
No API key, order or network request is used by this check.
"""
import sys
from pathlib import Path

import jsonschema
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from test_plugin import p, rule  # noqa: E402

spec = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert spec["servers"][0]["url"] == p.BASE_URL
names = {"gamekeys": "GameKeyOrder", "giftcards": "GiftCardOrder", "topups": "TopupOrder",
         "manual": "ManualOrder", "steamTopup": "SteamTopupOrder", "steamGifts": "SteamGiftOrder",
         "telegramStars": "TelegramStarsOrder", "telegramPremium": "TelegramPremiumOrder"}
for kind, name in names.items():
    configured = rule(kind)
    values = {field["path"]: "https://steamcommunity.com/id/player123" if field["path"] == "invite_url"
              else "player123" for field in configured["inputs"]}
    body = p.make_payload(configured, 1, values)
    body["maxCharge"] = 5.25
    schema = {"$ref": "#/components/schemas/" + name, "components": spec["components"]}
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(body)
    print(kind + ": PASS")
statuses = set(spec["components"]["schemas"]["Order"]["properties"]["status"]["enum"])
assert statuses == p.REMOTE_STATES
print("Official OpenAPI", spec["info"]["version"], ": request schemas and statuses PASS")
