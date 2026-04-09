from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from vrm_solar_automation.notifier import GmailSmtpNotifier


class GmailSmtpNotifierTests(unittest.TestCase):
    def _build_notifier(self) -> GmailSmtpNotifier:
        return GmailSmtpNotifier(
            sender="alerts@example.com",
            app_password="app-password",
            recipients=("user@example.com",),
            display_timezone="Europe/Madrid",
        )

    @staticmethod
    def _extract_message(mock_smtp: MagicMock):
        smtp_instance = mock_smtp.return_value.__enter__.return_value
        return smtp_instance.send_message.call_args.args[0]

    @patch("vrm_solar_automation.notifier.smtplib.SMTP")
    def test_plug_state_email_uses_human_friendly_subject_and_body(self, mock_smtp: MagicMock) -> None:
        notifier = self._build_notifier()

        notifier.send_plug_state_change_email(
            command_sent="turn_on",
            decision_action="turn_on",
            decision_reason="Battery SOC recovered above the target threshold.",
            intended_is_on=True,
            actuation_status="reconciled",
            observed_before_is_on=False,
            observed_after_is_on=True,
            at_iso="2026-01-10T12:34:56+00:00",
        )

        message = self._extract_message(mock_smtp)
        body = message.get_content()

        self.assertEqual(message["Subject"], "VRM Pump Update: Pump turned ON")
        self.assertIn("- Time: 2026-01-10 13:34", body)
        self.assertIn("- Action: Turn On", body)
        self.assertIn("- Command sent: Turn On", body)
        self.assertIn("- Result: Applied successfully", body)
        self.assertNotIn("turn_on", body)
        self.assertNotIn("+00:00", body)

    @patch("vrm_solar_automation.notifier.smtplib.SMTP")
    def test_battery_alert_email_is_concise_list_format(self, mock_smtp: MagicMock) -> None:
        notifier = self._build_notifier()

        notifier.send_battery_alert_email(
            battery_soc_percent=34.2,
            crossed_thresholds=(40, 35),
            at_iso="2026-01-10T12:34:56+00:00",
        )

        message = self._extract_message(mock_smtp)
        body = message.get_content()

        self.assertEqual(message["Subject"], "VRM Alert: Battery SOC low (34.2%)")
        self.assertIn("- Time: 2026-01-10 13:34", body)
        self.assertIn("- Battery SOC: 34.2%", body)
        self.assertIn("- Thresholds crossed: 40%, 35%", body)

    @patch("vrm_solar_automation.notifier.smtplib.SMTP")
    def test_weather_blocked_email_uses_friendly_mode_label(self, mock_smtp: MagicMock) -> None:
        notifier = self._build_notifier()

        notifier.send_weather_blocked_alert_email(
            at_iso="2026-01-10T12:34:56+00:00",
            local_date="2026-01-10",
            weather_mode="insufficient_sun",
            decision_reason="Today's sunshine forecast is below the minimum threshold.",
            today_sunshine_hours=3.2,
            tomorrow_sunshine_hours=7.1,
            night_reference_sunshine_hours=None,
        )

        message = self._extract_message(mock_smtp)
        body = message.get_content()

        self.assertEqual(
            message["Subject"],
            "VRM Update: Automation OFF due to weather (2026-01-10)",
        )
        self.assertIn("- Time: 2026-01-10 13:34", body)
        self.assertIn("- Forecast mode: Insufficient sunshine forecast", body)
        self.assertIn("- Today's sunshine: 3.2 h", body)
        self.assertIn("- Night reference sunshine: n/a", body)


if __name__ == "__main__":
    unittest.main()
