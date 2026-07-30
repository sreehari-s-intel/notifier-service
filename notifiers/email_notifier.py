"""
notifiers/email_notifier.py
Sends alert emails via SMTP.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)
_email_config_warned = False


def send_email(cfg: dict, hits_info: list[dict]):
    global _email_config_warned
    email_cfg = cfg["email"]
    if not email_cfg.get("enabled"):
        return

    sender = (email_cfg.get("sender") or "").strip()
    password = email_cfg.get("password") or ""
    recipients = [r for r in email_cfg.get("recipients", []) if isinstance(r, str) and r.strip()]

    if not sender or not recipients:
        if not _email_config_warned:
            logger.error(
                "Email notifier is enabled but sender/recipients are not configured. "
                "Set email.sender and at least one non-empty email.recipients entry."
            )
            _email_config_warned = True
        return

    subject = f"[Kibana Alert] {len(hits_info)} match(es) found for monitored keywords"
    body_html = _build_html(hits_info)
    body_text = _build_text(hits_info)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    smtp_timeout_seconds = int(email_cfg.get("timeout_seconds", 20))
    try:
        with smtplib.SMTP(
            email_cfg["smtp_host"],
            email_cfg["smtp_port"],
            timeout=smtp_timeout_seconds,
        ) as server:
            server.ehlo()
            if email_cfg.get("use_tls"):
                server.starttls()
                server.ehlo()
            if password:
                server.login(sender, password)
            server.sendmail(
                sender,
                recipients,
                msg.as_string()
            )
        logger.info("Email sent to %s", recipients)
    except Exception as e:
        logger.error(
            "Email send failed: %s. Check SMTP reachability/egress, sender credentials, and TLS settings.",
            e,
        )


def _build_text(hits_info: list[dict]) -> str:
    lines = ["Kibana Keyword Alert\n" + "=" * 40]
    for i, h in enumerate(hits_info, 1):
        lines.append(
            f"\n[{i}] Time: {h['timestamp']}"
            f"\n    Index: {h['index']}"
            f"\n    Keywords: {', '.join(h['matched_keywords'])}"
            f"\n    Message: {h['message']}"
        )
    return "\n".join(lines)


def _build_html(hits_info: list[dict]) -> str:
    rows = ""
    for h in hits_info:
        kws = ", ".join(f"<b>{kw}</b>" for kw in h["matched_keywords"])
        rows += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;color:#555">{h['timestamp']}</td>
            <td style="padding:8px;border:1px solid #ddd;color:#555">{h['index']}</td>
            <td style="padding:8px;border:1px solid #ddd">{kws}</td>
            <td style="padding:8px;border:1px solid #ddd;font-family:monospace;font-size:12px">{h['message']}</td>
        </tr>"""

    return f"""
    <html><body>
    <h2 style="color:#c0392b">⚠️ Kibana Keyword Alert</h2>
    <p>{len(hits_info)} match(es) found in Elasticsearch.</p>
    <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px">
        <thead>
            <tr style="background:#2c3e50;color:white">
                <th style="padding:10px;text-align:left">Timestamp</th>
                <th style="padding:10px;text-align:left">Index</th>
                <th style="padding:10px;text-align:left">Matched Keywords</th>
                <th style="padding:10px;text-align:left">Message</th>
            </tr>
        </thead>
        <tbody>{rows}</tbody>
    </table>
    </body></html>"""
