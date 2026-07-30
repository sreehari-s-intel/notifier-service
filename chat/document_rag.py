"""
chat/document_rag.py
Persistent document ingestion and retrieval for chat analysis (RAG-style).
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests

ALLOWED_EXTENSIONS = {".txt", ".log", ".md", ".json", ".csv", ".yaml", ".yml"}


class DocumentStore:
    def __init__(
        self,
        project_root: Path,
        embedding_url: str = "http://localhost:11434",
        embedding_model: str = "nomic-embed-text",
        embedding_timeout_seconds: int = 30,
    ):
        self.data_dir = project_root / "chat" / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir = self.data_dir / "uploads"
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "vectors.db"
        self.embedding_url = embedding_url.rstrip("/")
        self.embedding_model = embedding_model
        self.embedding_timeout_seconds = embedding_timeout_seconds
        self._ensure_db()

    def _ensure_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    text_size INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chunks (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    FOREIGN KEY(doc_id) REFERENCES documents(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id TEXT PRIMARY KEY,
                    vector_json TEXT NOT NULL,
                    FOREIGN KEY(chunk_id) REFERENCES chunks(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id)")
            conn.commit()

    def list_documents(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    d.id,
                    d.filename,
                    d.uploaded_at,
                    COUNT(c.id) AS chunk_count,
                    SUM(CASE WHEN e.chunk_id IS NOT NULL THEN 1 ELSE 0 END) AS embedded_count
                FROM documents d
                LEFT JOIN chunks c ON c.doc_id = d.id
                LEFT JOIN embeddings e ON e.chunk_id = c.id
                GROUP BY d.id, d.filename, d.uploaded_at
                ORDER BY d.uploaded_at DESC
                """
            ).fetchall()
        return [
            {
                "id": row[0],
                "filename": row[1],
                "uploaded_at": row[2],
                "chunks": int(row[3] or 0),
                "embedded_chunks": int(row[4] or 0),
                "pending_chunks": max(0, int(row[3] or 0) - int(row[4] or 0)),
            }
            for row in rows
        ]

    def add_document(self, filename: str, text: str, create_embeddings: bool = True) -> dict:
        doc_id = str(uuid.uuid4())
        uploaded_at = datetime.now(timezone.utc).isoformat()
        safe_filename = _safe_filename(filename)
        file_path = self.uploads_dir / f"{doc_id}__{safe_filename}"
        file_path.write_text(text, encoding="utf-8")

        chunks = _chunk_text(text)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO documents (id, filename, file_path, uploaded_at, text_size) VALUES (?, ?, ?, ?, ?)",
                (doc_id, filename, str(file_path), uploaded_at, len(text)),
            )

            for i, chunk_text in enumerate(chunks):
                chunk_id = f"{doc_id}:{i + 1}"
                conn.execute(
                    "INSERT INTO chunks (id, doc_id, chunk_index, text) VALUES (?, ?, ?, ?)",
                    (chunk_id, doc_id, i, chunk_text),
                )
                if create_embeddings:
                    vector = self._embed_text(chunk_text)
                    conn.execute(
                        "INSERT INTO embeddings (chunk_id, vector_json) VALUES (?, ?)",
                        (chunk_id, json.dumps(vector)),
                    )
            conn.commit()

        return {
            "id": doc_id,
            "filename": filename,
            "uploaded_at": uploaded_at,
            "chunks": len(chunks),
            "text_size": len(text),
            "saved_path": str(file_path),
            "pending_embeddings": 0 if create_embeddings else len(chunks),
        }

    def retrieve(self, question: str, top_k: int = 8) -> list[dict]:
        question = (question or "").strip()
        if not question:
            return []

        query_vector = self._embed_text(question)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.doc_id, c.text, d.filename, d.uploaded_at, e.vector_json
                FROM chunks c
                JOIN documents d ON d.id = c.doc_id
                LEFT JOIN embeddings e ON e.chunk_id = c.id
                """
            ).fetchall()

        scored = []
        for row in rows:
            chunk_id, doc_id, text, filename, uploaded_at, vector_json = row
            vector = json.loads(vector_json) if vector_json else _fallback_embedding(text)
            score = _cosine_similarity(query_vector, vector)
            if score <= 0:
                continue
            scored.append(
                (
                    score,
                    {
                        "doc_id": doc_id,
                        "filename": filename,
                        "chunk_id": chunk_id,
                        "text": text,
                        "uploaded_at": uploaded_at,
                    },
                )
            )

        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[: max(1, top_k)]]

    def embed_document(self, doc_id: str) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT c.id, c.text
                FROM chunks c
                LEFT JOIN embeddings e ON e.chunk_id = c.id
                WHERE c.doc_id = ? AND e.chunk_id IS NULL
                ORDER BY c.chunk_index ASC
                """,
                (doc_id,),
            ).fetchall()

            embedded = 0
            for chunk_id, chunk_text in rows:
                vector = self._embed_text(chunk_text)
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (chunk_id, vector_json) VALUES (?, ?)",
                    (chunk_id, json.dumps(vector)),
                )
                embedded += 1

            conn.commit()

        return {"doc_id": doc_id, "embedded_chunks": embedded}

    def embed_pending_documents(self, max_docs: int = 10) -> dict:
        with sqlite3.connect(self.db_path) as conn:
            doc_rows = conn.execute(
                """
                SELECT DISTINCT c.doc_id
                FROM chunks c
                LEFT JOIN embeddings e ON e.chunk_id = c.id
                WHERE e.chunk_id IS NULL
                LIMIT ?
                """,
                (max_docs,),
            ).fetchall()

        total = 0
        for (doc_id,) in doc_rows:
            result = self.embed_document(doc_id)
            total += int(result.get("embedded_chunks", 0))

        return {"documents_processed": len(doc_rows), "chunks_embedded": total}

    def _embed_text(self, text: str) -> list[float]:
        text = (text or "").strip()
        if not text:
            return _fallback_embedding("")

        try:
            resp = requests.post(
                f"{self.embedding_url}/api/embeddings",
                json={"model": self.embedding_model, "prompt": text},
                timeout=self.embedding_timeout_seconds,
            )
            if resp.status_code == 200:
                data = resp.json()
                emb = data.get("embedding")
                if isinstance(emb, list) and emb:
                    return [float(x) for x in emb]
        except Exception:
            pass

        # Deterministic fallback so retrieval still works when embedding server is unavailable.
        return _fallback_embedding(text)


def validate_upload(filename: str) -> None:
    if not filename:
        raise ValueError("File name is empty")
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
        )


def decode_file_content(data: bytes) -> str:
    if not data:
        return ""

    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


def build_document_findings(question: str, chunks: Iterable[dict], total_docs: int) -> str:
    chunks = list(chunks)
    if not chunks:
        return "No relevant content found in uploaded documents for this question."

    unique_docs = sorted({str(c.get("filename", "unknown")) for c in chunks})
    lines = [
        f"Findings for: {question}",
        f"Documents analyzed: {total_docs}",
        f"Relevant documents: {', '.join(unique_docs)}",
        "",
        "Top matching excerpts:",
    ]

    for i, chunk in enumerate(chunks, 1):
        text = " ".join(str(chunk.get("text", "")).split())
        if len(text) > 240:
            text = text[:240] + "..."
        lines.append(f"{i}. [{chunk.get('filename', 'unknown')}] {text}")

    return "\n".join(lines)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_\-]+", (text or "").lower()))


def _overlap_score(query_tokens: set[str], text_tokens: set[str]) -> int:
    if not query_tokens or not text_tokens:
        return 0
    return len(query_tokens.intersection(text_tokens))


def _chunk_text(text: str, chunk_size: int = 1200, overlap: int = 180) -> list[str]:
    compact = (text or "").strip()
    if not compact:
        return []

    chunks: list[str] = []
    start = 0
    step = max(1, chunk_size - overlap)
    while start < len(compact):
        end = start + chunk_size
        chunks.append(compact[start:end])
        start += step
    return chunks


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", name).strip("._")
    return cleaned or "document.txt"


def _fallback_embedding(text: str, dim: int = 128) -> list[float]:
    vec = [0.0] * dim
    for token in _tokens(text):
        slot = hash(token) % dim
        vec[slot] += 1.0
    return _normalize(vec)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(x * x for x in a[:n]) ** 0.5
    nb = sum(x * x for x in b[:n]) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _normalize(v: list[float]) -> list[float]:
    mag = sum(x * x for x in v) ** 0.5
    if mag == 0:
        return v
    return [x / mag for x in v]
