"""Generate code fixes via Claude and produce git format-patch output."""

import base64
import difflib
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from pydantic import BaseModel, Field

from .config import KOHA_GITHUB_MIRROR, settings
from .db import connect, init_db
from .recommend import Recommendation, get_stored_recommendation

GITHUB_API = "https://api.github.com"


class FileFix(BaseModel):
    file_path: str = Field(..., description="Path relative to repo root.")
    explanation: str = Field(..., description="What changed and why, 2-3 sentences.")
    content: str = Field(..., description="The complete modified file content.")


class CodeFixResponse(BaseModel):
    fixes: list[FileFix]
    commit_message: str = Field(..., description="A concise commit message in Koha format: Bug XXXXX: description")


SYSTEM_PROMPT = """You are implementing a code fix for the Koha ILS (Integrated Library System).

You will receive:
1. A bug description from Bugzilla
2. A fix recommendation (approach, affected files, guidelines)
3. The current content of the file(s) to modify

Your job: produce the COMPLETE modified file content for each file that needs changes. Do not produce diffs — return the entire file with your changes applied. Be surgical: change only what's needed to fix the bug.

Key Koha conventions you MUST follow:
- `use Modern::Perl;` at the top of every .pm/.pl file
- Koha:: namespace for new code (not C4::)
- Try::Tiny for error handling (never eval)
- Koha::Logger for logging (never warn)
- SQL placeholders (?) for all user input
- Filter all template variables: [% var | html %]
- CSRF-TOKEN header for POST/PUT/DELETE in JS
- GPL v3 licence header on .pm, .pl, .t files
- 4 spaces indentation (not tabs)
- snake_case for subroutine names

If a file path from the recommendation doesn't match what was fetched, adapt to the actual file structure you see."""


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

    files_context = []
    for fc in file_contents:
        if fc.get("content"):
            files_context.append(f"### {fc['path']}\n```\n{fc['content']}\n```")
        else:
            files_context.append(f"### {fc['path']}\n(Could not fetch: {fc.get('error', 'unknown')})")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.parse(
        model=model,
        max_tokens=16000,
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
                    + "\n\n".join(files_context)
                    + "\n\n---\n\nProduce the complete modified file content for each file."
                ),
            }
        ],
        output_format=CodeFixResponse,
    )

    fix = response.parsed_output
    if fix is None:
        raise RuntimeError("Claude did not return a valid code fix")

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

        patch_data = _generate_patch(bug, fix, file_contents)
        conn.execute(
            """
            INSERT OR REPLACE INTO code_fix_meta (bug_id, commit_message, model, created_at, patch_data)
            VALUES (?, ?, ?, ?, ?)
            """,
            (bug_internal_id, fix.commit_message, model, now, patch_data),
        )

    return fix


def _generate_patch(bug: dict, fix: CodeFixResponse, file_contents: list[dict]) -> str:
    """Generate git format-patch style output."""
    originals = {fc["path"]: fc.get("content", "") for fc in file_contents}
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%a, %d %b %Y %H:%M:%S +0000")

    lines = [
        f"From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001",
        f"From: koha-triage <koha-triage@bywatersolutions.com>",
        f"Date: {date_str}",
        f"Subject: [PATCH] {fix.commit_message}",
        "",
        f"Bug {bug['bug_id']}: {bug['summary']}",
        "",
        f"Assisted-by: Claude (Anthropic) via koha-triage",
        "",
        "---",
    ]

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
