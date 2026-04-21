"""
notifiers/teams_notifier.py
Sends alert cards to Microsoft Teams via Incoming Webhook.
Uses Adaptive Card format (supported in all modern Teams tenants).
"""

import json
import logging
import requests

logger = logging.getLogger(__name__)


def send_teams(cfg: dict, hits_info: list[dict]):
    teams_cfg = cfg.get("teams", {})
    if not teams_cfg.get("enabled"):
        return

    webhook_url = teams_cfg.get("webhook_url")
    if not webhook_url:
        logger.error("Teams webhook_url not configured.")
        return

    payload = _build_adaptive_card(hits_info)

    try:
        resp = requests.post(
            webhook_url,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=10
        )
        if resp.status_code == 200:
            logger.info("Teams notification sent.")
        else:
            logger.error(f"Teams webhook returned {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Teams notification failed: {e}")


def _build_adaptive_card(hits_info: list[dict]) -> dict:
    facts_blocks = []
    for i, h in enumerate(hits_info, 1):
        kws = ", ".join(h["matched_keywords"])
        msg_preview = h["message"][:300] + ("..." if len(h["message"]) > 300 else "")
        facts_blocks.append({
            "type": "FactSet",
            "facts": [
                {"title": f"[{i}] Time", "value": h["timestamp"]},
                {"title": "Index", "value": h["index"]},
                {"title": "Keywords", "value": kws},
                {"title": "Message", "value": msg_preview},
            ]
        })
        if i < len(hits_info):
            facts_blocks.append({"type": "Separator"})

    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": f"⚠️ Kibana Keyword Alert — {len(hits_info)} match(es)",
                            "weight": "Bolder",
                            "size": "Large",
                            "color": "Attention"
                        },
                        {
                            "type": "TextBlock",
                            "text": "The following log entries matched your monitored keywords:",
                            "wrap": True,
                            "isSubtle": True
                        },
                        *facts_blocks
                    ]
                }
            }
        ]
    }
