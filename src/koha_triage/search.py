from pathlib import Path
from typing import TypedDict

import numpy as np
from fastembed import TextEmbedding

from .db import connect, init_db
from .embed import _normalize, deserialize_embedding


class SearchResult(TypedDict):
    bug_id: int
    internal_id: int
    summary: str
    url: str
    status: str
    resolution: str
    component: str
    severity: str
    priority: str
    creator: str
    score: float
    description_snippet: str
    description: str


SNIPPET_CHARS = 300


class NoEmbeddingsError(RuntimeError):
    pass


def _embed_query(model_name: str, query: str) -> np.ndarray:
    model = TextEmbedding(model_name=model_name)
    vec = next(model.embed([query]))
    return _normalize(np.array(vec, dtype=np.float32))


def search(
    db_path: Path,
    query: str,
    model_name: str,
    top_k: int = 5,
    component: str | None = None,
    status: str | None = None,
) -> list[SearchResult]:
    init_db(db_path)
    query_vec = _embed_query(model_name, query)

    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, bug_id, summary, description, url, status,
                   resolution, component, severity, priority, creator, embedding
            FROM bugs
            WHERE embedding IS NOT NULL
            """
        ).fetchall()

    if component:
        rows = [r for r in rows if r["component"] == component]
    if status:
        rows = [r for r in rows if r["status"] == status]

    if not rows:
        raise NoEmbeddingsError(
            "No embedded bugs. Run `koha-triage embed` first to index bugs."
        )

    matrix = np.vstack([deserialize_embedding(r["embedding"]) for r in rows])
    scores = matrix @ query_vec
    top_indices = np.argsort(-scores)[:top_k]

    results: list[SearchResult] = []
    for idx in top_indices:
        row = rows[int(idx)]
        desc = row["description"] or ""
        snippet = desc.strip().replace("\r\n", "\n")
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS].rstrip() + "..."
        results.append(
            SearchResult(
                bug_id=row["bug_id"],
                internal_id=row["id"],
                summary=row["summary"],
                url=row["url"],
                status=row["status"],
                resolution=row["resolution"] or "",
                component=row["component"],
                severity=row["severity"] or "",
                priority=row["priority"] or "",
                creator=row["creator"] or "",
                score=float(scores[int(idx)]),
                description_snippet=snippet,
                description=desc,
            )
        )
    return results
