"""Generate code fixes via Claude and produce git format-patch output."""

import base64
import difflib
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from pydantic import BaseModel, Field

from .config import BUGZILLA_URL, KOHA_GITHUB_MIRROR, settings
from .db import connect, init_db
from .recommend import Recommendation, get_stored_recommendation

GITHUB_API = "https://api.github.com"
BUGZILLA_REST = f"{BUGZILLA_URL}/rest"


class FileFix(BaseModel):
    file_path: str = Field(..., description="Path relative to repo root.")
    explanation: str = Field(..., description="What changed and why, 2-3 sentences.")
    content: str = Field(..., description="The changed code sections with ~5 lines of context.")


class CodeFixResponse(BaseModel):
    fixes: list[FileFix]
    commit_message: str = Field(..., description="A concise commit message in Koha format: Bug XXXXX: description")


SYSTEM_PROMPT = """You are implementing a code fix for the Koha ILS (Integrated Library System).

You will receive:
1. A bug description from Bugzilla
2. A fix recommendation (approach, affected files, guidelines)
3. Relevant sections of the file(s) to modify

Your job: return ONLY the changed code — the specific subroutines, blocks, or sections that need modification. Include ~5 lines of unchanged context above and below each change so the human can locate where it goes. Do NOT return the entire file. Do NOT include licence headers, `use` statement blocks, or POD unless you are specifically changing them.

Think of your output like a git commit — just the lines that change plus enough context to apply them.

Key Koha conventions:
- Koha:: namespace for new code (not C4::)
- Try::Tiny for error handling (never eval)
- Koha::Logger for logging (never warn)
- SQL placeholders (?) for all user input
- Filter all template variables: [% var | html %]
- CSRF-TOKEN header for POST/PUT/DELETE in JS
- 4 spaces indentation (not tabs)
- snake_case for subroutine names"""


REBASE_SYSTEM_PROMPT = """You are rebasing a stale patch for the Koha ILS (Integrated Library System).

You will receive:
1. A bug description from Bugzilla
2. An existing patch that no longer applies cleanly to the current codebase
3. The current content of the file(s) the patch modifies

Your job: produce an UPDATED version of the patch that applies cleanly to the current code. Preserve the original author's intent and approach — you are rebasing, not rewriting. Return ONLY the changed code sections with ~5 lines of context, not full files.

The commit message MUST credit the original patch author. Use this format:
  Bug XXXXX: <original description> [rebased]

Key Koha conventions:
- Koha:: namespace for new code (not C4::)
- Try::Tiny for error handling (never eval)
- Koha::Logger for logging (never warn)
- SQL placeholders (?) for all user input
- Filter all template variables: [% var | html %]
- CSRF-TOKEN header for POST/PUT/DELETE in JS
- 4 spaces indentation (not tabs)
- snake_case for subroutine names"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_file_from_mirror(path: str, token: str | None = None) -> tuple[str, str]:
    """Fetch a file from the Koha GitHub mirror. Returns (content, sha)."""
    headers: dict = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{GITHUB_API}/repos/{KOHA_GITHUB_MIRROR}/contents/{path}"
    resp = httpx.get(url, headers=headers, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return content, data["sha"]


def _truncate_file(content: str, path: str, max_lines: int = 500) -> str:
    """Truncate large files for context, keeping first N lines."""
    lines = content.splitlines()
    if len(lines) > max_lines:
        snippet = "\n".join(lines[:max_lines])
        return (
            f"### {path} (first {max_lines} of {len(lines)} lines)\n"
            f"```\n{snippet}\n```\n"
            f"(... {len(lines) - max_lines} more lines truncated)"
        )
    return f"### {path}\n```\n{content}\n```"


# ---------------------------------------------------------------------------
# Bugzilla attachment fetching
# ---------------------------------------------------------------------------

def fetch_patches_from_bugzilla(bug_id: int) -> list[dict]:
    """Fetch patch attachments for a bug from Bugzilla REST API.

    Returns list of dicts with keys: id, file_name, creator, creation_time,
    summary, data (decoded text), is_obsolete.
    Sorted newest first, non-obsolete patches preferred.
    """
    url = f"{BUGZILLA_REST}/bug/{bug_id}/attachment"
    resp = httpx.get(url, headers={"Accept": "application/json"}, timeout=30.0)
    resp.raise_for_status()
    raw_attachments = resp.json().get("bugs", {}).get(str(bug_id), [])

    patches = []
    for att in raw_attachments:
        if not att.get("is_patch"):
            continue
        try:
            data = base64.b64decode(att["data"]).decode("utf-8", errors="replace")
        except Exception:
            continue
        patches.append({
            "id": att["id"],
            "file_name": att.get("file_name", ""),
            "creator": att.get("creator", ""),
            "creation_time": att.get("creation_time", ""),
            "summary": att.get("summary", att.get("description", "")),
            "data": data,
            "is_obsolete": bool(att.get("is_obsolete")),
        })

    # Sort: non-obsolete first, then by creation_time descending
    patches.sort(key=lambda p: (p["is_obsolete"], p["creation_time"]), reverse=True)
    # Flip so non-obsolete (False=0) come first
    patches.sort(key=lambda p: p["is_obsolete"])

    return patches


def _extract_files_from_patch(patch_data: str) -> list[str]:
    """Extract file paths mentioned in a unified diff patch."""
    files = []
    for line in patch_data.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path and path != "/dev/null":
                files.append(path)
        elif line.startswith("diff --git"):
            parts = line.split()
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                if path not in files:
                    files.append(path)
    return files


# ---------------------------------------------------------------------------
# Standard code fix generation
# ---------------------------------------------------------------------------

def generate_code_fix(
    db_path: Path,
    bug_internal_id: int,
    api_key: str,
    github_token: str | None = None,
    model: str = "claude-opus-4-6",
    max_files: int = 3,
) -> CodeFixResponse:
    init_db(db_path)
    stored = get_stored_recommendation(db_path, bug_internal_id)
    if stored is None:
        raise ValueError("No recommendation exists. Generate one first.")

    rec, _model, _created = stored

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Bug {bug_internal_id} not found")

    bug = dict(row)

    file_contents: list[dict] = []
    for path in rec.likely_files[:max_files]:
        try:
            content, sha = fetch_file_from_mirror(path, token=github_token)
            file_contents.append({"path": path, "content": content, "sha": sha})
        except Exception as e:
            file_contents.append({"path": path, "content": None, "error": str(e)})

    truncated_context = []
    for fc in file_contents:
        if fc.get("content"):
            truncated_context.append(_truncate_file(fc["content"], fc["path"]))
        else:
            truncated_context.append(f"### {fc['path']}\n(Could not fetch: {fc.get('error', 'unknown')})")

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Bug {bug['bug_id']}\n"
                    f"**Summary:** {bug['summary']}\n"
                    f"**Status:** {bug['status']}\n"
                    f"**Component:** {bug['component']}\n\n"
                    f"**Description:**\n{bug.get('description') or '(empty)'}\n\n"
                    f"---\n\n## Recommendation\n\n"
                    f"**Fix approach:** {rec.fix_approach}\n\n"
                    f"**Key guidelines:** {', '.join(rec.key_guidelines)}\n\n"
                    f"**Test plan:** {rec.test_plan}\n\n"
                    f"---\n\n## Current file contents\n\n"
                    + "\n\n".join(truncated_context)
                    + "\n\n---\n\nProduce the modified code for each file that needs changes."
                ),
            }
        ],
        output_format=CodeFixResponse,
    ) as stream:
        final = stream.get_final_message()

    fix = final.parsed_output
    if fix is None:
        # Fallback: manually parse from response text
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        fix = CodeFixResponse.model_validate_json(text)

    _store_fix(db_path, bug_internal_id, bug, fix, file_contents, model)
    return fix


# ---------------------------------------------------------------------------
# Rebase stale patch
# ---------------------------------------------------------------------------

def rebase_patch(
    db_path: Path,
    bug_internal_id: int,
    api_key: str,
    github_token: str | None = None,
    model: str = "claude-opus-4-6",
) -> CodeFixResponse:
    """Fetch the latest patch from Bugzilla, rebase it against current code."""
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Bug {bug_internal_id} not found")

    bug = dict(row)

    # Fetch comments — they often explain why the patch stopped applying
    with connect(db_path) as conn:
        comment_rows = conn.execute(
            "SELECT author, body, creation_time FROM comments WHERE bug_id = ? ORDER BY creation_time",
            (bug_internal_id,),
        ).fetchall()
    comments = [dict(c) for c in comment_rows]

    # Fetch patches from Bugzilla
    patches = fetch_patches_from_bugzilla(bug["bug_id"])
    if not patches:
        raise ValueError(f"No patch attachments found on Bug {bug['bug_id']}")

    # Use the most recent non-obsolete patch, or fall back to most recent overall
    non_obsolete = [p for p in patches if not p["is_obsolete"]]
    patch = non_obsolete[0] if non_obsolete else patches[0]

    original_author = patch["creator"]
    patch_data = patch["data"]
    patch_summary = patch["summary"]

    # Figure out which files the patch touches
    patch_files = _extract_files_from_patch(patch_data)

    # Fetch current versions of those files
    file_contents: list[dict] = []
    for path in patch_files[:5]:
        try:
            content, sha = fetch_file_from_mirror(path, token=github_token)
            file_contents.append({"path": path, "content": content, "sha": sha})
        except Exception as e:
            file_contents.append({"path": path, "content": None, "error": str(e)})

    truncated_context = []
    for fc in file_contents:
        if fc.get("content"):
            truncated_context.append(_truncate_file(fc["content"], fc["path"]))
        else:
            truncated_context.append(f"### {fc['path']}\n(Could not fetch: {fc.get('error', 'unknown')})")

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=32000,
        system=REBASE_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"## Bug {bug['bug_id']}\n"
                    f"**Summary:** {bug['summary']}\n"
                    f"**Status:** {bug['status']}\n"
                    f"**Component:** {bug['component']}\n\n"
                    f"**Description:**\n{bug.get('description') or '(empty)'}\n\n"
                    f"---\n\n## Original patch by {original_author}\n"
                    f"**Patch summary:** {patch_summary}\n"
                    f"**Attached:** {patch['creation_time'][:10]}\n\n"
                    f"```diff\n{patch_data}\n```\n\n"
                    f"---\n\n## Bug comments (may explain why the patch no longer applies)\n\n"
                    + "\n".join(
                        f"**{c.get('author', 'unknown')}** ({c['creation_time'][:10]}): {c['body'][:500]}"
                        for c in comments[-10:]
                    )
                    + f"\n\n---\n\n## Current file contents (what the patch needs to apply against)\n\n"
                    + "\n\n".join(truncated_context)
                    + "\n\n---\n\n"
                    f"Rebase this patch so it applies to the current code. "
                    f"Credit the original author ({original_author}) in the commit message."
                ),
            }
        ],
        output_format=CodeFixResponse,
    ) as stream:
        final = stream.get_final_message()

    fix = final.parsed_output
    if fix is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        fix = CodeFixResponse.model_validate_json(text)

    _store_fix(
        db_path, bug_internal_id, bug, fix, file_contents, model,
        original_author=original_author,
    )
    return fix


# ---------------------------------------------------------------------------
# Shared storage + patch generation
# ---------------------------------------------------------------------------

def _store_fix(
    db_path: Path,
    bug_internal_id: int,
    bug: dict,
    fix: CodeFixResponse,
    file_contents: list[dict],
    model: str,
    original_author: str | None = None,
) -> None:
    now = _utc_now_iso()
    with connect(db_path) as conn:
        conn.execute("DELETE FROM code_fixes WHERE bug_id = ?", (bug_internal_id,))
        for f in fix.fixes:
            conn.execute(
                """
                INSERT INTO code_fixes (bug_id, file_path, original_content, fixed_content, explanation, model, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bug_internal_id,
                    f.file_path,
                    next((fc["content"] for fc in file_contents if fc["path"] == f.file_path), None),
                    f.content,
                    f.explanation,
                    model,
                    now,
                ),
            )

        patch_data = _generate_patch(bug, fix, file_contents, original_author=original_author)
        conn.execute(
            """
            INSERT OR REPLACE INTO code_fix_meta (bug_id, commit_message, model, created_at, patch_data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bug_internal_id, fix.commit_message, model, now, patch_data),
        )


def _generate_patch(
    bug: dict,
    fix: CodeFixResponse,
    file_contents: list[dict],
    original_author: str | None = None,
) -> str:
    """Generate git format-patch style output."""
    originals = {fc["path"]: fc.get("content", "") for fc in file_contents}
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%a, %d %b %Y %H:%M:%S +0000")

    # If rebasing, credit the original author
    if original_author:
        from_line = f"From: {original_author}"
    else:
        from_line = "From: koha-triage <koha-triage@bywatersolutions.com>"

    lines = [
        "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001",
        from_line,
        f"Date: {date_str}",
        f"Subject: [PATCH] {fix.commit_message}",
        "",
        f"Bug {bug['bug_id']}: {bug['summary']}",
        "",
    ]

    if original_author:
        lines.append(f"Original-author: {original_author}")
        lines.append("Rebased-by: koha-triage (ByWater Solutions)")
        lines.append("Assisted-by: Claude (Anthropic) via koha-triage")
    else:
        lines.append("Assisted-by: Claude (Anthropic) via koha-triage")

    lines.extend(["", "---"])

    stat_lines = []
    total_insertions = 0
    total_deletions = 0

    diffs: list[str] = []
    for f in fix.fixes:
        original = originals.get(f.file_path, "")
        orig_lines = (original or "").splitlines(keepends=True)
        new_lines = f.content.splitlines(keepends=True)
        diff = list(difflib.unified_diff(
            orig_lines, new_lines,
            fromfile=f"a/{f.file_path}",
            tofile=f"b/{f.file_path}",
        ))
        if diff:
            ins = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
            dels = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
            total_insertions += ins
            total_deletions += dels
            stat_lines.append(f" {f.file_path} | {ins + dels} {'+'*ins}{'-'*dels}")
            diffs.append("".join(diff))

    lines.extend(stat_lines)
    lines.append(f" {len(fix.fixes)} file(s) changed, {total_insertions} insertions(+), {total_deletions} deletions(-)")
    lines.append("")
    for d in diffs:
        lines.append(d)
    lines.append("-- ")
    lines.append("koha-triage 0.0.1")
    lines.append("")

    return "\n".join(lines)


def get_stored_fixes(db_path: Path, bug_internal_id: int) -> tuple[list[dict], dict | None]:
    init_db(db_path)
    with connect(db_path) as conn:
        fixes = conn.execute(
            "SELECT * FROM code_fixes WHERE bug_id = ? ORDER BY id",
            (bug_internal_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT * FROM code_fix_meta WHERE bug_id = ?",
            (bug_internal_id,),
        ).fetchone()
    return [dict(f) for f in fixes], dict(meta) if meta else None
