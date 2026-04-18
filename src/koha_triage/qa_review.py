"""AI-powered QA review of Bugzilla patches."""

from pathlib import Path

import anthropic
from pydantic import BaseModel, Field

from .db import connect, init_db

GUIDELINES_DIR = Path(__file__).parent / "guidelines"


class QAResult(BaseModel):
    overall_verdict: str = Field(
        ...,
        description="One of: 'passes_qa', 'needs_followup', 'fails_qa'",
    )
    summary: str = Field(..., description="2-3 sentence summary of the QA review.")
    strengths: list[str] = Field(..., description="What the patch does well.")
    issues: list[str] = Field(
        ...,
        description="Issues found — coding guideline violations, missing tests, logic errors, etc.",
    )
    testing_notes: str = Field(..., description="What was checked and what should be tested manually.")
    suggested_followups: list[str] = Field(
        default_factory=list,
        description="Specific follow-up items if verdict is needs_followup.",
    )


def _load_guidelines() -> str:
    parts = []
    for md_file in sorted(GUIDELINES_DIR.glob("*.md")):
        parts.append(f"# {md_file.stem}\n\n{md_file.read_text()}")
    return "\n\n---\n\n".join(parts) if parts else "(no guidelines available)"


SYSTEM_PROMPT = """You are a QA reviewer for the Koha ILS (Integrated Library System).

You will receive:
1. Koha coding guidelines and development handbook
2. A bug report with description and comments
3. A patch attached to the bug

Your job: review the patch against Koha's coding standards and the bug's requirements. Be thorough but fair — focus on real issues, not style nitpicks.

Check for:
- Does the patch actually fix the described bug?
- Coding guideline compliance (Modern::Perl, Try::Tiny, Koha:: namespace, CSRF, SQL placeholders, template filtering, etc.)
- Security issues (XSS, SQL injection, CSRF bypass)
- Missing or inadequate tests
- Database changes without atomic updates
- API spec changes without bundle rebuild
- Potential regressions or edge cases

Verdict meanings:
- passes_qa: Patch is solid, follows guidelines, has adequate tests. Ready for sign-off.
- needs_followup: Patch works but has minor issues that should be addressed. List specific follow-ups.
- fails_qa: Patch has significant problems — security issues, missing tests for critical paths, guideline violations, or doesn't actually fix the bug.

Be constructive. If the patch is close, say what specifically needs to change."""


def review_patch(
    db_path: Path,
    bug_internal_id: int,
    patch_data: str,
    patch_author: str,
    api_key: str,
    model: str = "claude-opus-4-6",
) -> QAResult:
    """Run AI QA review on a patch."""
    init_db(db_path)

    with connect(db_path) as conn:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        if row is None:
            raise ValueError(f"Bug {bug_internal_id} not found")
        comments = conn.execute(
            "SELECT author, body, creation_time FROM comments WHERE bug_id = ? ORDER BY creation_time LIMIT 10",
            (bug_internal_id,),
        ).fetchall()

    bug = dict(row)
    guidelines = _load_guidelines()

    comment_text = "\n".join(
        f"**{c['author']}** ({c['creation_time'][:10]}): {c['body'][:500]}"
        for c in comments
    )

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
                    f"---\n\n# BUG REPORT\n\n"
                    f"## Bug {bug['bug_id']}: {bug['summary']}\n"
                    f"**Status:** {bug['status']}\n"
                    f"**Component:** {bug['component']}\n\n"
                    f"**Description:**\n{bug.get('description') or '(empty)'}\n\n"
                    f"**Comments:**\n{comment_text}\n\n"
                    f"---\n\n# PATCH TO REVIEW\n"
                    f"**Author:** {patch_author}\n\n"
                    f"```diff\n{patch_data}\n```\n\n"
                    "Review this patch against the coding guidelines and bug requirements."
                ),
            }
        ],
        output_format=QAResult,
    ) as stream:
        final = stream.get_final_message()

    result = final.parsed_output
    if result is None:
        text = final.content[0].text if hasattr(final.content[0], 'text') else str(final.content[0])
        result = QAResult.model_validate_json(text)

    return result


def format_qa_comment(
    result: QAResult,
    bug_id: int,
    reviewer_name: str,
    reviewer_email: str,
) -> str:
    """Format QA review as a Bugzilla comment."""
    lines = [
        f"QA Review by {reviewer_name} <{reviewer_email}>",
        f"Assisted-by: Claude (Anthropic) via koha-triage",
        "",
        f"**Overall: {result.overall_verdict.replace('_', ' ').title()}**",
        "",
        result.summary,
        "",
    ]

    if result.strengths:
        lines.append("**Strengths:**")
        for s in result.strengths:
            lines.append(f"- {s}")
        lines.append("")

    if result.issues:
        lines.append("**Issues:**")
        for issue in result.issues:
            lines.append(f"- {issue}")
        lines.append("")

    if result.suggested_followups:
        lines.append("**Follow-ups needed:**")
        for f in result.suggested_followups:
            lines.append(f"- {f}")
        lines.append("")

    lines.append(f"**Testing notes:** {result.testing_notes}")

    return "\n".join(lines)
