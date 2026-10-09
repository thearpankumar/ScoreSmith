"""Transactional e-mail (password reset, e-mail verification).

EMAIL_BACKEND=log (default, local dev): the message, including its link, is written to the server log - open
the API log to follow it. EMAIL_BACKEND=ses: sent through Amazon SES (sesv2) from EMAIL_FROM using the process's
normal AWS credential chain (a task role in production)."""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)


def _send_ses(to: str, subject: str, body: str) -> None:
    import boto3

    s = get_settings()
    boto3.client("sesv2", region_name=s.aws_region).send_email(
        FromEmailAddress=s.email_from,
        Destination={"ToAddresses": [to]},
        Content={"Simple": {"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}}},
    )


async def send_email(to: str, subject: str, body: str) -> None:
    """Never raises: a mail outage must not turn "forgot password" into a 500 (which would also reveal that the
    address exists)."""
    s = get_settings()
    try:
        if s.email_backend == "ses" and s.email_from:
            await asyncio.to_thread(_send_ses, to, subject, body)
        else:
            logger.info("EMAIL (log backend) to=%s subject=%r\n%s", to, subject, body)
    except Exception:  # noqa: BLE001
        logger.warning("Could not send e-mail to a user (subject=%r).", subject, exc_info=True)
