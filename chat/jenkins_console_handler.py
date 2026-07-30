"""
chat/jenkins_console_handler.py
Handler for analyzing Jenkins pipeline console output.
"""

from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse

import requests

_ERROR_PATTERNS = [
    r"\berror\b",
    r"\bexception\b",
    r"\btraceback\b",
    r"\bfailed\b",
    r"\bfailure\b",
    r"\bscript returned exit code\b",
]

_WARNING_PATTERNS = [
    r"\bwarn\b",
    r"\bwarning\b",
    r"\bdeprecated\b",
]

_STAGE_PATTERN = re.compile(r"(?:\[Pipeline\]\s*\{\s*\(([^\)]+)\)|Entering stage\s*\[?([^\]]+)\]?)")
_BUILD_RESULT_PATTERN = re.compile(r"Finished:\s*(SUCCESS|FAILURE|UNSTABLE|ABORTED)", re.IGNORECASE)


def fetch_jenkins_console_output(
    console_url: str,
    username: str = "",
    password: str = "",
    api_token: str = "",
    bearer_token: str = "",
    verify_ssl: bool = True,
    timeout_seconds: int = 30,
) -> str:
    """Fetch Jenkins console output from URL. Supports basic and bearer auth."""
    if not (console_url or "").strip():
        raise ValueError("console_url is empty")

    normalized_url = _normalize_console_url(console_url.strip())
    headers = {"Accept": "text/plain"}
    auth = None

    if username and api_token:
        auth = (username, api_token)
    elif username and password:
        auth = (username, password)
    elif bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"

    resp = requests.get(
        normalized_url,
        headers=headers,
        auth=auth,
        timeout=timeout_seconds,
        verify=verify_ssl,
    )
    if resp.status_code in {401, 403}:
        raise PermissionError(
            f"Jenkins authorization failed ({resp.status_code}) for URL: {normalized_url}. "
            "Verify jenkins.username and jenkins.password (or api_token/bearer_token) and job permissions."
        )
    resp.raise_for_status()

    body = resp.text or ""
    # If HTML endpoint is used by mistake, strip tags to keep analysis usable.
    if "<html" in body.lower() and "</" in body:
        body = re.sub(r"<[^>]+>", " ", body)

    return body


def analyze_jenkins_console(console_output: str, question: str = "") -> dict:
    text = console_output or ""
    if not text.strip():
        return {
            "answer": "No Jenkins console output provided.",
            "events_count": 0,
            "status": "UNKNOWN",
            "stages": [],
        }

    lines = text.splitlines()
    stages: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    build_status = "UNKNOWN"
    stage_counts = Counter()
    current_stage = "unknown"

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        stage_match = _STAGE_PATTERN.search(line)
        if stage_match:
            stage_name = (stage_match.group(1) or stage_match.group(2) or "").strip()
            if stage_name:
                current_stage = stage_name
                stages.append(stage_name)

        status_match = _BUILD_RESULT_PATTERN.search(line)
        if status_match:
            build_status = status_match.group(1).upper()

        low = line.lower()
        if _matches_any(low, _ERROR_PATTERNS):
            errors.append(f"[{current_stage}] {line}")
            stage_counts[current_stage] += 1
        elif _matches_any(low, _WARNING_PATTERNS):
            warnings.append(f"[{current_stage}] {line}")

    unique_stages = list(dict.fromkeys(stages))
    top_error_stages = ", ".join(
        f"{name}:{count}" for name, count in stage_counts.most_common(3)
    ) or "n/a"

    findings = [
        f"Jenkins console findings{(': ' + question) if question else ''}",
        f"Build status: {build_status}",
        f"Stages detected: {len(unique_stages)}",
        f"Error lines: {len(errors)}",
        f"Warning lines: {len(warnings)}",
        f"Top error stages: {top_error_stages}",
    ]

    if unique_stages:
        findings.append("Stage flow: " + " -> ".join(unique_stages[:12]))

    if errors:
        findings.append("")
        findings.append("Top error snippets:")
        for i, item in enumerate(errors[:8], 1):
            snippet = " ".join(item.split())
            if len(snippet) > 240:
                snippet = snippet[:240] + "..."
            findings.append(f"{i}. {snippet}")

    if warnings:
        findings.append("")
        findings.append("Top warning snippets:")
        for i, item in enumerate(warnings[:5], 1):
            snippet = " ".join(item.split())
            if len(snippet) > 200:
                snippet = snippet[:200] + "..."
            findings.append(f"{i}. {snippet}")

    if not errors and build_status == "SUCCESS":
        findings.append("")
        findings.append("No failure indicators found in console output.")

    return {
        "answer": "\n".join(findings),
        "events_count": len(errors) + len(warnings),
        "status": build_status,
        "stages": unique_stages,
    }


def _matches_any(line: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, line, flags=re.IGNORECASE) for pattern in patterns)


def _normalize_console_url(url: str) -> str:
    """Prefer Jenkins /consoleText endpoint for plain text output."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Invalid Jenkins console URL")

    cleaned = url.rstrip("/")
    if cleaned.endswith("/consoleText"):
        return cleaned
    if cleaned.endswith("/console"):
        return cleaned + "Text"
    if cleaned.endswith("/consoleFull"):
        return cleaned + "Text"
    return cleaned + "/consoleText"
