"""
core/kibana_log_client.py
Fetches logs from Kibana's console proxy API and scans for keywords.
Works when only Kibana (:5601) is network-accessible, not Elasticsearch directly.
Uses: POST /api/console/proxy?path=<index>/_search&method=POST
"""

import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_BACKOFF = 2  # seconds


def fetch_remote_logs(cfg: dict) -> str:
    """
    Query Elasticsearch through Kibana's console proxy API.
    Builds a proper search query with time range and filters from config.
    Returns the raw JSON response text.
    """
    kibana_cfg = cfg["kibana_source"]
    base_url = kibana_cfg["base_url"].rstrip("/")
    index = kibana_cfg["index"]
    username = kibana_cfg.get("username")
    password = kibana_cfg.get("password")
    headers = kibana_cfg.get("headers", {})
    verify_ssl = kibana_cfg.get("verify_ssl", False)
    timeout = kibana_cfg.get("timeout_seconds", 30)
    lookback = kibana_cfg.get("lookback_minutes", 5)

    auth = (username, password) if username and password else None

    # Build search query body
    query_body = _build_search_query(cfg, lookback)

    # Try multiple Kibana routes because some roles block console proxy (403).
    encoded_path = quote(f"{index}/_search", safe="")
    endpoints = [
        {
            "name": "console_proxy",
            "url": f"{base_url}/api/console/proxy?path={encoded_path}&method=POST",
            "payload": query_body,
        },
        {
            "name": "internal_search_es",
            "url": f"{base_url}/internal/search/es",
            "payload": {"params": {"index": index, "body": query_body}},
        },
        {
            "name": "legacy_elasticsearch_proxy",
            "url": f"{base_url}/elasticsearch/{index}/_search",
            "payload": query_body,
        },
    ]

    for attempt in range(1, MAX_RETRIES + 1):
        for endpoint in endpoints:
            try:
                logger.info(
                    f"Querying Kibana endpoint '{endpoint['name']}' (attempt {attempt}/{MAX_RETRIES})"
                )
                resp = _post_kibana(
                    url=endpoint["url"],
                    auth=auth,
                    headers=headers,
                    payload=endpoint["payload"],
                    verify_ssl=verify_ssl,
                    timeout=timeout,
                )
                logger.info(
                    f"Kibana endpoint '{endpoint['name']}' returned {len(resp.text)} chars"
                )
                return resp.text
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else "unknown"
                body = e.response.text[:300] if e.response is not None else ""
                logger.warning(
                    f"Endpoint '{endpoint['name']}' failed with HTTP {status}. "
                    f"Response preview: {body}"
                )
            except requests.exceptions.RequestException as e:
                logger.warning(f"Endpoint '{endpoint['name']}' failed: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
        else:
            logger.error(f"All {MAX_RETRIES} attempts across Kibana endpoints failed.")
            endpoint_names = ", ".join(ep["name"] for ep in endpoints)
            raise ConnectionError(
                "Cannot query Kibana APIs. Tried endpoints: "
                f"{endpoint_names}. If all return 403, your Kibana role lacks API permissions."
            )


def _post_kibana(url: str, auth, headers: dict, payload: dict, verify_ssl: bool, timeout: int):
    """Issue a POST request to a Kibana endpoint with JSON payload."""
    resp = requests.post(
        url,
        auth=auth,
        headers=headers,
        json=payload,
        verify=verify_ssl,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


def _build_search_query(cfg: dict, lookback_minutes: int) -> dict:
    """
    Build an Elasticsearch query body that mirrors the Discover view filters.
    Includes time range, host filters, container image filters, and keyword matching.
    """
    kibana_cfg = cfg["kibana_source"]
    keywords = cfg["keywords"]
    filters = kibana_cfg.get("filters", {})

    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=lookback_minutes)

    # Must clauses: time range
    must_clauses = [
        {"range": {"@timestamp": {"gte": since.isoformat(), "lte": now.isoformat()}}}
    ]

    # Filter by kubernetes hosts (if configured)
    hosts = filters.get("kubernetes_hosts", [])
    if hosts:
        must_clauses.append({
            "bool": {
                "should": [{"match_phrase": {"kubernetes.host": h}} for h in hosts],
                "minimum_should_match": 1
            }
        })

    # Filter by container images (if configured)
    images = filters.get("container_images", [])
    if images:
        must_clauses.append({
            "bool": {
                "should": [{"match_phrase": {"kubernetes.container_image": img}} for img in images],
                "minimum_should_match": 1
            }
        })

    # Should clauses: keyword matching across log/message fields
    should_clauses = [
        {"multi_match": {
            "query": kw,
            "fields": ["log", "message", "log.original", "event.original"],
            "type": "phrase"
        }}
        for kw in keywords
    ]

    query = {
        "query": {
            "bool": {
                "must": must_clauses,
                "should": should_clauses,
                "minimum_should_match": 1
            }
        },
        "sort": [{"@timestamp": {"order": "desc"}}],
        "size": 100,
        "_source": ["@timestamp", "log", "message", "kubernetes.host",
                    "kubernetes.container_image", "kubernetes.labels.pipeline"]
    }

    return query


def scan_logs_for_keywords(log_content: str, keywords: list[str]) -> list[dict]:
    """
    Parse log content and return hits where any keyword is found.
    Auto-detects JSON (Kibana/ES API response) vs plain text format.
    """
    # Try parsing as JSON first (Kibana API / ES response)
    try:
        data = json.loads(log_content)
        if isinstance(data, dict) and "hits" in data:
            return _parse_es_json_response(data, keywords)
        elif isinstance(data, list):
            return _parse_json_array(data, keywords)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: plain text line-by-line scan
    return _parse_plain_text(log_content, keywords)


def _parse_es_json_response(data: dict, keywords: list[str]) -> list[dict]:
    """
    Parse Elasticsearch/Kibana JSON response format:
    { "hits": { "hits": [ { "_id": ..., "_source": { "message": ... } } ] } }
    """
    hits_data = data.get("hits", {})
    raw_hits = hits_data.get("hits", [])
    results = []

    total = hits_data.get("total", {})
    total_count = total.get("value", len(raw_hits)) if isinstance(total, dict) else total
    logger.info(f"Kibana API returned {total_count} total hits, processing {len(raw_hits)}.")

    for hit in raw_hits:
        source = hit.get("_source", {})
        hit_id = hit.get("_id", hashlib.sha256(str(source).encode()).hexdigest()[:16])

        # Extract message from common fields
        message = _extract_message_from_source(source)
        message_lower = message.lower()

        matched = [kw for kw in keywords if kw.lower() in message_lower]
        if matched:
            results.append({
                "id": hit_id,
                "index": hit.get("_index", "unknown"),
                "timestamp": source.get("@timestamp", "unknown"),
                "message": message[:1000],
                "matched_keywords": matched,
                "source": source,
            })

    logger.info(f"ES JSON: {len(raw_hits)} fetched, {len(results)} matched keywords.")
    return results


def _parse_json_array(data: list, keywords: list[str]) -> list[dict]:
    """Parse a JSON array of log objects."""
    results = []

    for i, entry in enumerate(data):
        if isinstance(entry, dict):
            message = _extract_message_from_source(entry)
        else:
            message = str(entry)

        message_lower = message.lower()
        matched = [kw for kw in keywords if kw.lower() in message_lower]
        if matched:
            hit_id = hashlib.sha256(f"{i}:{message}".encode()).hexdigest()[:16]
            results.append({
                "id": hit_id,
                "index": "remote-log",
                "timestamp": entry.get("@timestamp", "unknown") if isinstance(entry, dict) else "unknown",
                "message": message[:1000],
                "matched_keywords": matched,
                "source": entry if isinstance(entry, dict) else {"raw": message},
            })

    logger.info(f"JSON array: {len(data)} entries, {len(results)} matched keywords.")
    return results


def _parse_plain_text(log_content: str, keywords: list[str]) -> list[dict]:
    """Fallback plain text line-by-line scan."""
    hits = []
    lines = log_content.splitlines()

    for line_num, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        line_lower = line.lower()
        matched = [kw for kw in keywords if kw.lower() in line_lower]
        if matched:
            hit_id = hashlib.sha256(f"{line_num}:{line}".encode()).hexdigest()[:16]
            hits.append({
                "id": hit_id,
                "index": "remote-log",
                "timestamp": _extract_timestamp(line),
                "message": line[:1000],
                "matched_keywords": matched,
                "source": {"line_number": line_num, "raw": line},
            })

    logger.info(f"Plain text: scanned {len(lines)} lines, {len(hits)} matched keywords.")
    return hits


def _extract_message_from_source(source: dict) -> str:
    """
    Extract the log message from an ES document _source.
    Searches common fields in priority order.
    """
    candidates = [
        source.get("log"),
        source.get("message"),
        source.get("log", {}).get("original") if isinstance(source.get("log"), dict) else None,
        source.get("event", {}).get("original") if isinstance(source.get("event"), dict) else None,
        source.get("msg"),
        source.get("log_message"),
        source.get("text"),
    ]

    for candidate in candidates:
        if candidate and isinstance(candidate, str):
            return candidate

    # Fallback: stringify the entire source
    return str(source)


def _extract_timestamp(line: str) -> str:
    """Best-effort timestamp extraction from a log line."""
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", line)
    if iso_match:
        return iso_match.group(0)

    syslog_match = re.search(r"[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}", line)
    if syslog_match:
        return syslog_match.group(0)

    return "unknown"
