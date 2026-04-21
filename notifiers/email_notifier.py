"""
notifiers/email_notifier.py
Sends alert emails via SMTP.
"""

import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(cfg: dict, hits_info: list[dict]):
    email_cfg = cfg["email"]
    if not email_cfg.get("enabled"):
        return

    subject = f"[Kibana Alert] {len(hits_info)} match(es) found for monitored keywords"
    body_html = _build_html(hits_info)
    body_text = _build_text(hits_info)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = email_cfg["sender"]
    msg["To"] = ", ".join(email_cfg["recipients"])
    msg.attach(MIMEText(body_text, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            if email_cfg.get("use_tls"):
                server.starttls()
            server.login(email_cfg["sender"], email_cfg["password"])
            server.sendmail(
                email_cfg["sender"],
                email_cfg["recipients"],
                msg.as_string()
            )
        logger.info(f"Email sent to {email_cfg['recipients']}")
    except Exception as e:
        logger.error(f"Email send failed: {e}")


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
