"""Official Stripe transport; network operations never block the async event loop."""

import asyncio


class StripeGateway:
    def __init__(self, settings):
        import stripe
        self.stripe = stripe
        self.settings = settings
        self.client = stripe.StripeClient(settings.stripe_secret_key, max_network_retries=2)

    @staticmethod
    def _plain(value):
        return value.to_dict_recursive() if hasattr(value, "to_dict_recursive") else dict(value)

    def verify_event(self, raw, signature):
        return self._plain(self.stripe.Webhook.construct_event(
            raw, signature, self.settings.stripe_webhook_secret, tolerance=300,
        ))

    async def create_checkout(self, order):
        product = order["product_snapshot"]
        base = self.settings.public_url.rstrip("/")
        params = {
            "mode": "payment", "client_reference_id": order["id"],
            "metadata": {"order_id": order["id"]},
            "payment_intent_data": {"metadata": {"order_id": order["id"]}},
            "payment_method_types": ["alipay", "wechat_pay"],
            "payment_method_options": {"wechat_pay": {"client": "web"}},
            "line_items": [{"quantity": 1, "price_data": {
                "currency": "cny", "unit_amount": order["amount_fen"],
                "product_data": {"name": product["title"]},
            }}],
            "success_url": base + "/shop/orders/" + order["id"],
            "cancel_url": base + "/shop/orders/" + order["id"],
            "expires_at": order["expires_timestamp"],
        }
        response = await asyncio.to_thread(
            self.client.checkout.sessions.create, params,
            options={"idempotency_key": "checkout:" + order["id"]},
        )
        return self._plain(response)

    async def retrieve_checkout(self, session_id):
        response = await asyncio.to_thread(
            self.client.checkout.sessions.retrieve, session_id,
            {"expand": ["payment_intent.latest_charge"]},
        )
        result = self._plain(response)
        intent = result.get("payment_intent")
        charge = intent.get("latest_charge") if isinstance(intent, dict) else None
        result["charge_refunded"] = bool(isinstance(charge, dict) and (
            charge.get("refunded") or int(charge.get("amount_refunded") or 0) > 0))
        result["dispute_status"] = None
        if isinstance(charge, dict) and charge.get("disputed"):
            disputes = await asyncio.to_thread(
                self.client.disputes.list, {"charge": charge["id"], "limit": 10},
            )
            values = self._plain(disputes).get("data", [])
            statuses = [item["status"] for item in values]
            result["dispute_status"] = next(
                (s for s in statuses if s not in {"won", "warning_closed"}),
                statuses[0] if statuses else "unknown",
            )
        return result
