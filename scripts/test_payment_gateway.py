#!/usr/bin/env python3
"""Stripe checkout contract tests without network or Bot initialization."""

import ast
import importlib.util
import itertools
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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
            "created_at": datetime.fromtimestamp(1999998200, timezone.utc).replace(tzinfo=None),
        }
        self.client_patch = patch("stripe.StripeClient", autospec=True)
        self.client_factory = self.client_patch.start()
        self.addCleanup(self.client_patch.stop)
        self.client = self.client_factory.return_value
        self.client.checkout = SimpleNamespace(sessions=SimpleNamespace(
            create=Mock(return_value={"id": "cs_test_fixture", "url": "https://checkout.stripe.com/fixture"}),
            list=Mock(return_value={"data": [], "has_more": False}),
        ))
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

    async def test_recovery_lists_only_the_fixed_order_window_without_creating_objects(self):
        self.assertIsNone(await self.gateway.find_checkout(self.order))
        self.client.checkout.sessions.list.assert_called_once_with({
            "created": {"gte": 1999997900, "lte": 2000000300}, "limit": 100,
        })
        self.client.checkout.sessions.create.assert_not_called()
        self.client.payment_method_configurations.create.assert_not_called()

    async def test_recovery_handles_aware_and_naive_utc_creation_dates_consistently(self):
        await self.gateway.find_checkout(self.order)
        expected = self.client.checkout.sessions.list.call_args
        local_time = self.order["created_at"].replace(tzinfo=timezone.utc).astimezone(
            timezone(timedelta(hours=8)))
        await self.gateway.find_checkout(dict(self.order, created_at=local_time))
        self.assertEqual(self.client.checkout.sessions.list.call_args, expected)

    async def test_recovery_can_filter_by_known_payment_intent(self):
        await self.gateway.find_checkout(dict(self.order, stripe_payment_intent_id="pi_fixture"))
        self.assertEqual(self.client.checkout.sessions.list.call_args.args[0], {
            "created": {"gte": 1999997900, "lte": 2000000300},
            "limit": 100, "payment_intent": "pi_fixture",
        })

    async def test_recovery_scans_all_pages_after_a_candidate_and_preserves_query_params(self):
        self.client.checkout.sessions.list.side_effect = [
            {"data": [{"id": "cs_test_first", "client_reference_id": self.order["id"]}], "has_more": True},
            {"data": [{"id": "cs_test_other", "metadata": {"order_id": "another_order"}}], "has_more": True},
            {"data": [], "has_more": False},
        ]
        self.assertEqual(await self.gateway.find_checkout(self.order), "cs_test_first")
        base = {"created": {"gte": 1999997900, "lte": 2000000300}, "limit": 100}
        self.assertEqual([call.args[0] for call in self.client.checkout.sessions.list.call_args_list], [
            base, {**base, "starting_after": "cs_test_first"}, {**base, "starting_after": "cs_test_other"},
        ])
        self.client.checkout.sessions.create.assert_not_called()

    async def test_recovery_does_not_filter_by_status_or_require_both_references(self):
        references = [
            {"client_reference_id": self.order["id"]},
            {"metadata": {"order_id": self.order["id"]}},
            {"client_reference_id": self.order["id"], "metadata": {"order_id": "wrong_order"}},
            {"client_reference_id": "wrong_order", "metadata": {"order_id": self.order["id"]}},
            {"client_reference_id": self.order["id"], "metadata": None},
        ]
        for status, payment_status in (("complete", "paid"), ("expired", "unpaid"), ("open", "unpaid")):
            for reference in references:
                with self.subTest(status=status, reference=reference):
                    self.client.checkout.sessions.list.return_value = {"data": [{
                        "id": "cs_test_candidate", "status": status, "payment_status": payment_status, **reference,
                    }], "has_more": False}
                    self.assertEqual(await self.gateway.find_checkout(self.order), "cs_test_candidate")
        self.client.checkout.sessions.create.assert_not_called()

    async def test_recovery_returns_none_when_all_results_belong_to_other_orders(self):
        self.client.checkout.sessions.list.return_value = {"data": [
            {"id": "cs_test_other", "client_reference_id": "another_order", "metadata": {"order_id": "other"}},
            {"id": "cs_test_unrelated", "metadata": {}},
        ], "has_more": False}
        self.assertIsNone(await self.gateway.find_checkout(self.order))

    async def test_recovery_rejects_multiple_candidates_on_one_page_or_across_pages(self):
        first = {"id": "cs_test_first", "client_reference_id": self.order["id"]}
        second = {"id": "cs_test_second", "metadata": {"order_id": self.order["id"]}}
        for pages in ([{"data": [first, second], "has_more": False}], [
                {"data": [first], "has_more": True}, {"data": [second], "has_more": False}]):
            self.client.checkout.sessions.list.side_effect = pages
            with self.assertRaises(GATEWAY.CheckoutRecoveryError) as error:
                await self.gateway.find_checkout(self.order)
            self.assertEqual(error.exception.code, "checkout_recovery_ambiguous")
            self.assertEqual(str(error.exception), "checkout_recovery_ambiguous")

    async def test_recovery_rejects_malformed_results_and_repeated_pagination(self):
        malformed = [
            None, {}, {"data": [], "has_more": 0}, {"data": None, "has_more": False},
            {"data": [], "has_more": True}, {"data": [None], "has_more": False},
            {"data": [{"id": "pi_wrong"}], "has_more": False},
            {"data": [{"id": "cs_test_"}], "has_more": False},
            {"data": [{"id": "cs_test_" + "x" * 256}], "has_more": False},
            {"data": [{"id": "cs_test_bad", "metadata": "invalid"}], "has_more": False},
            {"data": [{"id": "cs_test_bad", "client_reference_id": 123}], "has_more": False},
            {"data": [{"id": "cs_test_" + str(i)} for i in range(101)], "has_more": False},
            SimpleNamespace(to_dict_recursive=lambda: []),
        ]
        for response in malformed:
            with self.subTest(response=response):
                self.client.checkout.sessions.list.return_value = response
                with self.assertRaises(GATEWAY.CheckoutRecoveryError) as error:
                    await self.gateway.find_checkout(self.order)
                self.assertEqual(str(error.exception), "checkout_recovery_incomplete")
        self.client.checkout.sessions.list.side_effect = [
            {"data": [{"id": "cs_test_repeat"}], "has_more": True},
            {"data": [{"id": "cs_test_repeat"}], "has_more": False},
        ]
        with self.assertRaises(GATEWAY.CheckoutRecoveryError) as error:
            await self.gateway.find_checkout(self.order)
        self.assertEqual(error.exception.code, "checkout_recovery_incomplete")

    async def test_recovery_api_failures_never_turn_into_a_missing_session(self):
        for pages in ([RuntimeError("Stripe unavailable")], [
                {"data": [{"id": "cs_test_found", "client_reference_id": self.order["id"]}], "has_more": True},
                RuntimeError("Stripe unavailable")]):
            self.client.checkout.sessions.list.side_effect = pages
            with self.assertRaisesRegex(RuntimeError, "Stripe unavailable"):
                await self.gateway.find_checkout(self.order)
        self.client.checkout.sessions.create.assert_not_called()

    async def test_recovery_enforces_page_budget_even_after_a_candidate(self):
        self.client.checkout.sessions.list.side_effect = [
            {"data": [{"id": "cs_test_" + str(i), "client_reference_id": self.order["id"] if i == 0 else None}],
             "has_more": True} for i in range(21)
        ]
        with self.assertRaises(GATEWAY.CheckoutRecoveryError) as error:
            await self.gateway.find_checkout(self.order)
        self.assertEqual(error.exception.code, "checkout_recovery_incomplete")
        self.assertEqual(self.client.checkout.sessions.list.call_count, 20)
        self.client.checkout.sessions.create.assert_not_called()

    async def test_recovery_accepts_a_complete_twentieth_page(self):
        self.client.checkout.sessions.list.side_effect = [
            {"data": [{"id": "cs_test_" + str(i), "metadata": {"order_id": self.order["id"] if i == 19 else "other"}}],
             "has_more": i < 19} for i in range(20)
        ]
        self.assertEqual(await self.gateway.find_checkout(self.order), "cs_test_19")
        self.assertEqual(self.client.checkout.sessions.list.call_count, 20)

    async def test_recovery_rejects_invalid_order_bounds_without_querying_stripe(self):
        for fields in ({"created_at": None}, {"created_at": "invalid"}, {"expires_timestamp": True},
                       {"expires_timestamp": 1}, {"id": None}, {"stripe_payment_intent_id": "bad"}):
            with self.subTest(fields=fields):
                with self.assertRaises(GATEWAY.CheckoutRecoveryError) as error:
                    await self.gateway.find_checkout(dict(self.order, **fields))
                self.assertEqual(error.exception.code, "checkout_recovery_incomplete")
        self.client.checkout.sessions.list.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
