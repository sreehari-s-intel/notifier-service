"""
main.py
Entry point. Polls Elasticsearch on a schedule and dispatches notifications.
"""

import logging
import time
import yaml

from core.elasticsearch_client import build_client, fetch_matching_hits, extract_hit_info
from core.dedup import DedupTracker
from notifiers.email_notifier import send_email
from notifiers.teams_notifier import send_teams

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/notifier.log"),
    ]
)
logger = logging.getLogger("main")


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run_once(client, cfg: dict, dedup: DedupTracker):
    """Single poll cycle."""
    raw_hits = _poll_elasticsearch(client, cfg)

    if not raw_hits:
        logger.info("No matches this cycle.")
        return

    new_hits = []
    for hit in raw_hits:
        if dedup.is_new(hit["id"]):
            new_hits.append(hit)

    if not new_hits:
        logger.info(f"{len(raw_hits)} hit(s) found but all already notified (dedup).")
        return

    logger.info(f"Sending notifications for {len(new_hits)} new hit(s).")
    send_email(cfg, new_hits)
    send_teams(cfg, new_hits)


def _poll_elasticsearch(client, cfg: dict) -> list[dict]:
    """Fetch and extract hits from Elasticsearch."""
    raw_hits = fetch_matching_hits(client, cfg)
    keywords = cfg["keywords"]
    return [extract_hit_info(hit, keywords) for hit in raw_hits]


def main():
    cfg = load_config()
    dedup_window = cfg["notification"].get("dedup_window_seconds", 300)

    logger.info("Starting Kibana Keyword Notifier...")
    logger.info("Source mode: elasticsearch (forced)")
    logger.info(f"Keywords: {cfg['keywords']}")

    interval = cfg["elasticsearch"].get("poll_interval_seconds", 60)
    logger.info("Using Elasticsearch endpoint: %s", cfg["elasticsearch"].get("host"))
    client = build_client(cfg)

    logger.info(f"Poll interval: {interval}s | Dedup window: {dedup_window}s")
    dedup = DedupTracker(window_seconds=dedup_window)

    while True:
        try:
            run_once(client, cfg, dedup)
        except Exception as e:
            logger.error(f"Unexpected error in poll cycle: {e}", exc_info=True)
        logger.info(f"Sleeping {interval}s until next poll...")
        time.sleep(interval)


if __name__ == "__main__":
    main()
