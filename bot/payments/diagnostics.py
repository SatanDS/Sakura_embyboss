"""Safe metadata for diagnosing payment failures without request contents."""

import re


_SECRET = re.compile(r"(?:sk_|pk_|rk_|whsec_|pay_|cs_|pi_|bearer)", re.IGNORECASE)
_CHECKOUT_PARAM = re.compile(
    r"(?:amount|currency|expires_at|mode|success_url|cancel_url|payment_method_types(?:\[\d+\])?"
    r"|payment_method_options\[wechat_pay\]\[client\]"
    r"|line_items\[\d+\](?:\[quantity\]|\[price_data\]\[(?:unit_amount|currency)\]))"
)
_OPERATIONS = frozenset({
    "request", "worker", "checkout", "webhook", "webhook_tasks",
    "task_create_checkout", "task_stripe_event", "task_reconcile_order",
    "task_fulfill_order", "task_notify_code", "task_notify_review",
})


def _field(value, pattern):
    if (not isinstance(value, str) or len(value) > 120 or _SECRET.search(value)
            or not re.fullmatch(pattern, value)):
        return "-"
    return value


def payment_error_fields(exc):
    status = getattr(exc, "http_status", None)
    return {
        "type": _field(type(exc).__name__, r"[A-Za-z][A-Za-z0-9]{0,63}"),
        "code": _field(getattr(exc, "code", None), r"[a-z][a-z0-9_]{0,63}"),
        "param": _field(getattr(exc, "param", None), _CHECKOUT_PARAM),
        "http_status": status if type(status) is int and 400 <= status <= 599 else "-",
        "request_id": _field(getattr(exc, "request_id", None), r"req_[A-Za-z0-9]{1,100}"),
    }


def log_payment_error(logger, operation, exc):
    fields = payment_error_fields(exc)
    operation = operation if isinstance(operation, str) and operation in _OPERATIONS else "unknown"
    logger.error("payment_failure operation={} type={} code={} param={} http_status={} request_id={}",
                 operation, fields["type"], fields["code"], fields["param"],
                 fields["http_status"], fields["request_id"])
