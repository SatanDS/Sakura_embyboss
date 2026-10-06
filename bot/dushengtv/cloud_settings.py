"""Portable desktop preferences only; account identity always comes from auth."""

import json
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = json.loads(Path(__file__).with_name("cloud-settings-schema.json").read_text(encoding="utf-8"))
# Accept this historical preference from older clients, validate it using its
# original bounds, then omit it from persisted and returned portable settings.
DEPRECATED_RULES = {"danmakuArea": {"type": "number", "minimum": 0.2, "maximum": 1}}
INPUT_RULES = {**SCHEMA["properties"], **DEPRECATED_RULES}


def without_deprecated_settings(values):
    return {key: value for key, value in values.items() if key not in DEPRECATED_RULES}


def validate_cloud_settings(data):
    if not isinstance(data, dict) or set(data) != {"schema", "settings"} or type(data["schema"]) is not int or data["schema"] != 1:
        raise ValueError("云端设置格式或版本无效")
    values = data["settings"]
    if not isinstance(values, dict) or not values or set(values) - set(INPUT_RULES):
        raise ValueError("备份包含不支持的设置")
    for key, value in values.items():
        rule = INPUT_RULES[key]
        kind = rule["type"]
        valid = (type(value) is bool if kind == "boolean" else
                 type(value) is str if kind == "string" else
                 type(value) is int if kind == "integer" else
                 type(value) in (int, float) and math.isfinite(value))
        if not valid:
            raise ValueError("设置类型无效")
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("设置选项无效")
        if kind in ("integer", "number") and not rule.get("minimum", -math.inf) <= value <= rule.get("maximum", math.inf):
            raise ValueError("设置数值超出范围")
        if kind == "string" and (len(value) > rule["maxLength"] or not re.fullmatch(rule["pattern"], value)):
            raise ValueError("设置内容无效")
        if key == "subtitleTranslationEndpoint":
            try:
                endpoint = urlsplit(value)
                if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname
                        or endpoint.username is not None or endpoint.password is not None
                        or endpoint.query or endpoint.fragment or endpoint.port == 0
                        or any(char.isspace() or ord(char) < 32 for char in value)
                        or (endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"})):
                    raise ValueError()
            except ValueError:
                raise ValueError("翻译 API 地址无效，不能包含密钥或账号密码") from None
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32768:
        raise ValueError("设置内容过大")
    settings = without_deprecated_settings(values)
    if not settings:
        raise ValueError("备份不包含可恢复的设置")
    return settings
