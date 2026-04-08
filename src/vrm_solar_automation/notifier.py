from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class GmailSmtpNotifier:
    sender: str
    app_password: str
    recipients: tuple[str, ...]
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

    def send_plug_state_change_email(
        self,
        *,
        command_sent: str,
        decision_action: str,
        decision_reason: str,
        intended_is_on: bool,
        actuation_status: str,
        observed_before_is_on: bool | None,
        observed_after_is_on: bool | None,
        at_iso: str,
    ) -> None:
        self._send_email(
            subject=f"VRM plug state change: {command_sent}",
            body_lines=(
                "The VRM pump controller changed plug state.",
                f"Timestamp: {at_iso}",
                f"Decision action: {decision_action}",
                f"Decision reason: {decision_reason}",
                f"Intended target: {self._format_bool(intended_is_on)}",
                f"Command: {command_sent}",
                f"Actuation status: {actuation_status}",
                f"Observed before: {self._format_bool(observed_before_is_on)}",
                f"Observed after: {self._format_bool(observed_after_is_on)}",
            ),
        )

    def send_battery_alert_email(
        self,
        *,
        battery_soc_percent: float,
        crossed_thresholds: tuple[int, ...],
        at_iso: str,
    ) -> None:
        thresholds_text = ", ".join(f"{threshold}%" for threshold in crossed_thresholds)
        self._send_email(
            subject=f"VRM battery alert: {battery_soc_percent:.1f}% SOC",
            body_lines=(
                "The VRM battery SOC dropped below the configured alert threshold.",
                f"Timestamp: {at_iso}",
                f"Battery SOC: {battery_soc_percent:.1f}%",
                f"Thresholds crossed this run: {thresholds_text}",
            ),
        )

    def send_generator_started_email(
        self,
        *,
        generator_watts: float,
        at_iso: str,
    ) -> None:
        self._send_email(
            subject=f"VRM generator alert: {generator_watts:.0f} W detected",
            body_lines=(
                "The VRM controller detected generator power.",
                f"Timestamp: {at_iso}",
                f"Generator power: {generator_watts:.0f} W",
            ),
        )

    def send_weather_blocked_alert_email(
        self,
        *,
        at_iso: str,
        local_date: str,
        weather_mode: str,
        decision_reason: str,
        today_sunshine_hours: float | None,
        tomorrow_sunshine_hours: float | None,
        night_reference_sunshine_hours: float | None,
    ) -> None:
        self._send_email(
            subject=f"VRM weather block alert: automation OFF on {local_date}",
            body_lines=(
                "The VRM controller kept the pump OFF due to weather forecast conditions.",
                f"Timestamp: {at_iso}",
                f"Local weather date: {local_date}",
                f"Weather mode: {weather_mode}",
                f"Decision reason: {decision_reason}",
                f"Today's sunshine hours: {self._format_hours(today_sunshine_hours)}",
                f"Tomorrow's sunshine hours: {self._format_hours(tomorrow_sunshine_hours)}",
                "Night reference sunshine hours: "
                f"{self._format_hours(night_reference_sunshine_hours)}",
            ),
        )

    def _send_email(
        self,
        *,
        subject: str,
        body_lines: tuple[str, ...],
    ) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Subject"] = subject
        message.set_content("\n".join(body_lines))

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(self.sender, self.app_password)
            smtp.send_message(message)

    @staticmethod
    def _format_bool(value: bool | None) -> str:
        if value is None:
            return "unknown"
        return "ON" if value else "OFF"

    @staticmethod
    def _format_hours(value: float | None) -> str:
        if value is None:
            return "unknown"
        return f"{value:.1f} h"
