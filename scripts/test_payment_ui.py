#!/usr/bin/env python3
"""Static contract checks for the CDN-style payment pages."""

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PaymentUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("payment_pages_under_test", ROOT / "bot/payments/pages.py")
        cls.pages = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.pages)
        cls.template = (ROOT / "bot/payments/templates/page.html").read_text(encoding="utf-8")
        cls.javascript = (ROOT / "bot/payments/static/payments.js").read_text(encoding="utf-8")
        cls.styles = (ROOT / "bot/payments/static/payments.css").read_text(encoding="utf-8")

    def test_all_pages_render_with_local_assets(self):
        for page in ("shop", "orders", "order", "admin"):
            with self.subTest(page=page):
                rendered = self.pages.render_page(page, {"order_id": "order-fixture"})
                self.assertIn("/payments/static/payments.css", rendered)
                self.assertIn("/payments/static/payments.js", rendered)
                self.assertIn('data-page="' + page + '"', rendered)

    def test_order_page_has_waiting_state_and_identity_slots(self):
        rendered = self.pages.render_page("order", {"order_id": "order-fixture"})
        self.assertIn('id="payment-waiting"', rendered)
        self.assertIn('class="identity-name"', rendered)
        self.assertIn('class="identity-uid"', rendered)

    def test_checkout_and_order_poll_warn_before_repeat_payment(self):
        for marker in ("正在创建支付", "正在跳转到 Stripe", "window.open", "scheduleOrderPoll", "请不要重复付款"):
            self.assertIn(marker, self.javascript)
        self.assertIn("checkout-wait.css", self.javascript)
        self.assertIn("dusheng-stripe-checkout-${suffix}", self.javascript)
        self.assertIn('!$("payment-waiting").hidden', self.javascript)
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.template)
        self.assertIn(".payment-waiting", self.styles)
        self.assertIn("animation:spin", self.styles)
        self.assertIn("checkout-wait-spin", (ROOT / "bot/payments/static/checkout-wait.css").read_text(encoding="utf-8"))

    def test_payment_code_prefix_is_distinct_from_admin_format(self):
        self.assertIn('prefix="DuSheng-Pay_"', (ROOT / "bot/payments/service.py").read_text(encoding="utf-8"))
        self.assertIn("DuSheng-Pay_", (ROOT / "bot/modules/commands/exchange.py").read_text(encoding="utf-8"))
        self.assertIn("DuSheng-...", (ROOT / "README.md").read_text(encoding="utf-8"))

    def test_payment_identity_keeps_two_line_uid_visible_on_mobile(self):
        self.assertIn(".identity{max-width:130px}", self.styles)


if __name__ == "__main__":
    unittest.main(verbosity=2)
