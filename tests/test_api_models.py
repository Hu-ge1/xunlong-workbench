import unittest

from pydantic import ValidationError

from fastapi import HTTPException

from app.main import SettingsRequest, SettingsValues, get_settings, put_settings


class ApiModelTests(unittest.TestCase):
    def test_settings_reject_invalid_ranges_and_types(self):
        for values in (
            {"scan_limit": "oops"},
            {"scan_limit": 500},
            {"auction_threshold": -1},
            {"technical_threshold": 13},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                SettingsValues.model_validate(values)

    def test_settings_accept_only_official_wecom_webhook(self):
        valid = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
        self.assertEqual(SettingsValues(wecom_webhook=valid).wecom_webhook, valid)
        for invalid in (
            "http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key",
            "https://example.com/cgi-bin/webhook/send?key=test-key",
            "https://qyapi.weixin.qq.com/cgi-bin/webhook/send",
            "http://127.0.0.1/internal",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                SettingsValues(wecom_webhook=invalid)

    def test_settings_times_match_scheduler_contract(self):
        settings = get_settings()
        self.assertEqual(settings["auction_scan_time"], "09:26")
        self.assertEqual(settings["auction_push_time"], "09:28")

    def test_push_threshold_cannot_be_lower_than_candidate_threshold(self):
        request = SettingsRequest(
            values=SettingsValues(
                rulebook_threshold=70,
                rulebook_push_threshold=60,
            )
        )
        with self.assertRaises(HTTPException) as raised:
            put_settings(request)
        self.assertEqual(raised.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
