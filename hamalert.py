#!/usr/bin/env python3
"""
HamAlert Telnet Bot
Connects to hamalert.org:7300, parses JSON spots, and forwards them via Gmail.
"""

import telnetlib
import json
import smtplib
import time
import logging
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# ── Config (from environment variables) ──────────────────────────────────────
TELNET_HOST   = os.getenv("TELNET_HOST",    "hamalert.org")
TELNET_PORT   = int(os.getenv("TELNET_PORT", "7300"))
HA_USERNAME   = os.getenv("HA_USERNAME",    "")   # HamAlert callsign / login
HA_PASSWORD   = os.getenv("HA_PASSWORD",    "")   # HamAlert password

GMAIL_USER    = os.getenv("GMAIL_USER",     "")   # your-bot@gmail.com
GMAIL_APP_PW  = os.getenv("GMAIL_APP_PW",  "")   # Gmail App Password (16 chars)
EMAIL_TO      = os.getenv("EMAIL_TO",       "")

# How many spots to batch before sending one e-mail (set to 1 for instant)
BATCH_SIZE    = int(os.getenv("BATCH_SIZE", "1"))
# Reconnect delay in seconds after a connection drop
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "30"))


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(spots: list[dict]) -> None:
    """Send a batch of spots as a formatted e-mail."""
    if not spots:
        return

    subject = (
        f"HamAlert – {len(spots)} new spot{'s' if len(spots) > 1 else ''} "
        f"@ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    )

    # ── Plain-text body ──
    lines = []
    for s in spots:
        lines.append("─" * 50)
        # Prefer pretty keys if present, fall back to raw JSON keys
        dx       = s.get("dx",          s.get("dxCall",      "?"))
        freq     = s.get("freq",        s.get("frequency",   "?"))
        spotter  = s.get("spotter",     s.get("spottedBy",   "?"))
        mode     = s.get("mode",        "?")
        comment  = s.get("comment",     s.get("info",        ""))
        ts       = s.get("time",        s.get("timestamp",   ""))

        lines.append(f"DX       : {dx}")
        lines.append(f"Freq     : {freq} kHz")
        lines.append(f"Mode     : {mode}")
        lines.append(f"Spotter  : {spotter}")
        if comment:
            lines.append(f"Comment  : {comment}")
        if ts:
            lines.append(f"Time     : {ts}")
    lines.append("─" * 50)
    plain_body = "\n".join(lines)

    # ── HTML body ──
    rows = ""
    for s in spots:
        dx      = s.get("dx",      s.get("dxCall",    "?"))
        freq    = s.get("freq",    s.get("frequency", "?"))
        spotter = s.get("spotter", s.get("spottedBy", "?"))
        mode    = s.get("mode",    "?")
        comment = s.get("comment", s.get("info",      ""))
        ts      = s.get("time",    s.get("timestamp", ""))
        rows += (
            f"<tr>"
            f"<td><b>{dx}</b></td>"
            f"<td>{freq} kHz</td>"
            f"<td>{mode}</td>"
            f"<td>{spotter}</td>"
            f"<td>{comment}</td>"
            f"<td>{ts}</td>"
            f"</tr>"
        )
    html_body = f"""
    <html><body>
    <h2 style="font-family:sans-serif;color:#1a73e8;">📡 HamAlert Spots</h2>
    <table border="1" cellpadding="6" cellspacing="0"
           style="font-family:monospace;border-collapse:collapse;">
      <thead style="background:#1a73e8;color:white;">
        <tr>
          <th>DX</th><th>Freq</th><th>Mode</th>
          <th>Spotter</th><th>Comment</th><th>Time</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="font-family:sans-serif;font-size:0.8em;color:#888;">
      Sent by HamAlert-Bot · {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
    </p>
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body,  "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(GMAIL_USER, GMAIL_APP_PW)
            server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info("✉️  Email sent: %s → %s (%d spot(s))", GMAIL_USER, EMAIL_TO, len(spots))
    except Exception as exc:
        log.error("Failed to send email: %s", exc)


# ── Telnet loop ───────────────────────────────────────────────────────────────
def connect_and_stream() -> None:
    """Open Telnet connection, log in, and process incoming spot lines."""
    log.info("Connecting to %s:%s …", TELNET_HOST, TELNET_PORT)
    tn = telnetlib.Telnet(TELNET_HOST, TELNET_PORT, timeout=30)

    # ── Login handshake ──
    # HamAlert sends a login prompt; wait for it then send credentials
    banner = tn.read_until(b"login:", timeout=15).decode("utf-8", errors="replace")
    log.debug("Banner: %s", banner.strip())
    tn.write(HA_USERNAME.encode() + b"\n")

    prompt = tn.read_until(b"password:", timeout=10).decode("utf-8", errors="replace")
    log.debug("Prompt: %s", prompt.strip())
    tn.write(HA_PASSWORD.encode() + b"\n")

    # Small delay for the server to accept credentials
    time.sleep(2)
    log.info("Logged in as %s. Waiting for spots …", HA_USERNAME)

    pending: list[dict] = []

    while True:
        try:
            raw = tn.read_until(b"\n", timeout=120)
        except EOFError:
            log.warning("Connection closed by server.")
            break

        if not raw:
            # Timeout – connection still alive, keep waiting
            continue

        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue

        log.debug("RAW: %s", line)

        # ── Parse JSON spot ──
        try:
            spot = json.loads(line)
        except json.JSONDecodeError:
            # Not every line is JSON (e.g. keep-alive pings, text messages)
            log.debug("Non-JSON line skipped: %s", line)
            continue

        log.info("📻 Spot: %s", spot)
        pending.append(spot)

        if len(pending) >= BATCH_SIZE:
            send_email(pending)
            pending.clear()

    tn.close()


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    # Validate required config
    missing = [
        name for name, val in [
            ("HA_USERNAME",  HA_USERNAME),
            ("HA_PASSWORD",  HA_PASSWORD),
            ("GMAIL_USER",   GMAIL_USER),
            ("GMAIL_APP_PW", GMAIL_APP_PW),
            ("EMAIL_TO",     EMAIL_TO),
        ]
        if not val
    ]
    if missing:
        log.error("Missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)

    while True:
        try:
            connect_and_stream()
        except KeyboardInterrupt:
            log.info("Interrupted by user. Goodbye!")
            sys.exit(0)
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)

        log.info("Reconnecting in %d seconds …", RECONNECT_DELAY)
        time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    main()

