"""
notifiers/teams_notifier.py
Sends alert cards to Microsoft Teams via Incoming Webhook.
Uses Adaptive Card format (supported in all modern Teams tenants).
"""

import json
import logging
import requests

logger = logging.getLogger(__name__)
_teams_auth_warned = False


def send_teams(cfg: dict, hits_info: list[dict]):
    global _teams_auth_warned
    teams_cfg = cfg.get("teams", {})
    if not teams_cfg.get("enabled"):
        return

    webhook_url = teams_cfg.get("webhook_url")
    if not webhook_url:
        logger.error("Teams webhook_url not configured.")
        return

    bearer_token = (teams_cfg.get("bearer_token") or "").strip()
    is_power_automate_direct_api = "powerautomate/automations/direct/workflows" in webhook_url
    if is_power_automate_direct_api and not bearer_token and not _teams_auth_warned:
        logger.error(
            "Teams URL is a Power Automate Direct API endpoint and requires OAuth bearer token. "
            "Set teams.bearer_token, or use a Teams Incoming Webhook URL."
        )
        _teams_auth_warned = True
        return

    payload = _build_adaptive_card(hits_info)
    headers = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    try:
        resp = requests.post(
            webhook_url,
            headers=headers,
            data=json.dumps(payload),
            timeout=10
        )
        if resp.status_code == 200:
            logger.info("Teams notification sent.")
        else:
            logger.error(
                "Teams webhook returned %s: %s",
                resp.status_code,
                resp.text,
            )
            if resp.status_code == 401 and not _teams_auth_warned:
                logger.error(
                    "Teams authorization failed. For Power Automate Direct API endpoints, "
                    "configure teams.bearer_token or switch to a standard Teams Incoming Webhook URL."
                )
                _teams_auth_warned = True
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
