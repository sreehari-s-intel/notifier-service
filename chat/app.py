"""
chat/app.py
FastAPI web app for multi-source log analysis.
Run with: python -m chat.app
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from collections import Counter
from pathlib import Path
from typing import Any

import uvicorn
import yaml
from elasticsearch import Elasticsearch
from fastapi import BackgroundTasks, FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

try:
    from chat.llm import parse_user_query, summarize_results, summarize_document_results
    from chat.query_builder import build_es_query, execute_query
    from chat.document_rag import (
        DocumentStore,
        validate_upload,
        decode_file_content,
        build_document_findings,
    )
    from chat.jenkins_console_handler import analyze_jenkins_console, fetch_jenkins_console_output
except ModuleNotFoundError:
    # Allows running from the chat directory with: python -m app
    from llm import parse_user_query, summarize_results, summarize_document_results
    from query_builder import build_es_query, execute_query
    from document_rag import (
        DocumentStore,
        validate_upload,
        decode_file_content,
        build_document_findings,
    )
    from jenkins_console_handler import analyze_jenkins_console, fetch_jenkins_console_output

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

CHAT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CHAT_DIR.parent


def load_config(path: str | None = None) -> dict:
    if path is None:
        path = str(PROJECT_ROOT / "config" / "config.yaml")
    with open(path) as f:
        return yaml.safe_load(f)


CFG = load_config()
CHAT_CFG = CFG.get("chat", {})
CHAT_PASSWORD_HASH = hashlib.sha256(CHAT_CFG.get("password", "admin").encode()).hexdigest()
OLLAMA_URL = CHAT_CFG.get("ollama_url", "http://localhost:11434")
OLLAMA_MODEL = CHAT_CFG.get("ollama_model", "llama3.2")
OLLAMA_PARSE_TIMEOUT_SECONDS = int(CHAT_CFG.get("ollama_parse_timeout_seconds", 45))
OLLAMA_SUMMARY_TIMEOUT_SECONDS = int(CHAT_CFG.get("ollama_summary_timeout_seconds", 90))
RAG_TOP_K = int(CHAT_CFG.get("rag_top_k", 8))
JENKINS_CFG = CHAT_CFG.get("jenkins", {})
RAG_EMBEDDING_URL = CHAT_CFG.get("rag_embedding_url", OLLAMA_URL)
RAG_EMBEDDING_MODEL = CHAT_CFG.get("rag_embedding_model", "nomic-embed-text")
RAG_EMBEDDING_TIMEOUT_SECONDS = int(CHAT_CFG.get("rag_embedding_timeout_seconds", 30))

DOC_STORE = DocumentStore(
    PROJECT_ROOT,
    embedding_url=RAG_EMBEDDING_URL,
    embedding_model=RAG_EMBEDDING_MODEL,
    embedding_timeout_seconds=RAG_EMBEDDING_TIMEOUT_SECONDS,
)

app = FastAPI(title="Log Chat API")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32)),
)
templates = Jinja2Templates(directory=str(CHAT_DIR / "templates"))


def _is_authenticated(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


def _unauthorized_response() -> JSONResponse:
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


def get_es_client() -> Elasticsearch:
    es_cfg = CFG["elasticsearch"]
    return Elasticsearch(
        hosts=[es_cfg["host"]],
        basic_auth=(es_cfg["username"], es_cfg["password"]),
        verify_certs=False,
    )


def _build_findings_response(question: str, hits: list[dict], max_items: int = 8) -> str:
    if not hits:
        return f"No matching logs found for: {question}"

    level_counter = Counter((str(h.get("level", "")).strip().lower() or "unknown") for h in hits)
    top_levels = ", ".join(f"{lvl}:{count}" for lvl, count in level_counter.most_common(3))

    lines = [
        f"Findings for: {question}",
        f"Total matches: {len(hits)}",
        f"Top levels: {top_levels}" if top_levels else "Top levels: n/a",
        "",
        "Recent matching entries:",
    ]

    for i, hit in enumerate(hits[:max_items], 1):
        ts = hit.get("timestamp", "?")
        idx = hit.get("index", "?")
        message = " ".join(str(hit.get("message", "")).split())
        if len(message) > 220:
            message = message[:220] + "..."
        lines.append(f"{i}. [{ts}] ({idx}) {message}")

    remaining = len(hits) - min(len(hits), max_items)
    if remaining > 0:
        lines.append(f"... and {remaining} more matching entries.")

    return "\n".join(lines)


def _resolve_target_indices(question: str, parsed: dict[str, Any], cfg: dict) -> str:
    es_cfg = cfg.get("elasticsearch", {})
    configured = es_cfg.get("indices")
    default_index = es_cfg.get("index", "*")

    if isinstance(configured, list) and configured:
        candidates = [str(i).strip() for i in configured if str(i).strip()]
    else:
        candidates = [default_index]

    alias_to_index = {}
    for idx in candidates:
        alias = idx.replace("*", "").strip("-_").lower()
        if alias:
            alias_to_index[alias] = idx

    parsed_indices = parsed.get("indices") if isinstance(parsed, dict) else None
    if isinstance(parsed_indices, list):
        requested = [str(i).strip() for i in parsed_indices if str(i).strip()]
    elif isinstance(parsed_indices, str) and parsed_indices.strip():
        requested = [parsed_indices.strip()]
    else:
        requested = []

    normalized_requested = []
    for item in requested:
        key = item.replace("*", "").strip("-_").lower()
        normalized_requested.append(alias_to_index.get(key, item))
    requested = normalized_requested

    q = question.lower()
    if not requested:
        for idx in candidates:
            base = idx.replace("*", "").replace("-", "").replace("_", "").lower()
            if base and base in q:
                requested.append(idx)

    if not requested:
        return ",".join(candidates)

    candidate_set = set(candidates)
    filtered = [i for i in requested if i in candidate_set]
    if filtered:
        return ",".join(filtered)

    return ",".join(requested)


def _analyze_documents(question: str) -> tuple[str, int, list[dict]]:
    chunks = DOC_STORE.retrieve(question, top_k=RAG_TOP_K)
    total_docs = len(DOC_STORE.list_documents())
    findings = build_document_findings(question, chunks, total_docs=total_docs)
    return findings, len(chunks), chunks


def _should_use_documents(source: str, question: str) -> bool:
    source = (source or "auto").lower().strip()
    if source == "documents":
        return True
    if source == "elasticsearch":
        return False

    q = question.lower()
    doc_terms = ["document", "file", "upload", "pdf", "report", "doc"]
    return any(t in q for t in doc_terms)


def _should_use_jenkins_console(source: str, question: str) -> bool:
    source = (source or "auto").lower().strip()
    if source == "jenkins_console":
        return True
    if source in {"documents", "elasticsearch"}:
        return False

    q = question.lower()
    console_terms = ["jenkins console", "pipeline output", "console output", "jenkins build log"]
    return any(t in q for t in console_terms)


def _resolve_jenkins_console_text(
    console_output: str,
    console_url: str,
    username: str = "",
    password: str = "",
    api_token: str = "",
    bearer_token: str = "",
) -> str:
    text = str(console_output or "")
    if text.strip():
        return text

    url = str(console_url or "").strip()
    if not url:
        raise ValueError("Provide Jenkins console_output or console_url.")

    cfg_username = str(JENKINS_CFG.get("username", ""))
    cfg_password = str(JENKINS_CFG.get("password", ""))
    cfg_api_token = str(JENKINS_CFG.get("api_token", ""))
    cfg_bearer_token = str(JENKINS_CFG.get("bearer_token", ""))

    username = username or cfg_username
    password = password or cfg_password
    api_token = api_token or cfg_api_token
    bearer_token = bearer_token or cfg_bearer_token
    verify_ssl = bool(JENKINS_CFG.get("verify_ssl", True))
    timeout_seconds = int(JENKINS_CFG.get("timeout_seconds", 30))

    return fetch_jenkins_console_output(
        console_url=url,
        username=username,
        password=password,
        api_token=api_token,
        bearer_token=bearer_token,
        verify_ssl=verify_ssl,
        timeout_seconds=timeout_seconds,
    )


def _background_embed_document(doc_id: str) -> None:
    try:
        result = DOC_STORE.embed_document(doc_id)
        logger.info("Background embedding completed for %s: %s", doc_id, result)
    except Exception as exc:
        logger.error("Background embedding failed for %s: %s", doc_id, exc)


@app.get("/login")
async def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html", context={})


@app.post("/login")
async def login(request: Request):
    data = await request.json()
    password = str(data.get("password", ""))
    password_hash = hashlib.sha256(password.encode()).hexdigest()

    if secrets.compare_digest(password_hash, CHAT_PASSWORD_HASH):
        request.session["authenticated"] = True
        return JSONResponse({"ok": True})
    return JSONResponse({"error": "Invalid password"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return JSONResponse({"ok": True})


@app.get("/")
async def index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request=request, name="chat.html", context={})


@app.post("/api/chat")
async def chat(request: Request):
    if not _is_authenticated(request):
        return _unauthorized_response()

    data = await request.json()
    question = str(data.get("question", "")).strip()
    source = str(data.get("source", "auto")).strip().lower()
    console_output = data.get("console_output", "")
    console_url = data.get("console_url", "")
    jenkins_username = str(data.get("jenkins_username", "")).strip()
    jenkins_password = str(data.get("jenkins_password", ""))

    if not question:
        return JSONResponse({"error": "Please enter a question."}, status_code=400)

    if len(question) > 2000:
        return JSONResponse({"error": "Question too long (max 2000 characters)."}, status_code=400)

    try:
        if _should_use_jenkins_console(source, question):
            try:
                console_text = await run_in_threadpool(
                    _resolve_jenkins_console_text,
                    str(console_output),
                    str(console_url),
                    jenkins_username,
                    jenkins_password,
                )
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except ValueError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)

            result = await run_in_threadpool(analyze_jenkins_console, console_text, question)
            return JSONResponse(
                {
                    "answer": result["answer"],
                    "hits_count": result["events_count"],
                    "query_params": {"source": "jenkins_console"},
                    "source_used": "jenkins_console",
                    "build_status": result["status"],
                    "stages": result["stages"],
                }
            )

        if _should_use_documents(source, question):
            fallback_findings, chunks_count, chunks = await run_in_threadpool(_analyze_documents, question)
            if chunks_count > 0:
                try:
                    answer = await run_in_threadpool(
                        summarize_document_results,
                        question,
                        chunks,
                        OLLAMA_MODEL,
                        OLLAMA_URL,
                        OLLAMA_SUMMARY_TIMEOUT_SECONDS,
                    )
                except RuntimeError as exc:
                    logger.warning("Document LLM summary failed, returning deterministic findings: %s", exc)
                    answer = fallback_findings
            else:
                answer = fallback_findings

            return JSONResponse(
                {
                    "answer": answer,
                    "hits_count": chunks_count,
                    "query_params": {"source": "documents"},
                    "source_used": "documents",
                }
            )

        logger.info("Processing question: %s...", question[:100])
        parsed = await run_in_threadpool(
            parse_user_query,
            question,
            OLLAMA_MODEL,
            OLLAMA_URL,
            OLLAMA_PARSE_TIMEOUT_SECONDS,
        )
        logger.info("Parsed query: %s", parsed)

        es_query = await run_in_threadpool(build_es_query, parsed)
        index_pattern = _resolve_target_indices(question, parsed, CFG)
        client = get_es_client()
        hits = await run_in_threadpool(execute_query, client, index_pattern, es_query)

        analysis_type = str(parsed.get("analysis_type", "summary")).lower().strip()
        if analysis_type == "count":
            answer = _build_findings_response(question, hits)
        else:
            try:
                answer = await run_in_threadpool(
                    summarize_results,
                    question,
                    hits,
                    OLLAMA_MODEL,
                    OLLAMA_URL,
                    OLLAMA_SUMMARY_TIMEOUT_SECONDS,
                )
            except RuntimeError as exc:
                logger.warning("LLM summary failed, returning direct findings: %s", exc)
                answer = _build_findings_response(question, hits)

        return JSONResponse(
            {
                "answer": answer,
                "hits_count": len(hits),
                "query_params": parsed,
                "source_used": "elasticsearch",
                "index_used": index_pattern,
            }
        )

    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:
        logger.error("Chat error: %s", exc, exc_info=True)
        return JSONResponse({"error": f"An error occurred: {exc}"}, status_code=500)


@app.post("/api/documents/upload")
async def upload_documents(request: Request, background_tasks: BackgroundTasks, files: list[UploadFile] = File(...)):
    if not _is_authenticated(request):
        return _unauthorized_response()

    if not files:
        return JSONResponse({"error": "No files uploaded. Use form-data key 'files'."}, status_code=400)

    uploaded = []
    rejected = []
    for file in files:
        filename = (file.filename or "").strip()
        try:
            validate_upload(filename)
            content = decode_file_content(await file.read())
            if not content.strip():
                raise ValueError("File is empty")

            doc_meta = await run_in_threadpool(DOC_STORE.add_document, filename, content, False)
            uploaded.append(doc_meta)
            background_tasks.add_task(_background_embed_document, doc_meta["id"])
        except Exception as exc:
            rejected.append({"filename": filename or "unknown", "error": str(exc)})

    return JSONResponse(
        {
            "uploaded": uploaded,
            "rejected": rejected,
            "uploaded_count": len(uploaded),
            "message": "Documents saved. Embeddings are being generated in the background.",
        }
    )


@app.get("/api/documents")
async def list_documents(request: Request):
    if not _is_authenticated(request):
        return _unauthorized_response()

    docs = await run_in_threadpool(DOC_STORE.list_documents)
    return JSONResponse({"documents": docs, "count": len(docs)})


@app.post("/api/documents/reindex")
async def reindex_documents(request: Request, background_tasks: BackgroundTasks):
    if not _is_authenticated(request):
        return _unauthorized_response()

    def _job() -> None:
        result = DOC_STORE.embed_pending_documents(max_docs=50)
        logger.info("Background document reindex finished: %s", result)

    background_tasks.add_task(_job)
    return JSONResponse({"ok": True, "message": "Started background embedding for pending documents."})


@app.post("/api/jenkins/console/analyze")
async def analyze_jenkins_console_route(request: Request):
    if not _is_authenticated(request):
        return _unauthorized_response()

    data = await request.json()
    question = str(data.get("question", "Jenkins pipeline console analysis")).strip()
    console_output = str(data.get("console_output", ""))
    console_url = str(data.get("console_url", ""))
    jenkins_username = str(data.get("jenkins_username", "")).strip()
    jenkins_password = str(data.get("jenkins_password", ""))

    try:
        console_text = await run_in_threadpool(
            _resolve_jenkins_console_text,
            console_output,
            console_url,
            jenkins_username,
            jenkins_password,
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    result = await run_in_threadpool(analyze_jenkins_console, console_text, question)
    return JSONResponse(
        {
            "answer": result["answer"],
            "events_count": result["events_count"],
            "build_status": result["status"],
            "stages": result["stages"],
            "source_used": "jenkins_console",
        }
    )


if __name__ == "__main__":
    host = CHAT_CFG.get("host", "0.0.0.0")
    port = int(CHAT_CFG.get("port", 5001))
    debug = bool(CHAT_CFG.get("debug", False))
    logger.info("Starting Log Chat (FastAPI) on http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, reload=debug)
