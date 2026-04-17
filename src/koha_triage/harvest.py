"""Harvest bugs and comments from the Koha community Bugzilla."""

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


def _fetch_bug_description(client: httpx.Client, bug_id: int) -> str:
    """Fetch comment 0 (the description) for a single bug."""
    resp = client.get(f"/bug/{bug_id}/comment")
    resp.raise_for_status()
    data = resp.json()
    comments = data.get("bugs", {}).get(str(bug_id), {}).get("comments", [])
    if comments:
        return comments[0].get("text", "")
    return ""


def _fetch_comments_batch(
    client: httpx.Client,
    bug_ids: list[int],
    on_page: Optional[Callable[[int, int], None]] = None,
) -> dict[int, list[dict]]:
    """Fetch comments for a batch of bugs. Returns {bug_id: [comments]}."""
    result: dict[int, list[dict]] = {}
    for i, bug_id in enumerate(bug_ids):
        try:
            resp = client.get(f"/bug/{bug_id}/comment")
            resp.raise_for_status()
            data = resp.json()
            comments = data.get("bugs", {}).get(str(bug_id), {}).get("comments", [])
            result[bug_id] = comments
        except httpx.HTTPStatusError:
            result[bug_id] = []
        if on_page is not None and (i + 1) % 50 == 0:
            on_page(i + 1, len(bug_ids))
    return result


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

        print(f"  Fetching comments for {len(bugs)} bugs...", flush=True)
        bug_ids = [b["id"] for b in bugs]
        all_comments = _fetch_comments_batch(client, bug_ids, on_page=on_comments)

        with connect(db_path) as conn:
            for bz_bug_id, comments in all_comments.items():
                internal_id = bug_id_map.get(bz_bug_id)
                if internal_id is None:
                    continue
                if comments:
                    description = comments[0].get("text", "")
                    conn.execute(
                        "UPDATE bugs SET description = ? WHERE id = ?",
                        (description, internal_id),
                    )
                for comment in comments[1:]:
                    upsert_comment(conn, internal_id, comment)
                    counts["comments"] += 1

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
