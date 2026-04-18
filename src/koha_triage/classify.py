from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .search import SearchResult, search as semantic_search


VERDICT_DESCRIPTIONS = """
- `has_patch`: A candidate has an attached patch or is in "Patch doesn't apply" / "Signed off" status. The reporter should review/test that patch.
- `resolved_fixed`: The bug was resolved FIXED. The reporter should verify the fix in the latest release.
- `reported_only`: Same or very similar problem but no fix in progress. The reporter should comment / add details rather than file new.
- `likely_duplicate`: Essentially the same problem. The reporter should mark as duplicate or comment there.
- `tangentially_related`: Same general area but different root cause. Worth referencing when filing new.
- `unrelated`: Semantic-search false positive; no meaningful overlap.
""".strip()


SYSTEM_PROMPT = (
    "You are a triage assistant for the Koha ILS Bugzilla.\n\n"
    "Given a user's problem description and a list of candidate related bugs that were "
    "surfaced by semantic search, classify each candidate's relevance.\n\n"
    "Verdict vocabulary (use these exact values):\n"
    + VERDICT_DESCRIPTIONS
    + "\n\n"
    "For each candidate, produce:\n"
    "  - match_id: the 1-indexed position of the candidate in the input list\n"
    "  - verdict: one of the values above\n"
    "  - rationale: ONE sentence explaining the classification\n"
    "  - suggested_action: ONE sentence telling the user what to do\n\n"
    "Be conservative: `likely_duplicate` and `has_patch` are strong claims.\n\n"
    "Return a JSON object with an ordered `verdicts` array."
)


class Verdict(BaseModel):
    match_id: int = Field(...)
    verdict: Literal[
        "has_patch",
        "resolved_fixed",
        "reported_only",
        "likely_duplicate",
        "tangentially_related",
        "unrelated",
    ] = Field(...)
    rationale: str = Field(...)
    suggested_action: str = Field(...)


class ClassifyResponse(BaseModel):
    verdicts: list[Verdict]


def _build_candidate_text(results: list[SearchResult]) -> str:
    lines = []
    for i, r in enumerate(results, start=1):
        desc = r["description_snippet"] if r["description_snippet"] else "(no description)"
        resolution = f", resolution={r['resolution']}" if r["resolution"] else ""
        lines.append(
            f"{i}. [Bug {r['bug_id']}] "
            f"(status={r['status']}{resolution}, component={r['component']}) "
            f'"{r["summary"]}"\n'
            f"   Description: {desc}"
        )
    return "\n\n".join(lines)


def classify(
    db_path: Path,
    query: str,
    embedding_model: str,
    api_key: str,
    classification_model: str = "claude-opus-4-6",
    top_k: int = 5,
    component: str | None = None,
) -> tuple[list[SearchResult], list[Verdict]]:
    results = semantic_search(
        db_path, query, embedding_model, top_k=top_k, component=component
    )
    if not results:
        return [], []

    client = anthropic.Anthropic(api_key=api_key)
    candidate_text = _build_candidate_text(results)

    with client.messages.stream(
        model=classification_model,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"QUERY: {query}\n\n"
                    f"CANDIDATES:\n\n{candidate_text}\n\n"
                    "Classify each candidate."
                ),
            }
        ],
        output_format=ClassifyResponse,
    ) as stream:
        response = stream.get_final_message()

    try:
        parsed = ClassifyResponse.model_validate_json(response.content[0].text)  # type: ignore
    except Exception:
        return results, []

    verdicts_by_idx = {v.match_id: v for v in parsed.verdicts}
    aligned = [verdicts_by_idx.get(i + 1) for i in range(len(results))]
    return results, [v for v in aligned if v is not None]
