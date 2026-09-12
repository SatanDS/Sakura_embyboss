"""Official Stripe transport; network operations never block the async event loop."""

import asyncio
import re
from datetime import datetime, timezone

from .channels import CHANNEL_KEYS, LEGACY_CHANNELS, ChannelError, normalize_channels


class CheckoutRecoveryError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


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

    async def publish_channel_configuration(self, channels, idempotency_key):
        channels = normalize_channels(channels)
        if not any(channels.values()):
            return None
        params = {
            "name": "DuSheng checkout",
            **{key: {"display_preference": {"preference": "on" if value else "off"}}
               for key, value in channels.items()},
            "link": {"display_preference": {"preference": "off"}},
        }
        response = await asyncio.to_thread(
            self.client.payment_method_configurations.create, params,
            options={"idempotency_key": idempotency_key},
        )
        try:
            configuration = self._plain(response)
        except (TypeError, ValueError):
            raise ChannelError("payment_channel_config_invalid") from None
        expected_live = getattr(self.settings, "live_mode", None)
        if expected_live is None:
            expected_live = getattr(self.settings, "mode", "test") == "live"
        configuration_id = configuration.get("id")
        if (not isinstance(configuration_id, str) or len(configuration_id) > 255
                or not re.fullmatch(r"pmc_[A-Za-z0-9]+", configuration_id)
                or configuration.get("active") is not True
                or configuration.get("livemode") is not expected_live):
            raise ChannelError("payment_channel_config_invalid")
        for key in (*CHANNEL_KEYS, "link"):
            value = configuration.get(key)
            enabled = channels.get(key, False)
            if not isinstance(value, dict):
                raise ChannelError("payment_channel_config_invalid")
            preference = value.get("display_preference")
            if (not isinstance(preference, dict)
                    or preference.get("value") != ("on" if enabled else "off")
                    or type(value.get("available")) is not bool):
                raise ChannelError("payment_channel_config_invalid")
            if enabled and value["available"] is not True:
                raise ChannelError("payment_channels_unavailable")
            if not enabled and value["available"] is not False:
                raise ChannelError("payment_channel_config_invalid")
        # Never let Stripe defaults expose a channel absent from our switches.
        for key, value in configuration.items():
            if key not in CHANNEL_KEYS and isinstance(value, dict):
                preference = value.get("display_preference")
                if (value.get("available") is True
                        or isinstance(preference, dict) and preference.get("value") == "on"):
                    raise ChannelError("payment_channel_config_invalid")
        return configuration_id

    async def create_checkout(self, order):
        snapshot = order.get("payment_channels_snapshot")
        channel_params = {
            "payment_method_types": ["alipay", "wechat_pay"],
            "payment_method_options": {"wechat_pay": {"client": "web"}},
        }
        if snapshot is not None:
            if not isinstance(snapshot, dict):
                raise ChannelError("payment_channel_config_invalid")
            channels = normalize_channels(snapshot.get("channels"))
            if not any(channels.values()):
                raise ChannelError("payment_channels_unavailable")
            configuration_id = snapshot.get("configuration_id")
            if configuration_id is not None:
                if (not isinstance(configuration_id, str)
                        or len(configuration_id) > 255
                        or not re.fullmatch(r"pmc_[A-Za-z0-9]+", configuration_id)):
                    raise ChannelError("payment_channel_config_invalid")
                channel_params = {"payment_method_configuration": configuration_id}
                if channels["wechat_pay"]:
                    channel_params["payment_method_options"] = {"wechat_pay": {"client": "web"}}
            elif channels != LEGACY_CHANNELS:
                raise ChannelError("payment_channel_config_invalid")
        product = order["product_snapshot"]
        base = self.settings.public_url.rstrip("/")
        params = {
            "mode": "payment", "client_reference_id": order["id"],
            "metadata": {"order_id": order["id"]},
            "payment_intent_data": {"metadata": {"order_id": order["id"]}},
            **channel_params,
            "line_items": [{"quantity": 1, "price_data": {
                "currency": "cny", "unit_amount": order["amount_fen"],
                "product_data": {"name": product["title"]},
            }}],
            "success_url": base + "/payments/shop/orders/" + order["id"],
            "cancel_url": base + "/payments/shop/orders/" + order["id"],
            "expires_at": order["expires_timestamp"],
        }
        response = await asyncio.to_thread(
            self.client.checkout.sessions.create, params,
            options={"idempotency_key": "checkout:" + order["id"]},
        )
        return self._plain(response)

    async def find_checkout(self, order) -> str | None:
        created_at = order.get("created_at")
        expires_at = order.get("expires_timestamp")
        order_id = order.get("id")
        if (not isinstance(created_at, datetime) or type(expires_at) is not int
                or not isinstance(order_id, str) or not order_id):
            raise CheckoutRecoveryError("checkout_recovery_incomplete")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_timestamp = int(created_at.timestamp())
        if created_timestamp < 0 or expires_at < created_timestamp:
            raise CheckoutRecoveryError("checkout_recovery_incomplete")
        params = {
            "created": {"gte": created_timestamp - 300, "lte": expires_at + 300},
            "limit": 100,
        }
        payment_intent = order.get("stripe_payment_intent_id")
        if payment_intent:
            if (not isinstance(payment_intent, str) or len(payment_intent) > 255
                    or not re.fullmatch(r"pi_[A-Za-z0-9]+", payment_intent)):
                raise CheckoutRecoveryError("checkout_recovery_incomplete")
            params["payment_intent"] = payment_intent
        candidate = None
        seen = set()
        # Scan to completion even after a match; another session must not be
        # silently discarded when deciding which remote payment to reconcile.
        for _ in range(20):
            response = await asyncio.to_thread(self.client.checkout.sessions.list, params)
            try:
                page = self._plain(response)
            except (TypeError, ValueError):
                raise CheckoutRecoveryError("checkout_recovery_incomplete") from None
            if not isinstance(page, dict):
                raise CheckoutRecoveryError("checkout_recovery_incomplete")
            data, has_more = page.get("data"), page.get("has_more")
            if (not isinstance(data, list) or len(data) > 100 or type(has_more) is not bool
                    or has_more and not data):
                raise CheckoutRecoveryError("checkout_recovery_incomplete")
            for session in data:
                if not isinstance(session, dict):
                    raise CheckoutRecoveryError("checkout_recovery_incomplete")
                session_id = session.get("id")
                metadata = session.get("metadata")
                reference = session.get("client_reference_id")
                if (not isinstance(session_id, str) or len(session_id) > 255
                        or not re.fullmatch(r"cs_(?:test_|live_)?[A-Za-z0-9]+", session_id)
                        or session_id in seen
                        or metadata is not None and not isinstance(metadata, dict)
                        or reference is not None and not isinstance(reference, str)):
                    raise CheckoutRecoveryError("checkout_recovery_incomplete")
                seen.add(session_id)
                if reference == order_id or (metadata or {}).get("order_id") == order_id:
                    if candidate is not None:
                        raise CheckoutRecoveryError("checkout_recovery_ambiguous")
                    candidate = session_id
            if not has_more:
                return candidate
            params = {**params, "starting_after": data[-1]["id"]}
        raise CheckoutRecoveryError("checkout_recovery_incomplete")

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
