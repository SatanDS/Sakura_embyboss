#!/usr/bin/env python3
"""Stripe checkout contract tests without network or Bot initialization."""

import ast
import importlib.util
import itertools
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "payment_gateway_test_package"
PACKAGE = types.ModuleType(PACKAGE_NAME)
PACKAGE.__path__ = [str(ROOT / "bot" / "payments")]
sys.modules[PACKAGE_NAME] = PACKAGE
SPEC = importlib.util.spec_from_file_location(
    PACKAGE_NAME + ".stripe_gateway", ROOT / "bot" / "payments" / "stripe_gateway.py",
)
GATEWAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATEWAY)


class PaymentGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            public_url="https://pay.example.test",
            stripe_secret_key="sk_test_fixture",
            stripe_webhook_secret="whsec_fixture",
            live_mode=False,
        )
        self.order = {
            "id": "order_fixture",
            "product_snapshot": {"title": "VIP 1 month"},
            "amount_fen": 1288,
            "expires_timestamp": 2000000000,
        }
        self.client_patch = patch("stripe.StripeClient", autospec=True)
        self.client_factory = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.client = self.client_factory.return_value
        self.client.checkout = SimpleNamespace(sessions=SimpleNamespace(create=Mock(
            return_value={"id": "cs_test_fixture", "url": "https://checkout.stripe.com/fixture"},
        )))
        self.client.payment_method_configurations = SimpleNamespace(create=Mock())
        self.gateway = GATEWAY.StripeGateway(self.settings)

    def configuration_response(self, channels, **extra):
        return {
            "id": "pmc_fixture", "active": True, "livemode": False,
            **{key: {"available": enabled, "display_preference": {
                "preference": "on" if enabled else "off", "value": "on" if enabled else "off",
            }} for key, enabled in {**channels, "link": False}.items()},
            **extra,
        }

    def order_with_channels(self, channels, configuration_id="pmc_fixture"):
        return dict(self.order, payment_channels_snapshot={
            "channels": channels, "configuration_id": configuration_id, "version": 1,
        })

    async def test_return_urls_match_the_order_page_route(self):
        tree = ast.parse((ROOT / "bot" / "web" / "api" / "payment.py").read_text(encoding="utf-8"))
        router = next(
            node.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "router" for target in node.targets)
        )
        prefix = next(ast.literal_eval(item.value) for item in router.keywords if item.arg == "prefix")
        routes = {
            ast.literal_eval(decorator.args[0])
            for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router" and decorator.func.attr == "get"
        }
        self.assertIn("/shop/orders/{order_id}", routes)
        order_route = prefix + "/shop/orders/{order_id}".format(order_id=self.order["id"])
        for public_url in ("https://pay.example.test", "https://pay.example.test/"):
            with self.subTest(public_url=public_url):
                self.settings.public_url = public_url
                await self.gateway.create_checkout(self.order)
                params = self.client.checkout.sessions.create.call_args.args[0]
                expected = "https://pay.example.test" + order_route
                self.assertEqual(params["success_url"], expected)
                self.assertEqual(params["cancel_url"], expected)

    async def test_checkout_uses_cny_single_purchase_and_server_order_snapshot(self):
        response = await self.gateway.create_checkout(self.order)
        self.client_factory.assert_called_once_with("sk_test_fixture", max_network_retries=2)
        self.assertEqual(response["id"], "cs_test_fixture")
        params = self.client.checkout.sessions.create.call_args.args[0]
        self.assertEqual(params["mode"], "payment")
        self.assertEqual(params["payment_method_types"], ["alipay", "wechat_pay"])
        self.assertEqual(params["payment_method_options"], {"wechat_pay": {"client": "web"}})
        self.assertEqual(params["line_items"], [{"quantity": 1, "price_data": {
            "currency": "cny", "unit_amount": 1288, "product_data": {"name": "VIP 1 month"},
        }}])
        self.assertEqual(params["client_reference_id"], "order_fixture")
        self.assertEqual(params["metadata"], {"order_id": "order_fixture"})
        self.assertEqual(params["payment_intent_data"], {"metadata": {"order_id": "order_fixture"}})
        self.assertEqual(params["expires_at"], 2000000000)

    async def test_retry_uses_the_same_order_idempotency_key(self):
        await self.gateway.create_checkout(self.order)
        await self.gateway.create_checkout(self.order)
        other_order = dict(self.order, id="another_order")
        await self.gateway.create_checkout(other_order)
        keys = [
            call.kwargs["options"]["idempotency_key"]
            for call in self.client.checkout.sessions.create.call_args_list
        ]
        self.assertEqual(keys, ["checkout:order_fixture", "checkout:order_fixture", "checkout:another_order"])

    async def test_legacy_payload_is_unchanged_including_initial_default_snapshot(self):
        expected = {
            "mode": "payment", "client_reference_id": "order_fixture",
            "metadata": {"order_id": "order_fixture"},
            "payment_intent_data": {"metadata": {"order_id": "order_fixture"}},
            "payment_method_types": ["alipay", "wechat_pay"],
            "payment_method_options": {"wechat_pay": {"client": "web"}},
            "line_items": [{"quantity": 1, "price_data": {
                "currency": "cny", "unit_amount": 1288, "product_data": {"name": "VIP 1 month"},
            }}],
            "success_url": "https://pay.example.test/payments/shop/orders/order_fixture",
            "cancel_url": "https://pay.example.test/payments/shop/orders/order_fixture",
            "expires_at": 2000000000,
        }
        for order in (self.order, dict(self.order, payment_channels_snapshot=None),
                      self.order_with_channels(GATEWAY.LEGACY_CHANNELS, None)):
            await self.gateway.create_checkout(order)
            call = self.client.checkout.sessions.create.call_args
            self.assertEqual(json.dumps(call.args[0]), json.dumps(expected))
            self.assertEqual(call.kwargs, {"options": {"idempotency_key": "checkout:order_fixture"}})

    async def test_every_supported_channel_selection_creates_a_dedicated_configuration(self):
        for values in itertools.product((False, True), repeat=5):
            channels = dict(zip(GATEWAY.CHANNEL_KEYS, values))
            if not any(values) or not channels["card"] and (channels["apple_pay"] or channels["google_pay"]):
                continue
            with self.subTest(channels=channels):
                self.client.payment_method_configurations.create.return_value = self.configuration_response(channels)
                self.assertEqual(await self.gateway.publish_channel_configuration(channels, "channel-key"), "pmc_fixture")
                call = self.client.payment_method_configurations.create.call_args
                self.assertEqual(call.args[0], {
                    "name": "DuSheng checkout",
                    **{key: {"display_preference": {"preference": "on" if enabled else "off"}}
                       for key, enabled in channels.items()},
                    "link": {"display_preference": {"preference": "off"}},
                })
                self.assertEqual(call.kwargs, {"options": {"idempotency_key": "channel-key"}})
                await self.gateway.create_checkout(self.order_with_channels(channels))
                params = self.client.checkout.sessions.create.call_args.args[0]
                self.assertEqual(params["payment_method_configuration"], "pmc_fixture")
                self.assertNotIn("payment_method_types", params)
                self.assertNotIn("wallet_options", params)
                if channels["wechat_pay"]:
                    self.assertEqual(params["payment_method_options"], {"wechat_pay": {"client": "web"}})
                else:
                    self.assertNotIn("payment_method_options", params)

    async def test_new_checkout_retries_keep_the_order_configuration(self):
        channels = dict.fromkeys(GATEWAY.CHANNEL_KEYS, True)
        order = self.order_with_channels(channels)
        await self.gateway.create_checkout(order)
        first = self.client.checkout.sessions.create.call_args
        await self.gateway.create_checkout(order)
        self.assertEqual(first, self.client.checkout.sessions.create.call_args)
        self.assertEqual(first.kwargs, {"options": {"idempotency_key": "checkout:order_fixture"}})
        self.client.payment_method_configurations.create.assert_not_called()

    async def test_all_channels_off_never_calls_stripe(self):
        channels = dict.fromkeys(GATEWAY.CHANNEL_KEYS, False)
        self.assertIsNone(await self.gateway.publish_channel_configuration(channels, "channel-key"))
        with self.assertRaises(GATEWAY.ChannelError) as error:
            await self.gateway.create_checkout(self.order_with_channels(channels, None))
        self.assertEqual(error.exception.code, "payment_channels_unavailable")
        self.client.payment_method_configurations.create.assert_not_called()
        self.client.checkout.sessions.create.assert_not_called()

    async def test_channel_preferences_are_strict_and_wallets_require_card(self):
        malformed = [None, [], {}, {**GATEWAY.LEGACY_CHANNELS, "link": True},
                     {key: value for key, value in GATEWAY.LEGACY_CHANNELS.items() if key != "card"}]
        malformed.extend({**GATEWAY.LEGACY_CHANNELS, "card": value} for value in (0, 1, "true", None, [], {}))
        for channels in malformed:
            with self.subTest(channels=channels):
                with self.assertRaises(GATEWAY.ChannelError) as error:
                    await self.gateway.publish_channel_configuration(channels, "channel-key")
                self.assertEqual(error.exception.code, "invalid_channels")
        for wallet in ("apple_pay", "google_pay"):
            with self.assertRaises(GATEWAY.ChannelError) as error:
                await self.gateway.publish_channel_configuration({**GATEWAY.LEGACY_CHANNELS, wallet: True}, "channel-key")
            self.assertEqual(error.exception.code, "wallet_requires_card")
        self.client.payment_method_configurations.create.assert_not_called()

    async def test_malformed_or_wrong_mode_configuration_is_rejected(self):
        for invalid in ({"id": ""}, {"id": "pmc_"}, {"id": "cs_test_fixture"}, {"id": 1},
                        {"id": "pmc_x\n"}, {"id": "pmc_" + "x" * 256},
                        {"active": False}, {"active": 1}, {"livemode": True}, {"livemode": 0}):
            with self.subTest(invalid=invalid):
                self.client.payment_method_configurations.create.return_value = self.configuration_response(
                    GATEWAY.LEGACY_CHANNELS, **invalid)
                with self.assertRaises(GATEWAY.ChannelError) as error:
                    await self.gateway.publish_channel_configuration(GATEWAY.LEGACY_CHANNELS, "channel-key")
                self.assertEqual(error.exception.code, "payment_channel_config_invalid")

    async def test_missing_configuration_response_is_rejected(self):
        self.client.payment_method_configurations.create.return_value = None
        with self.assertRaises(GATEWAY.ChannelError) as error:
            await self.gateway.publish_channel_configuration(GATEWAY.LEGACY_CHANNELS, "channel-key")
        self.assertEqual(error.exception.code, "payment_channel_config_invalid")

    async def test_live_configuration_and_mode_fallback(self):
        for settings in (SimpleNamespace(live_mode=True), SimpleNamespace(mode="live")):
            self.gateway.settings = settings
            self.client.payment_method_configurations.create.return_value = self.configuration_response(
                GATEWAY.LEGACY_CHANNELS, livemode=True)
            self.assertEqual(await self.gateway.publish_channel_configuration(GATEWAY.LEGACY_CHANNELS, "channel-key"), "pmc_fixture")

    async def test_unapproved_selected_channel_is_rejected(self):
        configuration = self.configuration_response(GATEWAY.LEGACY_CHANNELS)
        configuration["alipay"]["available"] = False
        self.client.payment_method_configurations.create.return_value = configuration
        with self.assertRaises(GATEWAY.ChannelError) as error:
            await self.gateway.publish_channel_configuration(GATEWAY.LEGACY_CHANNELS, "channel-key")
        self.assertEqual(error.exception.code, "payment_channels_unavailable")

    async def test_unwanted_or_malformed_effective_channels_are_rejected(self):
        for key, value in (
            ("card", {"available": True, "display_preference": {"value": "off"}}),
            ("card", {"available": False, "display_preference": {"value": "on"}}),
            ("alipay", {"available": True, "display_preference": {"value": "off"}}),
            ("alipay", {"available": "true", "display_preference": {"value": "on"}}),
            ("link", {"available": True, "display_preference": {"value": "on"}}),
            ("klarna", {"available": True, "display_preference": {"value": "off"}}),
            ("klarna", {"available": False, "display_preference": {"value": "on"}}),
            ("alipay", None), ("alipay", {}),
        ):
            with self.subTest(key=key, value=value):
                configuration = self.configuration_response(GATEWAY.LEGACY_CHANNELS)
                configuration[key] = value
                self.client.payment_method_configurations.create.return_value = configuration
                with self.assertRaises(GATEWAY.ChannelError) as error:
                    await self.gateway.publish_channel_configuration(GATEWAY.LEGACY_CHANNELS, "channel-key")
                self.assertEqual(error.exception.code, "payment_channel_config_invalid")

    async def test_missing_or_invalid_snapshot_configuration_fails_closed(self):
        channels = {**GATEWAY.LEGACY_CHANNELS, "card": True}
        orders = [dict(self.order, payment_channels_snapshot="invalid")]
        orders.extend(self.order_with_channels(channels, value) for value in (None, "", "pmc_", "cs_test_fixture", 1))
        for order in orders:
            with self.subTest(order=order):
                with self.assertRaises(GATEWAY.ChannelError) as error:
                    await self.gateway.create_checkout(order)
                self.assertEqual(error.exception.code, "payment_channel_config_invalid")
        self.client.checkout.sessions.create.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
