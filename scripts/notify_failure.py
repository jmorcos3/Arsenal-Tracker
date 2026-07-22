"""Fallback failure notifier: called by the workflow when digest.py couldn't even start."""

import os
import ssl
import smtplib
from email.mime.text import MIMEText

sender = os.environ.get("DIGEST_FROM", "jmorcos3@gmail.com")
recipient = os.environ.get("DIGEST_TO", "jmorcos3@gmail.com")
password = os.environ.get("GMAIL_APP_PASSWORD")
run_url = os.environ.get("RUN_URL", "(no url provided)")

if not password:
    raise SystemExit("no GMAIL_APP_PASSWORD; cannot notify")

body = (
    "The Arsenal digest workflow failed before the Python script could report "
    "the error itself (likely a setup, dependency, or secret issue).\n\n"
    f"Actions run: {run_url}"
)

msg = MIMEText(body)
msg["Subject"] = "Arsenal Digest — WORKFLOW FAILED"
msg["From"] = sender
msg["To"] = recipient

with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as server:
    server.login(sender, password)
    server.sendmail(sender, [recipient], msg.as_string())

print("Sent failure notification")
