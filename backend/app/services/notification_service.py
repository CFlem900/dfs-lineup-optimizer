"""Notification service for DFS analytics — email + SMS alerts.

Email: Sends slate reports (CSV, XLSX) as email attachments via SMTP.
SMS:   Sends concise text alerts via Twilio for time-critical events
       (late swaps, sim completion, alpha vacuums, etc.).

Configuration (via .env):
    # Email (Gmail SMTP)
    SMTP_SERVER      — SMTP host (default: smtp.gmail.com)
    SMTP_PORT        — SMTP port (default: 587, TLS)
    SENDER_EMAIL     — From address (Gmail address)
    SENDER_PASSWORD  — Gmail App Password (NOT your regular password)
    REPORT_RECIPIENT — Default recipient (override per-call)

    # SMS (Twilio)
    TWILIO_ACCOUNT_SID  — Twilio Account SID
    TWILIO_AUTH_TOKEN   — Twilio Auth Token
    TWILIO_FROM_NUMBER  — Twilio phone number (e.g. +1XXXXXXXXXX)
    MY_PHONE_NUMBER     — Your phone number for SMS alerts

Gmail App Password setup:
    1. Enable 2-Factor Auth on your Google account
    2. Go to https://myaccount.google.com/apppasswords
    3. Generate an app password for "Mail"
    4. Paste the 16-char password as SENDER_PASSWORD in .env

Twilio setup:
    1. Create a free account at https://www.twilio.com
    2. Get your Account SID and Auth Token from the console
    3. Buy or use the free trial phone number
    4. Add your verified phone number as MY_PHONE_NUMBER
"""

import logging
import mimetypes
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_SMTP_SERVER = "smtp.gmail.com"
_DEFAULT_SMTP_PORT = 587
_DEFAULT_RECIPIENT = "C.Flem900@gmail.com"


class NotificationService:
    """Send DFS analytics reports via email and SMS alerts via Twilio."""

    def __init__(self):
        # Email config
        self.smtp_server = os.getenv("SMTP_SERVER", _DEFAULT_SMTP_SERVER)
        self.smtp_port = int(os.getenv("SMTP_PORT", str(_DEFAULT_SMTP_PORT)))
        self.sender_email = os.getenv("SENDER_EMAIL", "")
        self.sender_password = os.getenv("SENDER_PASSWORD", "")
        self.default_recipient = os.getenv("REPORT_RECIPIENT", _DEFAULT_RECIPIENT)

        # Twilio SMS config — auto-prepend + for E.164 format
        self.twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.twilio_from = self._e164(os.getenv("TWILIO_FROM_NUMBER", ""))
        self.my_phone = self._e164(os.getenv("MY_PHONE_NUMBER", ""))

    @staticmethod
    def _e164(number: str) -> str:
        """Ensure phone number is in E.164 format (+1XXXXXXXXXX)."""
        n = number.strip().replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
        if not n:
            return ""
        if not n.startswith("+"):
            n = "+" + n
        return n

    @property
    def is_available(self) -> bool:
        """Check if email credentials are configured."""
        return bool(self.sender_email and self.sender_password)

    @property
    def sms_available(self) -> bool:
        """Check if Twilio SMS credentials are configured."""
        return bool(
            self.twilio_sid and self.twilio_token
            and self.twilio_from and self.my_phone
        )

    def send_slate_reports(
        self,
        subject: str,
        body: str,
        file_paths: List[str],
        recipient: Optional[str] = None,
    ) -> bool:
        """Send an email with file attachments.

        Parameters
        ----------
        subject : str
            Email subject line.
        body : str
            Plain-text email body.
        file_paths : list[str]
            Paths to files to attach (CSV, XLSX, etc.).
        recipient : str, optional
            Override the default recipient.

        Returns
        -------
        bool
            True if sent successfully, False otherwise.
        """
        if not self.is_available:
            logger.warning(
                "[Notification] Email not configured — set SENDER_EMAIL and "
                "SENDER_PASSWORD in .env (Gmail App Password required)"
            )
            return False

        to_raw = recipient or self.default_recipient
        # Support comma-separated recipients
        to_list = [addr.strip() for addr in to_raw.split(",") if addr.strip()]
        if not to_list:
            logger.warning("[Notification] No recipients configured")
            return False

        # Build the email
        msg = MIMEMultipart()
        msg["From"] = self.sender_email
        msg["To"] = ", ".join(to_list)
        msg["Subject"] = subject

        # Attach body text
        msg.attach(MIMEText(body, "plain"))

        # Attach files
        attached = 0
        for filepath in file_paths:
            path = Path(filepath)
            if not path.exists():
                logger.warning(
                    "[Notification] Skipping missing file: %s", filepath
                )
                continue

            content_type, _ = mimetypes.guess_type(str(path))
            if content_type is None:
                content_type = "application/octet-stream"
            maintype, subtype = content_type.split("/", 1)

            with open(path, "rb") as f:
                part = MIMEBase(maintype, subtype)
                part.set_payload(f.read())

            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f'attachment; filename="{path.name}"',
            )
            msg.attach(part)
            attached += 1
            logger.debug("[Notification] Attached: %s", path.name)

        if attached == 0:
            logger.warning("[Notification] No files to attach — skipping send")
            return False

        # Send via SMTP
        try:
            with smtplib.SMTP(self.smtp_server, self.smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(self.sender_email, self.sender_password)
                server.sendmail(self.sender_email, to_list, msg.as_string())

            logger.info(
                "[Notification] Email sent to %s — subject: '%s', "
                "%d attachment(s)",
                ", ".join(to_list), subject, attached,
            )
            return True

        except smtplib.SMTPAuthenticationError as e:
            logger.error(
                "[Notification] SMTP auth failed — check SENDER_EMAIL and "
                "SENDER_PASSWORD (must be a Gmail App Password, not your "
                "regular password): %s", e,
            )
            return False
        except smtplib.SMTPException as e:
            logger.error("[Notification] SMTP error: %s", e)
            return False
        except Exception as e:
            logger.error("[Notification] Failed to send email: %s", e)
            return False

    # ──────────────────────────────────────────────────────────────
    # SMS Alerts (Twilio)
    # ──────────────────────────────────────────────────────────────

    def send_sms_alert(
        self,
        message_body: str,
        to_number: Optional[str] = None,
    ) -> bool:
        """Send a concise SMS alert via Twilio.

        Parameters
        ----------
        message_body : str
            The SMS text (max ~1600 chars, but keep it short).
        to_number : str, optional
            Override recipient phone number (default: MY_PHONE_NUMBER).

        Returns
        -------
        bool
            True if sent successfully, False otherwise.
        """
        if not self.sms_available:
            logger.warning(
                "[Notification] Twilio not configured — set "
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
                "TWILIO_FROM_NUMBER, and MY_PHONE_NUMBER in .env"
            )
            return False

        recipient = to_number or self.my_phone

        try:
            from twilio.rest import Client

            client = Client(self.twilio_sid, self.twilio_token)
            message = client.messages.create(
                body=message_body,
                from_=self.twilio_from,
                to=recipient,
            )

            logger.info(
                "[Notification] SMS sent → %s | SID: %s | body: %.80s...",
                recipient, message.sid, message_body,
            )
            return True

        except ImportError:
            logger.error(
                "[Notification] twilio package not installed — "
                "run: pip install twilio"
            )
            return False
        except Exception as e:
            logger.error("[Notification] SMS failed: %s", e)
            return False
