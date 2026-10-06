"""HTML rendering kept independent from application startup and persistence."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


_ROOT = Path(__file__).resolve().parent
_TITLES = {
    "shop": "购买套餐",
    "orders": "我的订单",
    "order": "订单详情",
    "admin": "销售管理",
    "manual": "人工注册与续期",
}
_TEMPLATES = Environment(
    loader=FileSystemLoader(_ROOT / "templates"),
    autoescape=select_autoescape(["html"]),
)


def render_page(name, context=None):
    name = {"my-orders": "orders"}.get(name, name)
    if name not in _TITLES:
        raise ValueError("Unknown payment page")
    values = dict(context or {})
    values.update(page=name, page_title=_TITLES[name])
    values.setdefault("brand_name", "DuSheng")
    template = "manual.html" if name == "manual" else "page.html"
    return _TEMPLATES.get_template(template).render(**values)
