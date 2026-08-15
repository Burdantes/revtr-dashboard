#!/usr/bin/env python3
"""Diagnose the alert mail path with explicit SMTP steps.

`send_message()` collapses the whole conversation into success/failure, which
cannot distinguish "the relay accepted and queued it" from "the relay accepted
and dropped it". This walks MAIL FROM / RCPT TO / DATA separately and prints
the server's final response, including any queue or message id -- the only
local evidence that a message actually entered the provider's pipeline.

Run it the way production runs, via docker --env-file, so it exercises the
same config path:

    docker run --rm --env-file $HOME/revtr-alerts/alert.env \
      -v $HOME/revtr-alerts/smtp_probe.py:/probe.py:ro \
      --entrypoint python revtr-monitor:latest /probe.py

Do NOT `source` alert.env in bash to set these vars -- see ALERTING.md.

Prints no credentials: no set_debuglevel, no AUTH echo.
"""

import os
import smtplib
import ssl
import sys
from datetime import datetime, timezone
from email.message import EmailMessage


def main() -> int:
    try:
        host = os.environ["SMTP_HOST"]
        port = int(os.environ["SMTP_PORT"])
        user = os.environ["SMTP_USER"]
        password = os.environ["SMTP_PASSWORD"]
        sender = os.environ["SMTP_FROM"]
        rcpt = os.environ["ALERT_EMAIL_TO"]
    except KeyError as e:
        print(f"missing env var: {e}. Is alert.env complete?", file=sys.stderr)
        return 2

    stamp = datetime.now(timezone.utc).isoformat()
    msg = EmailMessage()
    msg["Subject"] = f"revTr SMTP probe {stamp}"
    msg["From"] = sender
    msg["To"] = rcpt
    msg.set_content(
        f"Delivery probe sent {stamp} from the revtr-dashboard VM.\n"
        "If you are reading this, outbound relay works end to end.\n"
    )

    print(f"connect {host}:{port}")
    with smtplib.SMTP(host, port, timeout=30) as s:
        code, banner = s.ehlo()
        first = banner.decode(errors="replace").splitlines()[0]
        print(f"  EHLO      -> {code} {first}")
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
        print(f"  STARTTLS  -> ok (peer cert present: {bool(s.sock.getpeercert())})")
        s.login(user, password)
        print("  AUTH      -> ok")
        code, resp = s.mail(sender)
        print(f"  MAIL FROM -> {code} {resp.decode(errors='replace')}")
        code, resp = s.rcpt(rcpt)
        print(f"  RCPT TO   -> {code} {resp.decode(errors='replace')}")
        code, resp = s.data(msg.as_bytes())
        print(f"  DATA      -> {code} {resp.decode(errors='replace')}")

    if code == 250:
        print("\nRESULT: accepted for relay.")
        print("NOTE: acceptance is not delivery. With SES Mail Manager the")
        print("      message still needs a 'Send to Internet' rule action.")
        return 0
    print(f"\nRESULT: unexpected final code {code}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
