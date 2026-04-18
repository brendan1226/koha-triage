import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .db import connect, init_db

GUIDELINES_DIR = Path(__file__).parent / "guidelines"


class Recommendation(BaseModel):
    summary: str = Field(..., description="1-2 sentence summary of the bug.")
    affected_areas: list[str] = Field(
        ..., description="Koha areas affected (e.g. Circulation, Cataloguing, REST API, OPAC)."
    )
    likely_files: list[str] = Field(
        ..., description="File paths that likely need changes (e.g. Koha/Circulation.pm, koha-tmpl/...)."
    )
    complexity: Literal["easy", "medium", "hard"] = Field(
        ..., description="Estimated complexity of the fix."
    )
    fix_approach: str = Field(
        ..., description="A paragraph explaining what to change, why, and key constraints."
    )
    key_guidelines: list[str] = Field(
        ..., description="Relevant Koha coding guideline rules that apply (short phrases)."
    )
    test_plan: str = Field(
        ..., description="How to verify the fix — which scenarios to test."
    )
    suggested_branch_name: str = Field(
        ..., description="Branch name in the form bug_XXXXX (Koha convention)."
    )
    needs_db_update: bool = Field(
        ..., description="True if the fix requires a database schema change (atomic update)."
    )


def _load_guidelines() -> str:
    parts = []
    for md_file in sorted(GUIDELINES_DIR.glob("*.md")):
        parts.append(f"# {md_file.stem}\n\n{md_file.read_text()}")
    return "\n\n---\n\n".join(parts) if parts else "(no guidelines available)"


SYSTEM_PROMPT = """You are a senior Koha ILS developer familiar with the full codebase.

You will be given:
1. Koha coding guidelines and development handbook
2. A Bugzilla bug report with its description and comments

Your job: analyze the bug and produce a structured fix recommendation that a developer (or AI coding agent) can act on. Be specific about file paths, function names, and the approach. Reference coding guidelines when they constrain the fix.

Key Koha conventions:
- Commits must reference bug numbers: "Bug XXXXX: description"
- Use Modern::Perl, Koha:: namespace (not C4::), Try::Tiny, Koha::Logger
- CSRF tokens via CSRF-TOKEN header for all POST/PUT/DELETE
- Template variables must be filtered: [% var | html %]
- Patches are submitted via git bz attach, not pull requests
- Database changes need atomic updates in installer/data/mysql/atomicupdate/
- Tests go in t/db_dependent/ and use TestBuilder with PLURAL class names

Be pragmatic — recommend the simplest fix that solves the problem while honoring the guidelines."""


def _build_bug_context(bug: dict, comments: list[dict]) -> str:
    lines = [
        f"## Bug {bug['bug_id']}",
        f"**Summary:** {bug['summary']}",
        f"**Status:** {bug['status']} {bug.get('resolution') or ''}".strip(),
        f"**Component:** {bug['component']}",
        f"**Severity:** {bug.get('severity') or 'unset'}",
        f"**Creator:** {bug.get('creator') or 'unknown'}",
        f"**Created:** {bug['creation_time']}",
        "",
        "**Description:**",
        bug.get("description") or "(empty)",
    ]

    if comments:
        lines.append("")
        lines.append(f"**Comments ({len(comments)}):**")
        for c in comments[:10]:
            lines.append(f"\n--- {c.get('author', 'unknown')} ({c['creation_time'][:10]}):")
            lines.append(c.get("body") or "(empty)")

    return "\n".join(lines)


def generate_recommendation(
    db_path: Path,
    bug_internal_id: int,
    api_key: str,
    model: str = "claude-opus-4-6",
) -> Recommendation:
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Bug {bug_internal_id} not found")

        comments = conn.execute(
            "SELECT * FROM comments WHERE bug_id = ? ORDER BY creation_time LIMIT 10",
            (bug_internal_id,),
        ).fetchall()

    bug = dict(row)
    guidelines = _load_guidelines()
    bug_context = _build_bug_context(bug, [dict(c) for c in comments])

    client = anthropic.Anthropic(api_key=api_key)

    with client.messages.stream(
        model=model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"# KOHA CODING GUIDELINES\n\n{guidelines}\n\n"
                    f"---\n\n# BUG TO ANALYZE\n\n{bug_context}\n\n"
                    "Produce a structured fix recommendation."
                ),
            }
        ],
        output_format=Recommendation,
    ) as stream:
        final = stream.get_final_message()

    rec = final.parsed_output
    if rec is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        rec = Recommendation.model_validate_json(text)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO recommendations (bug_id, model, recommendation, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(bug_id) DO UPDATE SET
                model = excluded.model,
                recommendation = excluded.recommendation,
                created_at = excluded.created_at
            """,
            (bug_internal_id, model, rec.model_dump_json(), now),
        )

    return rec


def get_stored_recommendation(db_path: Path, bug_internal_id: int) -> tuple[Recommendation, str, str] | None:
    init_db(db_path)
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT model, recommendation, created_at FROM recommendations WHERE bug_id = ?",
            (bug_internal_id,),
        ).fetchone()
    if row is None:
        return None
    rec = Recommendation.model_validate_json(row["recommendation"])
    return rec, row["model"], row["created_at"]
