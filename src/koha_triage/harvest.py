"""Harvest bugs and comments from the Koha community Bugzilla."""

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

import httpx

from .config import BUGZILLA_URL
from .db import connect, init_db

REST_BASE = f"{BUGZILLA_URL}/rest"
BUG_PAGE_LIMIT = 500


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _bugzilla_url(bug_id: int) -> str:
    return f"{BUGZILLA_URL}/show_bug.cgi?id={bug_id}"


def _build_client() -> httpx.Client:
    return httpx.Client(
        base_url=REST_BASE,
        headers={"Accept": "application/json", "User-Agent": "koha-triage/0.0.1"},
        timeout=60.0,
    )


def _fetch_bugs(
    client: httpx.Client,
    since: str | None,
    years_back: int = 5,
    on_page: Optional[Callable[[int, int], None]] = None,
) -> list[dict]:
    """Fetch all Koha bugs changed within the time window."""
    if since:
        change_since = since
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(days=365 * years_back)
        change_since = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    all_bugs: list[dict] = []
    offset = 0
    page = 1
    while True:
        params = {
            "product": "Koha",
            "last_change_time": change_since,
            "limit": BUG_PAGE_LIMIT,
            "offset": offset,
            "include_fields": (
                "id,summary,status,resolution,product,component,"
                "severity,priority,creator,assigned_to,"
                "creation_time,last_change_time,keywords"
            ),
        }
        resp = client.get("/bug", params=params)
        resp.raise_for_status()
        bugs = resp.json().get("bugs", [])
        if on_page is not None:
            on_page(page, len(bugs))
        all_bugs.extend(bugs)
        if len(bugs) < BUG_PAGE_LIMIT:
            break
        offset += BUG_PAGE_LIMIT
        page += 1
    return all_bugs


def _fetch_comments_with_retry(
    client: httpx.Client, bug_id: int, max_retries: int = 3
) -> list[dict]:
    """Fetch comments for a single bug with retry + backoff."""
    for attempt in range(max_retries):
        try:
            resp = client.get(f"/bug/{bug_id}/comment")
            resp.raise_for_status()
            data = resp.json()
            return data.get("bugs", {}).get(str(bug_id), {}).get("comments", [])
        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectError) as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f"    Retry {attempt + 1}/{max_retries} for bug {bug_id} after {wait}s ({e})", flush=True)
                time.sleep(wait)
            else:
                print(f"    Failed to fetch comments for bug {bug_id} after {max_retries} attempts", flush=True)
                return []
        except httpx.HTTPStatusError:
            return []
    return []


def upsert_bug(conn, bug: dict, description: str, harvested_at: str) -> int:
    bug_id = bug["id"]
    keywords = ",".join(bug.get("keywords", []))
    conn.execute(
        """
        INSERT INTO bugs (
            bug_id, summary, description, status, resolution, product,
            component, severity, priority, creator, assigned_to,
            creation_time, last_change_time, url, keywords, harvested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bug_id) DO UPDATE SET
            summary = excluded.summary,
            description = excluded.description,
            status = excluded.status,
            resolution = excluded.resolution,
            component = excluded.component,
            severity = excluded.severity,
            priority = excluded.priority,
            assigned_to = excluded.assigned_to,
            last_change_time = excluded.last_change_time,
            keywords = excluded.keywords,
            harvested_at = excluded.harvested_at
        """,
        (
            bug_id,
            bug["summary"],
            description,
            bug["status"],
            bug.get("resolution") or "",
            bug["product"],
            bug["component"],
            bug.get("severity") or "",
            bug.get("priority") or "",
            bug.get("creator") or "",
            bug.get("assigned_to") or "",
            bug["creation_time"],
            bug["last_change_time"],
            _bugzilla_url(bug_id),
            keywords,
            harvested_at,
        ),
    )
    row = conn.execute("SELECT id FROM bugs WHERE bug_id = ?", (bug_id,)).fetchone()
    return row["id"]


def upsert_comment(conn, internal_bug_id: int, comment: dict) -> None:
    conn.execute(
        """
        INSERT INTO comments (bug_id, bz_comment_id, count, author, body, creation_time)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(bz_comment_id) DO UPDATE SET
            body = excluded.body
        """,
        (
            internal_bug_id,
            comment["id"],
            comment.get("count", 0),
            comment.get("creator") or "",
            comment.get("text") or "",
            comment.get("creation_time") or "",
        ),
    )


def harvest(
    db_path: Path,
    years_back: int = 5,
    on_page: Optional[Callable[[int, int], None]] = None,
    on_comments: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Harvest bugs and comments from Koha Bugzilla.

    Uses `last_change_time` for incremental runs.
    """
    init_db(db_path)
    new_harvested_at = _utc_now_iso()
    counts = {"bugs": 0, "comments": 0, "new_bugs": 0, "updated_bugs": 0}

    with connect(db_path) as conn:
        row = conn.execute("SELECT last_harvested_at FROM harvest_state WHERE id = 1").fetchone()
        since = row["last_harvested_at"] if row else None

    with _build_client() as client:
        print(f"  Fetching bugs from Bugzilla (since={since or f'last {years_back} years'})...", flush=True)
        bugs = _fetch_bugs(client, since, years_back=years_back, on_page=on_page)
        counts["bugs"] = len(bugs)

        if not bugs:
            print("  No bugs to process.", flush=True)
            return counts

        with connect(db_path) as conn:
            bug_id_map: dict[int, int] = {}
            for bug in bugs:
                existing = conn.execute(
                    "SELECT id FROM bugs WHERE bug_id = ?", (bug["id"],)
                ).fetchone()
                was_new = existing is None
                description = ""
                internal_id = upsert_bug(conn, bug, description, new_harvested_at)
                bug_id_map[bug["id"]] = internal_id
                if was_new:
                    counts["new_bugs"] += 1
                else:
                    counts["updated_bugs"] += 1

            conn.execute(
                """
                INSERT INTO harvest_state (id, last_harvested_at, total_bugs)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_harvested_at = excluded.last_harvested_at,
                    total_bugs = excluded.total_bugs
                """,
                (new_harvested_at, counts["bugs"]),
            )

    return counts


def backfill_comments(
    db_path: Path,
    batch_size: int = 100,
    delay: float = 2.0,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
) -> dict:
    """Backfill descriptions and comments for bugs that don't have them yet.

    Processes in small batches with a delay between each batch to avoid
    overwhelming Bugzilla. Saves after each batch so progress is never lost.
    """
    init_db(db_path)
    counts = {"processed": 0, "comments": 0, "batches": 0, "failed": 0}

    with connect(db_path) as conn:
        pending = conn.execute(
            """
            SELECT id, bug_id FROM bugs
            WHERE description IS NULL OR description = ''
            ORDER BY bug_id
            """
        ).fetchall()

    total = len(pending)
    if total == 0:
        print("  All bugs already have descriptions.", flush=True)
        return counts

    print(f"  {total} bugs need comments. Processing in batches of {batch_size}...", flush=True)

    for batch_start in range(0, total, batch_size):
        batch = pending[batch_start : batch_start + batch_size]
        batch_num = (batch_start // batch_size) + 1
        total_batches = (total + batch_size - 1) // batch_size

        print(f"  Batch {batch_num}/{total_batches} ({len(batch)} bugs)...", flush=True)

        with _build_client() as client:
            with connect(db_path) as conn:
                for item in batch:
                    internal_id = item["id"]
                    bz_bug_id = item["bug_id"]

                    comments = _fetch_comments_with_retry(client, bz_bug_id)

                    if comments:
                        description = comments[0].get("text", "")
                        conn.execute(
                            "UPDATE bugs SET description = ? WHERE id = ?",
                            (description, internal_id),
                        )
                        for comment in comments[1:]:
                            upsert_comment(conn, internal_id, comment)
                            counts["comments"] += 1
                    else:
                        counts["failed"] += 1

                    counts["processed"] += 1

                    if on_progress is not None:
                        on_progress(counts["processed"], total, counts["comments"])

        counts["batches"] += 1

        if batch_start + batch_size < total:
            print(f"    Pausing {delay}s before next batch...", flush=True)
            time.sleep(delay)

    return counts
