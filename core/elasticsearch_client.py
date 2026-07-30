"""
core/elasticsearch_client.py
Handles ES connection and keyword-filtered queries.
"""

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


def build_client(cfg: dict) -> Elasticsearch:
    es_cfg = cfg["elasticsearch"]
    host = es_cfg["host"]
    parsed = urlparse(host)
    hint = ""
    if parsed.port == 5601:
        hint = " It looks like a Kibana endpoint (:5601). Use an Elasticsearch endpoint (usually :9200)."
    elif parsed.hostname in {"127.0.0.1", "localhost"} and parsed.scheme == "http":
        hint = (
            " If this is a kubectl tunnel to secured Elasticsearch, use HTTPS "
            "(set k8s.use_https: true)."
        )

    client = Elasticsearch(
        hosts=[host],
        basic_auth=(es_cfg["username"], es_cfg["password"]),
        verify_certs=False,  # Set True in production with proper certs
    )
    try:
        info = client.info()
        cluster_name = info.get("cluster_name", "unknown") if isinstance(info, dict) else "unknown"
        logger.info("Elasticsearch connected. Cluster: %s", cluster_name)
        return client
    except Exception as exc:
        message = str(exc)
        if "wrong version number" in message.lower():
            hint += " TLS handshake failed. The endpoint is likely plain HTTP while configured as HTTPS."
        elif "401" in message or "security_exception" in message.lower():
            hint += " Authentication failed. Verify elasticsearch.username/password for this cluster."
        elif "403" in message:
            hint += " Authorization failed. The user may not have cluster/index read permissions."
        elif "connection refused" in message.lower():
            hint += " Connection refused. Verify tunnel target service/port and that pods are healthy."

        raise ConnectionError(f"Cannot connect to Elasticsearch at {host}.{hint} Cause: {message}") from exc


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
    source_dict = source if isinstance(source, dict) else {}

    log_field = source_dict.get("log")
    event_field = source_dict.get("event")

    log_original = log_field.get("original") if isinstance(log_field, dict) else None
    event_original = event_field.get("original") if isinstance(event_field, dict) else None

    message = source_dict.get("message") or log_original or event_original or str(source)
    timestamp = source_dict.get("@timestamp", "unknown")
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
