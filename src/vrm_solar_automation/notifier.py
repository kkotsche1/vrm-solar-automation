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
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Subject"] = f"VRM plug state change: {command_sent}"
        message.set_content(
            "\n".join(
                (
                    "The VRM pump controller changed plug state.",
                    f"Timestamp: {at_iso}",
                    f"Decision action: {decision_action}",
                    f"Decision reason: {decision_reason}",
                    f"Intended target: {self._format_bool(intended_is_on)}",
                    f"Command: {command_sent}",
                    f"Actuation status: {actuation_status}",
                    f"Observed before: {self._format_bool(observed_before_is_on)}",
                    f"Observed after: {self._format_bool(observed_after_is_on)}",
                )
            )
        )

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(self.sender, self.app_password)
            smtp.send_message(message)

    @staticmethod
    def _format_bool(value: bool | None) -> str:
        if value is None:
            return "unknown"
        return "ON" if value else "OFF"
