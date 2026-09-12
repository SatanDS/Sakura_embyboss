"""Public payment shop and owner/admin order endpoints."""

import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
import asyncio
from datetime import datetime, timezone
from typing import Literal

from cacheout import Cache
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt

from bot import bot, config
from bot import LOGGER
owner = getattr(__import__('bot'), 'owner', 0)
admins = getattr(__import__('bot'), 'admins', [])
bot_name = getattr(__import__('bot'), 'bot_name', 'bot')
from bot.payments.pages import render_page
from bot.payments.diagnostics import log_payment_error
try:
    from bot.payments.models import Audit, Order
    from bot.payments.browser_auth import BrowserAuth, LoginError, session_csrf, LOGIN_SECONDS, SESSION_SECONDS
except ImportError:  # lightweight API tests stub bot.sql_helper without ORM metadata
    Audit = Order = None
    BrowserAuth = None
    class LoginError(Exception):
        code = "login_required"
    def session_csrf(value):
        return ""
    LOGIN_SECONDS, SESSION_SECONDS = 300, 86400


_PROFILE_TIMEOUT_SECONDS = 3
_profile_cache = Cache(maxsize=2048, ttl=300)


class SecurePaymentRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def guarded(request):
            try:
                response = await original(request)
            except RequestValidationError:
                response = JSONResponse({"detail": "invalid_request"}, status_code=422)
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            except Exception as exc:
                _log_provider_error("request", exc)
                response = JSONResponse({"detail": "service_unavailable"}, status_code=503)
            response.headers.update({
                "Cache-Control": "no-store", "Pragma": "no-cache",
                "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
            })
            return response

        return guarded


router = APIRouter(prefix="/payments", tags=["支付"], route_class=SecurePaymentRoute)
TERMS_TEXT = "请确认套餐及兑换用途。付款成功后将发放可转赠、仅限一次兑换的兑换码。除法律强制要求或支付平台要求外，不接受因买错套餐、不再使用或已转赠等原因提出的退款申请。"


async def payment_worker():
    """Retry payment outbox/reconciliation without blocking Bot startup."""
    import asyncio
    while True:
        try:
            service = _service()
            await service.process_tasks(limit=50)
            await service.reconcile_orders()
        except Exception as exc:
            # Payment is optional; a missing key or provider outage must not
            # terminate the Telegram worker.
            _log_provider_error("worker", exc)
        await asyncio.sleep(60)


def _settings():
    from bot.payments.settings import PaymentSettings
    return PaymentSettings.from_config(config)


def _service():
    from bot.sql_helper import Session
    from bot.payments.service import PaymentError, PaymentService
    from bot.payments.stripe_gateway import StripeGateway
    settings = _settings()
    # Stripe credentials are validated at network entry points. Existing
    # order queries and fulfillment must still work while sales are paused.
    if settings.enabled:
        settings.validate()
    service = PaymentService(Session, settings, StripeGateway(settings), _notify)
    service.seed_products()
    return service


def _session_user(request: Request):
    _check_origin(request)
    session_name, browser_name = _cookie_names()
    try:
        return _auth().identity(request.cookies.get(session_name, ""), request.cookies.get(browser_name, ""))
    except LoginError:
        raise HTTPException(status_code=401, detail="login_required") from None


def _auth():
    from bot.sql_helper import Session
    from bot.payments.browser_auth import BrowserAuth
    return BrowserAuth(Session)


def _cookie_names():
    prefix = "__Host-tgbot-payment-" if _settings().cookie_secure else "tgbot-local-payment-"
    return prefix + "session", prefix + "browser"


def _check_origin(request: Request, required=False):
    origin = request.headers.get("origin")
    expected = _settings().public_url.rstrip("/")
    if ((required and not origin) or (origin and origin.rstrip("/") != expected)
            or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}):
        raise HTTPException(status_code=403, detail="origin_invalid")


def _csrf_token(request: Request, user_id: int | None = None):
    """Return a session-bound token; it is never accepted as the identity."""
    session_name, _ = _cookie_names()
    value = request.cookies.get(session_name, "")
    if not value:
        return None
    return session_csrf(value)


def _require_csrf(request: Request, user_id: int | None = None):
    _check_origin(request, required=True)
    user_id = user_id or _session_user(request)
    expected = _csrf_token(request, user_id)
    received = request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(expected, received):
        raise HTTPException(status_code=403, detail="csrf_failed")
    return user_id


def _terms_hash():
    return hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()


def _error_code(exc):
    return getattr(exc, "code", "error")


_PUBLIC_ERROR_CODES = {
    "sales_disabled", "product_changed", "terms_required", "terms_changed", "sold_out",
    "capacity_full", "no_capacity", "not_found", "code_held", "code_unavailable",
    "test_buyer_not_allowed", "code_mode_mismatch", "order_mode_mismatch",
    "stripe_mode_mismatch", "invalid_event", "event_too_large", "stripe_signature_invalid",
    "archive_not_allowed", "invalid_archive_request",
    "checkout_recovery_required",
    "invalid_channels", "wallet_requires_card", "channels_changed", "payment_channels_disabled",
    "invalid_channel_request", "payment_channel_config_invalid", "payment_channels_unavailable", "channel_mode_mismatch",
}


def _public_error(exc, fallback="service_unavailable"):
    code = _error_code(exc)
    if code in _PUBLIC_ERROR_CODES:
        return code
    from stripe import AuthenticationError, PermissionError, StripeError
    if isinstance(exc, StripeError):
        if code == "amount_too_small":
            return "stripe_amount_too_small"
        param = str(getattr(exc, "param", "") or "")
        if param == "payment_method_types" or param.startswith("payment_method_types[") \
                or param.startswith("payment_method_options") or param == "payment_method_configuration" \
                or param in {f"{key}[display_preference][preference]" for key in
                             ("card", "apple_pay", "google_pay", "alipay", "wechat_pay", "link")}:
            return "stripe_payment_methods_unavailable"
        if isinstance(exc, AuthenticationError):
            return "stripe_credentials_invalid"
        if isinstance(exc, PermissionError):
            return "stripe_permission_denied"
    return fallback


def _log_provider_error(context, exc):
    log_payment_error(LOGGER, context, exc)


class CheckoutRequest(BaseModel):
    product_id: str = Field(min_length=1, max_length=32)
    product_version: StrictInt = Field(ge=1)
    terms_version: str = Field(min_length=1, max_length=64)
    accepted: StrictBool


class ProductRequest(BaseModel):
    id: str | None = None
    title: str = Field(min_length=1, max_length=120)
    kind: str
    tier: str
    months: int = Field(ge=1, le=12)
    price_fen: int = Field(ge=0, le=99_999_999)
    version: int | None = None
    active: bool = False
    sales_limit: int | None = Field(default=None, ge=1)


class ReviewRequest(BaseModel):
    note: str = Field(min_length=3, max_length=1000)


class ArchiveRequest(BaseModel):
    order_ids: list[str] = Field(min_length=1, max_length=100)
    archived: StrictBool
    accepted: StrictBool


class ChannelSelection(BaseModel):
    model_config = {'extra': 'forbid'}
    card: StrictBool
    apple_pay: StrictBool
    google_pay: StrictBool
    alipay: StrictBool
    wechat_pay: StrictBool


class ChannelSettingsRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    mode: Literal['test', 'live']
    version: StrictInt = Field(ge=1)
    channels: ChannelSelection
    request_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    accepted: StrictBool


async def _notify(task_type, payload):
    from bot.payments.service import PaymentError
    service = _service()
    order = service.get_order(payload["order_id"])
    if task_type == "notify_code":
        code = service.reveal_code(order["id"], order["buyer_tg"])
        if not code:
            raise PaymentError("code_unavailable")
        await bot.send_message(order["buyer_tg"],
                               f"✅ 支付成功，您的兑换码：`{code}`\n订单号：`{order['id']}`\n请勿重复兑换。")
    else:
        await bot.send_message(owner, f"⚠️ 支付订单需要人工核查：`{order['id']}`")


@router.get("/terms")
async def payment_terms():
    return {"version": _settings().terms_version, "hash": _terms_hash(), "text": TERMS_TEXT}


@router.post("/auth/start")
async def payment_auth_start(request: Request):
    """Create a five minute browser-bound challenge; Telegram approves it."""
    settings = _settings()
    _check_origin(request, required=True)
    if request.headers.get("X-Payment-Request") != "1":
        raise HTTPException(status_code=403, detail="csrf_failed")
    session_name, browser_name = _cookie_names()
    try:
        challenge = _auth().start(request.cookies.get(browser_name))
    except LoginError as exc:
        raise HTTPException(status_code=429 if exc.code == "rate_limited" else 503, detail=exc.code) from None
    response = JSONResponse({
        "login_url": f"https://t.me/{bot_name}?start=paylogin_{challenge['token']}",
        "challenge_id": challenge["id"], "display_code": challenge["display_code"],
        "expires_in": LOGIN_SECONDS,
    })
    response.set_cookie(browser_name, challenge["browser_token"], max_age=LOGIN_SECONDS,
                        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/")
    return response


@router.get("/auth/poll")
async def payment_auth_poll(request: Request):
    _check_origin(request)
    if request.headers.get("X-Payment-Request") != "1":
        raise HTTPException(status_code=403, detail="csrf_failed")
    session_name, browser_name = _cookie_names()
    session_token = request.cookies.get(session_name, "")
    try:
        user_id = _auth().identity(session_token, request.cookies.get(browser_name, ""))
        return {"authenticated": True, "telegram_id": user_id}
    except LoginError:
        pass
    try:
        token = _auth().poll(request.cookies.get(browser_name, ""))
    except LoginError as exc:
        raise HTTPException(status_code=410, detail=exc.code) from None
    response = JSONResponse({"authenticated": bool(token)})
    if token:
        response.set_cookie(session_name, token, max_age=SESSION_SECONDS,
                            httponly=True, secure=_settings().cookie_secure,
                            samesite="lax", path="/")
        response.set_cookie(browser_name, request.cookies[browser_name], max_age=SESSION_SECONDS,
                            httponly=True, secure=_settings().cookie_secure,
                            samesite="lax", path="/")
    return response


async def _telegram_profile(user_id: int):
    cached = _profile_cache.get(user_id)
    if cached is not None:
        return cached
    profile = {"username": "", "display_name": "Telegram 用户"}
    try:
        user = await asyncio.wait_for(bot.get_users(user_id), timeout=_PROFILE_TIMEOUT_SECONDS)
        username = getattr(user, "username", None)
        if isinstance(username, str):
            profile["username"] = username.strip()
        names = [getattr(user, name, None) for name in ("first_name", "last_name")]
        display_name = " ".join(name.strip() for name in names if isinstance(name, str) and name.strip())
        if display_name:
            profile["display_name"] = display_name
    except Exception:
        # Profile lookup is cosmetic; Telegram outages must not break checkout.
        _profile_cache.set(user_id, profile, ttl=30)
        return profile
    _profile_cache.set(user_id, profile)
    return profile


@router.get("/me")
async def payment_me(request: Request):
    user_id = _session_user(request)
    role = "owner" if user_id == owner else ("admin" if user_id in admins else "user")
    profile = await _telegram_profile(user_id)
    return {"telegram_id": user_id, "role": role, "csrf_token": _csrf_token(request, user_id), **profile}


@router.post("/auth/logout")
async def payment_logout(request: Request):
    _require_csrf(request)
    session_name, browser_name = _cookie_names()
    try:
        _auth().logout(request.cookies.get(session_name, ""), request.cookies.get(browser_name, ""))
    except LoginError:
        pass
    response = JSONResponse({"ok": True})
    response.delete_cookie(session_name, path="/")
    response.delete_cookie(browser_name, path="/")
    return response


@router.get("/products")
async def payment_products():
    try:
        service = _service()
        return {"terms": {"version": _settings().terms_version, "hash": _terms_hash(), "text": TERMS_TEXT},
                "products": service.list_products(), "payment_channels": service.get_channels()["channels"]}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=_public_error(exc))


@router.get("/shop", response_class=HTMLResponse)
async def payment_shop():
    return HTMLResponse(render_page("shop"))


@router.get("/shop/orders/{order_id}", response_class=HTMLResponse)
async def payment_order_page(order_id: str):
    return HTMLResponse(render_page("order", {"order_id": order_id}))


@router.get("/order/{order_id}", response_class=HTMLResponse, include_in_schema=False)
async def payment_order_alias(order_id: str):
    return HTMLResponse(render_page("order", {"order_id": order_id}))


@router.get("/my-orders", response_class=HTMLResponse)
async def payment_orders_page():
    return HTMLResponse(render_page("orders"))


@router.get("/admin", response_class=HTMLResponse)
async def payment_admin_page():
    return HTMLResponse(render_page("admin"))


@router.post("/checkout")
async def create_checkout(body: CheckoutRequest, request: Request):
    buyer = _require_csrf(request)
    if body.accepted is not True:
        raise HTTPException(status_code=400, detail="请先确认购买须知")
    try:
        settings = _settings()
        if not settings.enabled:
            raise HTTPException(status_code=503, detail="sales_disabled")
        settings.validate()
        result = await _service().create_checkout(buyer, body.product_id, body.product_version,
                                                   body.terms_version, body.accepted)
        return {"order": result, "checkout_url": result["checkout_url"]}
    except HTTPException:
        raise
    except Exception as exc:
        _log_provider_error("checkout", exc)
        raise HTTPException(status_code=409 if _error_code(exc) in {"product_changed", "sold_out"} else 400,
                             detail=_public_error(exc))


@router.get("/orders")
async def payment_orders(request: Request, mode: Literal['current', 'live', 'test', 'all'] = 'current',
                         archived: Literal['active', 'archived', 'all'] = 'active'):
    buyer = _session_user(request)
    service = _service()
    selected_mode = service.mode if mode == 'current' else mode
    return {"orders": service.list_orders(buyer, mode=selected_mode,
                                         archived={'active': False, 'archived': True, 'all': None}[archived]),
            "current_mode": service.mode}


@router.get("/orders/{order_id}")
async def payment_order(order_id: str, request: Request):
    buyer = _session_user(request)
    try:
        service = _service()
        order = service.get_order(order_id, buyer)
        if order.get("payment_state") == "pending":
            service.schedule_reconcile(order_id, buyer, audit=False)
        return order
    except Exception as exc:
        raise HTTPException(status_code=404, detail=_public_error(exc, "not_found"))


@router.get("/orders/{order_id}/code")
async def payment_code(order_id: str, request: Request):
    buyer = _session_user(request)
    try:
        service = _service()
        order = service.get_order(order_id, buyer)
        if order["payment_state"] != "paid" or order["refunded"]:
            raise HTTPException(status_code=409, detail="订单尚未完成或已被冻结")
        code = service.reveal_code(order_id, buyer)
        return {"order_id": order_id, "code": code}
    except Exception as exc:
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(default="", alias="Stripe-Signature")):
    settings = _settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="支付未启用")
    raw = await request.body()
    try:
        service = _service()
        service.ingest_webhook(raw, stripe_signature)
    except Exception as exc:
        _log_provider_error("webhook", exc)
        raise HTTPException(status_code=400, detail=_public_error(exc, "stripe_signature_invalid"))
    try:
        await service.process_tasks(limit=10)
    except Exception as exc:
        # The event and task are already durable; let Stripe stop retrying the
        # same delivery while the outbox retries provider/DB work.
        _log_provider_error("webhook_tasks", exc)
    return {"received": True}


def _require_admin(request: Request):
    user_id = _session_user(request)
    if user_id != owner and user_id not in admins:
        raise HTTPException(status_code=403, detail="管理员权限不足")
    return user_id


def _require_owner(request: Request):
    user_id = _require_csrf(request)
    if user_id != owner:
        raise HTTPException(status_code=403, detail="forbidden")
    return user_id


@router.get("/admin/orders")
async def admin_orders(request: Request, mode: Literal['current', 'live', 'test', 'all'] = 'current',
                       archived: Literal['active', 'archived', 'all'] = 'active'):
    _require_admin(request)
    service = _service()
    selected_mode = service.mode if mode == 'current' else mode
    return {"orders": service.list_orders(limit=500, mode=selected_mode,
                                         archived={'active': False, 'archived': True, 'all': None}[archived]),
            "current_mode": service.mode}


@router.post('/admin/orders/archive')
async def admin_archive_orders(body: ArchiveRequest, request: Request):
    actor = _require_owner(request)
    if body.accepted is not True:
        raise HTTPException(status_code=400, detail='invalid_archive_request')
    try:
        return _service().set_orders_archived(body.order_ids, actor, archived=body.archived)
    except Exception as exc:
        _log_provider_error('request', exc)
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.get("/admin/products")
async def admin_products(request: Request):
    _require_admin(request)
    return {"products": _service().list_products(include_inactive=True)}


@router.get('/admin/channels')
async def admin_payment_channels(request: Request):
    _require_admin(request)
    return _service().get_channels()


@router.post('/admin/channels')
async def admin_save_payment_channels(body: ChannelSettingsRequest, request: Request):
    actor = _require_owner(request)
    if body.accepted is not True:
        raise HTTPException(status_code=400, detail='invalid_channel_request')
    try:
        return await _service().save_channels(actor, body.model_dump(exclude={'accepted'}))
    except Exception as exc:
        _log_provider_error('request', exc)
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.post("/admin/products")
async def admin_save_product(body: ProductRequest, request: Request):
    actor = _require_owner(request)
    try:
        return _service().save_product(actor, body.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.post("/admin/orders/{order_id}/reconcile")
async def admin_reconcile(order_id: str, request: Request):
    actor = _require_csrf(request)
    if actor != owner and actor not in admins:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        return _service().schedule_reconcile(order_id, actor)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.post("/admin/orders/{order_id}/resend")
async def admin_resend(order_id: str, request: Request):
    actor = _require_csrf(request)
    if actor != owner and actor not in admins:
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        return _service().resend_code(order_id, actor)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=_public_error(exc))


@router.post("/admin/orders/{order_id}/review")
async def admin_review(order_id: str, body: ReviewRequest, request: Request):
    actor = _require_owner(request)
    from bot.sql_helper import Session
    try:
        with Session.begin() as session:
            order = session.get(Order, order_id)
            if order is None:
                raise HTTPException(status_code=404, detail="not_found")
            order.review_required = False
            session.add(Audit(actor_tg=actor, action="order_reviewed", target_id=order_id,
                              details={"note": body.note}))
        return {"ok": True, "order_id": order_id}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=409, detail=_public_error(exc))
