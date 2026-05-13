from celery import Celery
import imaplib
import socket
import smtplib
import time
from email.message import EmailMessage
from email.utils import make_msgid

from core.config import settings

celery_app = Celery(
    "luck_game",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def send_email_job(self, to_address: str, subject: str, body: str) -> dict:
    if not to_address:
        return {"sent": False, "error": "Missing recipient email address."}
    if not settings.smtp_host:
        return {"sent": False, "error": "SMTP_HOST is not configured.", "to": to_address}
    message = EmailMessage()
    message["From"] = settings.smtp_from_email or settings.smtp_username
    message["To"] = to_address
    message["Subject"] = subject
    message_id = make_msgid(domain=(settings.smtp_from_email or settings.smtp_username).split("@")[-1])
    message["Message-ID"] = message_id
    message.set_content(body)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
        cleanup = _delete_sent_copy(message_id) if settings.smtp_delete_sent_copy else {"enabled": False}
        return {"sent": True, "to": to_address, "subject": subject, "sent_copy_cleanup": cleanup}
    except smtplib.SMTPAuthenticationError as exc:
        return {"sent": False, "error": str(exc), "to": to_address, "subject": subject}
    except (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, socket.gaierror, TimeoutError) as exc:
        raise self.retry(exc=exc, countdown=min(300, 30 * (self.request.retries + 1))) from exc
    except Exception as exc:
        return {"sent": False, "error": str(exc), "to": to_address, "subject": subject}


def _delete_sent_copy(message_id: str) -> dict:
    if not settings.smtp_imap_host:
        return {"enabled": True, "deleted": False, "error": "SMTP_IMAP_HOST is not configured."}
    if not settings.smtp_username or not settings.smtp_password:
        return {"enabled": True, "deleted": False, "error": "SMTP username/password are required for sent cleanup."}

    try:
        with imaplib.IMAP4_SSL(settings.smtp_imap_host, settings.smtp_imap_port) as imap:
            imap.login(settings.smtp_username, settings.smtp_password)
            status, _ = imap.select(f'"{settings.smtp_sent_mailbox}"')
            if status != "OK":
                return {"enabled": True, "deleted": False, "error": f"Could not open mailbox {settings.smtp_sent_mailbox}."}

            encoded_id = message_id.encode("utf-8")
            for _ in range(4):
                status, data = imap.search(None, "HEADER", "Message-ID", encoded_id)
                if status == "OK" and data and data[0]:
                    ids = data[0].split()
                    for mail_id in ids:
                        imap.store(mail_id, "+FLAGS", "\\Deleted")
                    imap.expunge()
                    return {"enabled": True, "deleted": True, "message_count": len(ids)}
                time.sleep(1)

            return {"enabled": True, "deleted": False, "error": "Sent copy was not found by Message-ID."}
    except Exception as exc:
        return {"enabled": True, "deleted": False, "error": str(exc)}


@celery_app.task
def generate_report_job(report_name: str) -> dict:
    return {"queued": True, "report": report_name}


# ---------------------------------------------------------------------------
# Beat schedule + task discovery
# ---------------------------------------------------------------------------

from celery.schedules import crontab  # noqa: E402

celery_app.conf.update(
    # Import cleanup tasks so the worker registers them on startup
    imports=("tasks.cleanup",),
    timezone="UTC",
    beat_schedule={
        # Run the full cleanup sequence every day at 02:00 UTC
        "daily-cleanup": {
            "task": "tasks.cleanup.daily_cleanup_job",
            "schedule": crontab(hour=2, minute=0),
        },
    },
)
