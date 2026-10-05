"""Portable desktop preferences only; account identity always comes from auth."""

import json
import math
import re
from pathlib import Path

SCHEMA = json.loads(Path(__file__).with_name("cloud-settings-schema.json").read_text(encoding="utf-8"))


def validate_cloud_settings(data):
    if not isinstance(data, dict) or set(data) != {"schema", "settings"} or type(data["schema"]) is not int or data["schema"] != 1:
        raise ValueError("云端设置格式或版本无效")
    values = data["settings"]
    if not isinstance(values, dict) or not values or set(values) - set(SCHEMA["properties"]):
        raise ValueError("备份包含不支持的设置")
    for key, value in values.items():
        rule = SCHEMA["properties"][key]
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
    if len(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 32768:
        raise ValueError("设置内容过大")
    return dict(values)
