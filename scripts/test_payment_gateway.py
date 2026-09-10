#!/usr/bin/env python3
"""Stripe checkout contract tests without network or Bot initialization."""

import ast
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "payment_gateway_under_test", ROOT / "bot" / "payments" / "stripe_gateway.py",
)
GATEWAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATEWAY)


class PaymentGatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            public_url="https://pay.example.test",
            stripe_secret_key="sk_test_fixture",
            stripe_webhook_secret="whsec_fixture",
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
        self.gateway = GATEWAY.StripeGateway(self.settings)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
