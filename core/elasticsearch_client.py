"""
core/elasticsearch_client.py
Handles ES connection and keyword-filtered queries.
"""

import logging
from datetime import datetime, timedelta, timezone
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


def build_client(cfg: dict) -> Elasticsearch:
    es_cfg = cfg["elasticsearch"]
    client = Elasticsearch(
        hosts=[es_cfg["host"]],
        basic_auth=(es_cfg["username"], es_cfg["password"]),
        verify_certs=False,  # Set True in production with proper certs
    )
    if not client.ping():
        raise ConnectionError(f"Cannot reach Elasticsearch at {es_cfg['host']}")
    logger.info("Elasticsearch connected.")
    return client


def fetch_matching_hits(client: Elasticsearch, cfg: dict) -> list[dict]:
    """
    Query ES for documents in the last `lookback_minutes`
    that contain any of the configured keywords.
    Returns a list of hit dicts.
    """
    es_cfg = cfg["elasticsearch"]
    keywords = cfg["keywords"]
    lookback = es_cfg.get("lookback_minutes", 2)

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback)

    # Build a should (OR) query across message/log fields for each keyword
    should_clauses = [
        {"multi_match": {
            "query": kw,
            "fields": ["message", "log.original", "event.original", "*"],
            "type": "phrase"
        }}
        for kw in keywords
    ]

    query = {
        "query": {
            "bool": {
                "must": [
                    {"range": {"@timestamp": {"gte": since.isoformat(), "lte": now.isoformat()}}}
                ],
                "should": should_clauses,
                "minimum_should_match": 1
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 50  # max hits per poll
    }

    try:
        resp = client.search(index=es_cfg["index"], body=query)
        hits = resp["hits"]["hits"]
        logger.info(f"ES query returned {len(hits)} matching hits.")
        return hits
    except Exception as e:
        logger.error(f"ES query failed: {e}")
        return []


def extract_hit_info(hit: dict, keywords: list[str]) -> dict:
    """Pull relevant fields from a raw ES hit."""
    source = hit.get("_source", {})
    message = (
        source.get("message")
        or source.get("log", {}).get("original")
        or source.get("event", {}).get("original")
        or str(source)
    )
    timestamp = source.get("@timestamp", "unknown")
    index = hit.get("_index", "unknown")
    hit_id = hit.get("_id", "unknown")

    matched_keywords = [kw for kw in keywords if kw.lower() in message.lower()]

    return {
        "id": hit_id,
        "index": index,
        "timestamp": timestamp,
        "message": message[:1000],  # truncate long messages
        "matched_keywords": matched_keywords,
        "source": source,
    }
