"""
chat/query_builder.py
Translates parsed LLM output into Elasticsearch queries.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


def parse_relative_time(time_str: str) -> datetime:
    """Convert relative time strings like '1h', '30m', '2d' to absolute datetime."""
    now = datetime.now(timezone.utc)

    if time_str == "now":
        return now

    # Try ISO format first
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        pass

    # Parse relative formats
    match = re.match(r"^(\d+)([mhdw])$", time_str.strip())
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        delta_map = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
        delta = timedelta(**{delta_map[unit]: value})
        return now - delta

    # Default: 1 hour ago
    logger.warning(f"Could not parse time '{time_str}', defaulting to 1h ago")
    return now - timedelta(hours=1)


def build_es_query(parsed: dict) -> dict:
    """Build an Elasticsearch query from parsed LLM output."""
    time_from = parse_relative_time(parsed.get("time_from", "1h"))
    time_to = parse_relative_time(parsed.get("time_to", "now"))
    search_terms = parsed.get("search_terms", [])
    max_results = min(parsed.get("max_results", 50), 200)  # Cap at 200

    # Build should clauses for search terms
    should_clauses = []
    for term in search_terms:
        should_clauses.append({
            "multi_match": {
                "query": term,
                "fields": ["message", "log.original", "event.original", "log.level", "*"],
                "type": "phrase" if " " in term else "best_fields",
            }
        })

    query = {
        "query": {
            "bool": {
                "must": [
                    {"range": {"@timestamp": {
                        "gte": time_from.isoformat(),
                        "lte": time_to.isoformat(),
                    }}}
                ],
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": max_results,
    }

    if should_clauses:
        query["query"]["bool"]["should"] = should_clauses
        query["query"]["bool"]["minimum_should_match"] = 1

    return query


def execute_query(client: Elasticsearch, index: str, query: dict) -> list[dict]:
    """Execute ES query and return formatted results."""
    try:
        resp = client.search(index=index, body=query)
        hits = resp["hits"]["hits"]
        logger.info(f"Chat query returned {len(hits)} hits")

        results = []
        for hit in hits:
            source = hit.get("_source", {})
            source_dict = source if isinstance(source, dict) else {}

            log_field = source_dict.get("log")
            event_field = source_dict.get("event")

            log_original = log_field.get("original") if isinstance(log_field, dict) else None
            event_original = event_field.get("original") if isinstance(event_field, dict) else None
            log_level = log_field.get("level") if isinstance(log_field, dict) else None

            message = source_dict.get("message") or log_original or event_original or str(source)[:500]
            results.append({
                "id": hit.get("_id", ""),
                "index": hit.get("_index", ""),
                "timestamp": source_dict.get("@timestamp", "unknown"),
                "message": message[:1000],
                "level": log_level or source_dict.get("level", ""),
                "source": source,
            })
        return results
    except Exception as e:
        logger.error(f"ES query failed: {e}")
        return []
