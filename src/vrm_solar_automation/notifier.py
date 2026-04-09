from __future__ import annotations

import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class GmailSmtpNotifier:
    sender: str
    app_password: str
    recipients: tuple[str, ...]
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    display_timezone: str = "UTC"

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
        formatted_time = self._format_local_timestamp(at_iso)
        command_label = self._humanize_command(command_sent)
        self._send_email(
            subject=f"VRM Pump Update: {self._humanize_command_subject(command_sent)}",
            body_lines=self._build_body(
                headline="Pump state was updated by automation.",
                fields=(
                    ("Time", formatted_time),
                    ("Action", self._humanize_token(decision_action)),
                    ("Reason", decision_reason),
                    ("Requested state", self._format_bool(intended_is_on)),
                    ("Command sent", command_label),
                    ("Result", self._humanize_token(actuation_status)),
                    ("Plug before", self._format_bool(observed_before_is_on)),
                    ("Plug after", self._format_bool(observed_after_is_on)),
                ),
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
            subject=f"VRM Alert: Battery SOC low ({battery_soc_percent:.1f}%)",
            body_lines=self._build_body(
                headline="Battery SOC dropped below the configured alert thresholds.",
                fields=(
                    ("Time", self._format_local_timestamp(at_iso)),
                    ("Battery SOC", f"{battery_soc_percent:.1f}%"),
                    ("Thresholds crossed", thresholds_text),
                ),
            ),
        )

    def send_generator_started_email(
        self,
        *,
        generator_watts: float,
        at_iso: str,
    ) -> None:
        self._send_email(
            subject="VRM Alert: Generator power detected",
            body_lines=self._build_body(
                headline="Generator power was detected by automation.",
                fields=(
                    ("Time", self._format_local_timestamp(at_iso)),
                    ("Generator power", f"{generator_watts:.0f} W"),
                ),
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
            subject=f"VRM Update: Automation OFF due to weather ({local_date})",
            body_lines=self._build_body(
                headline="Automation stayed OFF because forecast conditions were not favorable.",
                fields=(
                    ("Time", self._format_local_timestamp(at_iso)),
                    ("Date", local_date),
                    ("Forecast mode", self._humanize_token(weather_mode)),
                    ("Reason", decision_reason),
                    ("Today's sunshine", self._format_hours(today_sunshine_hours)),
                    ("Tomorrow's sunshine", self._format_hours(tomorrow_sunshine_hours)),
                    ("Night reference sunshine", self._format_hours(night_reference_sunshine_hours)),
                ),
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
            return "Unknown"
        return "On" if value else "Off"

    @staticmethod
    def _format_hours(value: float | None) -> str:
        if value is None:
            return "n/a"
        return f"{value:.1f} h"

    def _format_local_timestamp(self, at_iso: str) -> str:
        try:
            timestamp = datetime.fromisoformat(at_iso)
        except ValueError:
            return at_iso
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(self._display_zone()).strftime("%Y-%m-%d %H:%M")

    def _display_zone(self):
        try:
            return ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError:
            return UTC

    @staticmethod
    def _build_body(
        *,
        headline: str,
        fields: tuple[tuple[str, str], ...],
    ) -> tuple[str, ...]:
        return (headline, *(f"- {label}: {value}" for label, value in fields))

    @staticmethod
    def _humanize_command(command_sent: str) -> str:
        normalized = command_sent.strip().lower()
        return {
            "turn_on": "Turn On",
            "plug_on": "Turn On",
            "turn_off": "Turn Off",
            "plug_off": "Turn Off",
        }.get(normalized, command_sent.replace("_", " ").strip().title())

    @staticmethod
    def _humanize_command_subject(command_sent: str) -> str:
        normalized = command_sent.strip().lower()
        return {
            "turn_on": "Pump turned ON",
            "plug_on": "Pump turned ON",
            "turn_off": "Pump turned OFF",
            "plug_off": "Pump turned OFF",
        }.get(normalized, f"Pump command: {command_sent.replace('_', ' ').strip().title()}")

    @staticmethod
    def _humanize_token(value: str) -> str:
        normalized = value.strip().lower()
        mapping = {
            "turn_on": "Turn On",
            "turn_off": "Turn Off",
            "keep_on": "Keep On",
            "keep_off": "Keep Off",
            "already_aligned": "Already aligned",
            "reconciled": "Applied successfully",
            "command_sent_unverified": "Command sent (pending verification)",
            "mismatch_after_command": "Final state mismatch",
            "blocked_quiet_hours": "Blocked by quiet hours",
            "no_target_change": "No target change",
            "unreachable": "Plug unreachable",
            "unknown": "Unknown",
            "skipped": "Skipped",
            "insufficient_sun": "Insufficient sunshine forecast",
            "sufficient_sun": "Sufficient sunshine forecast",
            "surplus_night": "Surplus night mode",
        }
        return mapping.get(normalized, value.replace("_", " ").strip().title())
