"""Payment diagnostics retain metadata but never credentials or response bodies."""

import importlib.util
import unittest
from pathlib import Path

from loguru import logger
import stripe


SOURCE = Path(__file__).resolve().parents[1] / "bot/payments/diagnostics.py"
SPEC = importlib.util.spec_from_file_location("payment_diagnostics_test", SOURCE)
DIAGNOSTICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTICS)


class PaymentDiagnosticTests(unittest.TestCase):
    def test_provider_metadata_is_rendered_without_message_or_payload(self):
        secret = "sk_test_DoNotLogThisCredential"
        error = stripe.InvalidRequestError(
            secret, param="payment_method_types[1]", code="parameter_invalid_enum",
            http_status=400, headers={"request-id": "req_fixture123", "Authorization": secret},
            http_body=secret, json_body={"error": {"message": secret}},
        )
        messages = []
        # The offline runner stubs logger.add to prevent production file sinks.
        sink = type(logger).add(logger, messages.append, format="{message}")
        try:
            DIAGNOSTICS.log_payment_error(logger, "checkout", error)
        finally:
            logger.remove(sink)
        self.assertEqual(len(messages), 1)
        message = str(messages[0])
        self.assertIn("type=InvalidRequestError", message)
        self.assertIn("code=parameter_invalid_enum", message)
        self.assertIn("param=payment_method_types[1]", message)
        self.assertIn("http_status=400 request_id=req_fixture123", message)
        self.assertNotIn(secret, message)
        self.assertNotIn("%s", message)

    def test_untrusted_metadata_cannot_inject_logs_or_secrets(self):
        for value in ("sk_test_fake", "pk_test_fake", "whsec_fake", "Pay_fake", "x\nspoofed",
                      "a" * 1000, {"secret": "not-a-string"}):
            with self.subTest(value_type=type(value).__name__):
                error = ValueError("do-not-log")
                error.code = error.param = error.request_id = error.http_status = value
                fields = DIAGNOSTICS.payment_error_fields(error)
                self.assertEqual(fields, {"type": "ValueError", "code": "-", "param": "-",
                                          "request_id": "-", "http_status": "-"})

    def test_exception_without_provider_metadata_still_has_type(self):
        fields = DIAGNOSTICS.payment_error_fields(TypeError("sensitive-argument"))
        self.assertEqual(fields["type"], "TypeError")
        self.assertEqual(fields["code"], "-")
        self.assertNotIn("sensitive-argument", str(fields))

    def test_channel_parameters_remain_allowlisted_without_configuration_values(self):
        for param in ("payment_method_configuration", "apple_pay[display_preference][preference]",
                      "google_pay[display_preference][preference]", "card[display_preference][preference]"):
            with self.subTest(param=param):
                error = stripe.InvalidRequestError("private-response", param=param)
                self.assertEqual(DIAGNOSTICS.payment_error_fields(error)["param"], param)
        error = stripe.InvalidRequestError("private-response", param="payment_method_configuration[pmc_private]")
        self.assertEqual(DIAGNOSTICS.payment_error_fields(error)["param"], "-")

    def test_operation_names_are_allowlisted(self):
        messages = []
        sink = type(logger).add(logger, messages.append, format="{message}")
        try:
            DIAGNOSTICS.log_payment_error(logger, "task_create_checkout", ValueError("private"))
            DIAGNOSTICS.log_payment_error(logger, "sk_test_secret\nforged", ValueError("private"))
        finally:
            logger.remove(sink)
        self.assertIn("operation=task_create_checkout", str(messages[0]))
        self.assertIn("operation=unknown", str(messages[1]))
        self.assertNotIn("sk_test_secret", str(messages))
        self.assertNotIn("forged", str(messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
