import hashlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

from .db import connect, init_db


def _embedding_text(summary: str, description: str | None) -> str:
    parts = [summary]
    if description:
        parts.append(description.strip())
    return "\n\n".join(parts)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vec, axis=-1, keepdims=True)
    norm = np.where(norm == 0, 1.0, norm)
    return vec / norm


def _serialize_embedding(vec: np.ndarray) -> bytes:
    return vec.astype(np.float32).tobytes()


def deserialize_embedding(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.float32)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def embed_pending(
    db_path: Path,
    model_name: str,
    batch_size: int = 32,
    chunk_size: int = 500,
    on_progress=None,
) -> dict:
    """Embed bugs in chunks to keep memory usage bounded.

    chunk_size controls how many bugs are processed and saved per round.
    batch_size controls texts per fastembed batch within each chunk.
    """
    init_db(db_path)
    embedded_at = _utc_now_iso()
    counts = {"embedded": 0, "skipped": 0, "total": 0}

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, summary, description, embed_text_hash FROM bugs"
        ).fetchall()
        counts["total"] = len(rows)

        pending_ids: list[int] = []
        pending_texts: list[str] = []
        pending_hashes: list[str] = []
        for row in rows:
            text = _embedding_text(row["summary"], row["description"])
            h = _text_hash(text)
            if row["embed_text_hash"] == h and row["embed_text_hash"] is not None:
                counts["skipped"] += 1
                continue
            pending_ids.append(row["id"])
            pending_texts.append(text)
            pending_hashes.append(h)

    if not pending_ids:
        return counts

    if on_progress is not None:
        on_progress("loading_model", model_name)
    model = TextEmbedding(model_name=model_name)

    if on_progress is not None:
        on_progress("embedding", len(pending_ids))

    # Process in chunks to avoid OOM on small machines
    total_pending = len(pending_ids)
    for chunk_start in range(0, total_pending, chunk_size):
        chunk_end = min(chunk_start + chunk_size, total_pending)
        chunk_texts = pending_texts[chunk_start:chunk_end]
        chunk_ids = pending_ids[chunk_start:chunk_end]
        chunk_hashes = pending_hashes[chunk_start:chunk_end]

        vectors = list(model.embed(chunk_texts, batch_size=batch_size))
        matrix = _normalize(np.array(vectors, dtype=np.float32))

        with connect(db_path) as conn:
            for i, bug_id in enumerate(chunk_ids):
                conn.execute(
                    """
                    UPDATE bugs
                    SET embedding = ?, embedded_at = ?, embed_text_hash = ?
                    WHERE id = ?
                    """,
                    (_serialize_embedding(matrix[i]), embedded_at, chunk_hashes[i], bug_id),
                )
                counts["embedded"] += 1

        if on_progress is not None:
            on_progress("chunk_done", f"{chunk_end}/{total_pending}")

        # Free memory before next chunk
        del vectors, matrix

    return counts
