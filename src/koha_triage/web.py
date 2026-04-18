import difflib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import BUGZILLA_URL, settings
from .db import connect, init_db

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="koha-triage", version="0.0.1")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

oauth = OAuth()
if settings.google_client_id:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compute_diff(original: str, modified: str, file_path: str) -> list[dict]:
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(orig_lines, mod_lines, fromfile=f"a/{file_path}", tofile=f"b/{file_path}")
    lines: list[dict] = []
    for raw in diff:
        text = raw.rstrip("\n")
        if text.startswith("+++") or text.startswith("---"):
            lines.append({"type": "header", "text": text})
        elif text.startswith("@@"):
            lines.append({"type": "hunk", "text": text})
        elif text.startswith("+"):
            lines.append({"type": "add", "text": text})
        elif text.startswith("-"):
            lines.append({"type": "del", "text": text})
        else:
            lines.append({"type": "ctx", "text": text})
    return lines


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    request.state.user = None

    if not settings.google_client_id:
        request.state.user = {"id": 0, "email": "local", "name": "Local Dev", "picture_url": ""}
        return await call_next(request)

    public_prefixes = ("/login", "/auth/", "/healthz", "/static")
    if any(request.url.path.startswith(p) for p in public_prefixes):
        return await call_next(request)

    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")

    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        request.session.clear()
        return RedirectResponse("/login")

    request.state.user = dict(row)
    return await call_next(request)

app.add_middleware(SessionMiddleware, secret_key=settings.session_secret)


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not settings.google_client_id:
        return RedirectResponse("/")
    return templates.TemplateResponse(
        request=request, name="login.html",
        context={"error": error, "allowed_domains": settings.allowed_domains},
    )


@app.get("/auth/start")
async def auth_start(request: Request):
    if not settings.google_client_id:
        return RedirectResponse("/")
    redirect_uri = str(request.url_for("auth_callback"))
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not settings.google_client_id:
        return RedirectResponse("/")
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        return RedirectResponse(f"/login?error={quote(str(e))}")

    user_info = token.get("userinfo")
    if not user_info:
        return RedirectResponse("/login?error=No+user+info+returned")

    email = user_info.get("email", "")
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    allowed = [d.strip() for d in settings.allowed_domains.split(",")]
    if domain not in allowed:
        return RedirectResponse(f"/login?error=Domain+{quote(domain)}+not+allowed")

    now = _utc_now_iso()
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        conn.execute(
            """
            INSERT INTO users (email, name, picture_url, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(email) DO UPDATE SET
                name = excluded.name, picture_url = excluded.picture_url,
                last_login_at = excluded.last_login_at
            """,
            (email, user_info.get("name", ""), user_info.get("picture", ""), now, now),
        )
        row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

    request.session["user_id"] = row["id"]
    return RedirectResponse("/")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login" if settings.google_client_id else "/")


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: bool = False):
    user = request.state.user
    current = {}
    token_display = None

    if user and user.get("id"):
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT github_token FROM user_settings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if row:
            current = dict(row)
            t = current.get("github_token") or ""
            token_display = f"...{t[-4:]}" if len(t) > 4 else ("set" if t else None)
            current["github_token"] = ""

    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={"user": user, "current_settings": current, "token_display": token_display, "saved": saved},
    )


@app.post("/settings")
def save_settings(request: Request, github_token: str = Form("")):
    user = request.state.user
    if not user or not user.get("id"):
        return RedirectResponse("/login")

    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        existing = conn.execute(
            "SELECT github_token FROM user_settings WHERE user_id = ?", (user["id"],)
        ).fetchone()
        if not github_token.strip() and existing:
            github_token = existing["github_token"] or ""

        conn.execute(
            """
            INSERT INTO user_settings (user_id, github_token, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                github_token = excluded.github_token, updated_at = excluded.updated_at
            """,
            (user["id"], github_token.strip(), now),
        )

    return RedirectResponse("/settings?saved=1", status_code=303)


def _get_user_github_token(request: Request) -> str | None:
    user = request.state.user
    if user and user.get("id"):
        with connect(settings.db_path) as conn:
            row = conn.execute(
                "SELECT github_token FROM user_settings WHERE user_id = ?",
                (user["id"],),
            ).fetchone()
        if row and row["github_token"]:
            return row["github_token"]
    return settings.github_token


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# ---------------------------------------------------------------------------
# About
# ---------------------------------------------------------------------------

@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="about.html")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        total_bugs = conn.execute("SELECT COUNT(*) FROM bugs").fetchone()[0]
        open_bugs = conn.execute("SELECT COUNT(*) FROM bugs WHERE status NOT IN ('RESOLVED','VERIFIED','CLOSED')").fetchone()[0]
        embedded_count = conn.execute("SELECT COUNT(*) FROM bugs WHERE embedding IS NOT NULL").fetchone()[0]
        total_comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        harvest = conn.execute("SELECT * FROM harvest_state WHERE id = 1").fetchone()

        components = conn.execute(
            """
            SELECT component, COUNT(*) as cnt,
                   SUM(CASE WHEN status NOT IN ('RESOLVED','VERIFIED','CLOSED') THEN 1 ELSE 0 END) as open_cnt
            FROM bugs GROUP BY component ORDER BY cnt DESC LIMIT 20
            """
        ).fetchall()

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "total_bugs": total_bugs,
            "open_bugs": open_bugs,
            "embedded_count": embedded_count,
            "total_comments": total_comments,
            "harvest": dict(harvest) if harvest else None,
            "components": [dict(c) for c in components],
            "has_anthropic_key": bool(settings.anthropic_api_key),
        },
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = "", k: int = 10) -> HTMLResponse:
    q = (q or "").strip()
    if not q:
        return templates.TemplateResponse(
            request=request, name="search.html",
            context={"query": "", "has_anthropic_key": bool(settings.anthropic_api_key)},
        )

    # If query is a bug number, jump straight to that bug's detail page
    stripped = q.lstrip("#").strip()
    if stripped.isdigit():
        bug_num = int(stripped)
        init_db(settings.db_path)
        with connect(settings.db_path) as conn:
            row = conn.execute("SELECT id FROM bugs WHERE bug_id = ?", (bug_num,)).fetchone()
        if row:
            return RedirectResponse(f"/bugs/{row['id']}", status_code=302)

    k = max(1, min(k, 20))
    from .search import NoEmbeddingsError, search as semantic_search
    error = None
    results = []
    verdicts = []
    classified = False
    try:
        if settings.anthropic_api_key:
            from .classify import classify as run_classify
            results, verdicts = run_classify(settings.db_path, q, settings.embedding_model, settings.anthropic_api_key, settings.classification_model, top_k=k)
            classified = True
        else:
            results = semantic_search(settings.db_path, q, settings.embedding_model, top_k=k)
    except NoEmbeddingsError as e:
        error = str(e)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    verdicts_by_idx = {i: v for i, v in enumerate(verdicts) if i < len(results)}
    rows = [{**r, "verdict": (v := verdicts_by_idx.get(i)) and v.verdict, "rationale": v and v.rationale, "suggested_action": v and v.suggested_action} for i, r in enumerate(results)]
    return templates.TemplateResponse(request=request, name="search.html", context={"query": q, "k": k, "rows": rows, "error": error, "classified": classified, "has_anthropic_key": bool(settings.anthropic_api_key), "model": settings.classification_model})


# ---------------------------------------------------------------------------
# Bug browser
# ---------------------------------------------------------------------------

SORT_COLUMNS = {
    "bug": "b.bug_id",
    "summary": "b.summary",
    "status": "b.status",
    "component": "b.component",
    "severity": "b.severity",
    "creator": "b.creator",
    "updated": "b.last_change_time",
}


@app.get("/bugs", response_class=HTMLResponse)
def bugs_list(request: Request, component: str = "", status: str = "open", severity: str = "", q: str = "", page: int = 1, sort: str = "updated", dir: str = "desc") -> HTMLResponse:
    init_db(settings.db_path)
    per_page = 50
    offset = (max(1, page) - 1) * per_page
    filters, params = [], []
    if component:
        filters.append("b.component = ?"); params.append(component)
    if status == "open":
        filters.append("b.status NOT IN ('RESOLVED','VERIFIED','CLOSED')")
    elif status == "closed":
        filters.append("b.status IN ('RESOLVED','VERIFIED','CLOSED')")
    if severity:
        filters.append("b.severity = ?"); params.append(severity)
    if q:
        filters.append("b.summary LIKE ?"); params.append(f"%{q}%")
    where = "WHERE " + " AND ".join(filters) if filters else ""

    sort_col = SORT_COLUMNS.get(sort, "b.last_change_time")
    sort_dir = "ASC" if dir == "asc" else "DESC"

    with connect(settings.db_path) as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM bugs b {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""SELECT b.id, b.bug_id, b.summary, b.status, b.resolution, b.component,
                       b.severity, b.priority, b.creator, b.creation_time, b.last_change_time, b.url
                FROM bugs b {where}
                ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?""",
            [*params, per_page, offset]
        ).fetchall()
        component_options = conn.execute("SELECT DISTINCT component FROM bugs ORDER BY component").fetchall()
        groups = conn.execute("SELECT id, name FROM groups ORDER BY name").fetchall()
    return templates.TemplateResponse(request=request, name="bugs.html", context={
        "bugs": [dict(r) for r in rows],
        "component_options": [r["component"] for r in component_options],
        "groups": [dict(g) for g in groups],
        "total": total, "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "per_page": per_page,
        "filter_component": component, "filter_status": status,
        "filter_severity": severity, "filter_q": q,
        "sort": sort, "sort_dir": dir,
    })


@app.get("/bugs/{bug_internal_id}", response_class=HTMLResponse)
def bug_detail(request: Request, bug_internal_id: int, error: str = "") -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        if row is None:
            return HTMLResponse("Bug not found", status_code=404)
        bug_comments = conn.execute("SELECT * FROM comments WHERE bug_id = ? ORDER BY creation_time", (bug_internal_id,)).fetchall()
        memberships = conn.execute("SELECT g.id, g.name FROM groups g JOIN group_members gm ON gm.group_id = g.id WHERE gm.bug_id = ?", (bug_internal_id,)).fetchall()
        all_groups = conn.execute("SELECT id, name FROM groups ORDER BY name").fetchall()

    keywords = [k.strip() for k in (row["keywords"] or "").split(",") if k.strip()]

    from .recommend import get_stored_recommendation
    stored = get_stored_recommendation(settings.db_path, bug_internal_id)
    rec, rec_meta = None, None
    if stored:
        rec_obj, rec_model, rec_created = stored
        rec = rec_obj.model_dump()
        rec_meta = {"model": rec_model, "created_at": rec_created}

    from .codegen import get_stored_fixes
    code_fixes, fix_meta = get_stored_fixes(settings.db_path, bug_internal_id)
    for fix in code_fixes:
        fix["diff_lines"] = _compute_diff(fix.get("original_content") or "", fix.get("fixed_content") or "", fix.get("file_path", "unknown"))

    # Check if this is a "Patch doesn't apply" bug — show rebase button
    is_patch_doesnt_apply = "Patch doesn't apply" in (row["status"] or "")

    return templates.TemplateResponse(request=request, name="bug_detail.html", context={
        "bug": dict(row), "keywords": keywords,
        "comments": [dict(c) for c in bug_comments],
        "memberships": [dict(m) for m in memberships],
        "all_groups": [dict(g) for g in all_groups],
        "rec": rec, "rec_meta": rec_meta,
        "code_fixes": code_fixes, "fix_meta": fix_meta,
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "has_github_token": bool(_get_user_github_token(request)),
        "bugzilla_url": BUGZILLA_URL,
        "error": error,
        "is_patch_doesnt_apply": is_patch_doesnt_apply,
    })


# ---------------------------------------------------------------------------
# Bug actions
# ---------------------------------------------------------------------------

@app.post("/bugs/{bug_internal_id}/recommend")
def generate_bug_recommendation(bug_internal_id: int) -> RedirectResponse:
    if not settings.anthropic_api_key:
        return RedirectResponse(f"/bugs/{bug_internal_id}?error=No+Anthropic+API+key", status_code=303)
    try:
        from .recommend import generate_recommendation
        generate_recommendation(settings.db_path, bug_internal_id, settings.anthropic_api_key, settings.classification_model)
    except Exception as e:
        return RedirectResponse(f"/bugs/{bug_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/bugs/{bug_internal_id}", status_code=303)


@app.post("/bugs/{bug_internal_id}/generate-fix")
def generate_fix(request: Request, bug_internal_id: int) -> RedirectResponse:
    try:
        github_token = _get_user_github_token(request)
        if not settings.anthropic_api_key:
            raise ValueError("No Anthropic API key configured.")
        from .codegen import generate_code_fix
        generate_code_fix(settings.db_path, bug_internal_id, settings.anthropic_api_key, github_token, settings.classification_model)
    except Exception as e:
        return RedirectResponse(f"/bugs/{bug_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/bugs/{bug_internal_id}", status_code=303)


@app.post("/bugs/{bug_internal_id}/rebase-patch")
def rebase_stale_patch(request: Request, bug_internal_id: int) -> RedirectResponse:
    try:
        github_token = _get_user_github_token(request)
        if not settings.anthropic_api_key:
            raise ValueError("No Anthropic API key configured.")
        from .codegen import rebase_patch
        rebase_patch(settings.db_path, bug_internal_id, settings.anthropic_api_key, github_token, settings.classification_model)
    except Exception as e:
        return RedirectResponse(f"/bugs/{bug_internal_id}?error={quote(str(e))}", status_code=303)
    return RedirectResponse(f"/bugs/{bug_internal_id}", status_code=303)


@app.get("/bugs/{bug_internal_id}/patch")
def download_patch(bug_internal_id: int) -> PlainTextResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        meta = conn.execute("SELECT * FROM code_fix_meta WHERE bug_id = ?", (bug_internal_id,)).fetchone()
        bug = conn.execute("SELECT bug_id FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
    if meta is None or not meta["patch_data"]:
        return PlainTextResponse("No patch available", status_code=404)
    bz_id = bug["bug_id"] if bug else "unknown"
    return PlainTextResponse(
        meta["patch_data"],
        headers={"Content-Disposition": f'attachment; filename="bug_{bz_id}.patch"'},
    )


# ---------------------------------------------------------------------------
# Bugzilla patches — viewer, QA review, comment posting
# ---------------------------------------------------------------------------

@app.get("/bugs/{bug_internal_id}/patches", response_class=HTMLResponse)
def view_patches(request: Request, bug_internal_id: int, error: str = "", qa_done: bool = False) -> HTMLResponse:
    """View non-obsolete patches from Bugzilla with optional AI QA review."""
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        bug = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
    if bug is None:
        return HTMLResponse("Bug not found", status_code=404)

    from .codegen import fetch_patches_from_bugzilla
    try:
        all_patches = fetch_patches_from_bugzilla(bug["bug_id"])
    except Exception as e:
        all_patches = []
        error = f"Failed to fetch patches: {e}"

    patches = [p for p in all_patches if not p["is_obsolete"]]

    # Parse each patch into diff lines for display
    for p in patches:
        diff_lines = []
        for raw_line in p["data"].splitlines():
            if raw_line.startswith("+++") or raw_line.startswith("---"):
                diff_lines.append({"type": "header", "text": raw_line})
            elif raw_line.startswith("@@"):
                diff_lines.append({"type": "hunk", "text": raw_line})
            elif raw_line.startswith("+"):
                diff_lines.append({"type": "add", "text": raw_line})
            elif raw_line.startswith("-"):
                diff_lines.append({"type": "del", "text": raw_line})
            else:
                diff_lines.append({"type": "ctx", "text": raw_line})
        p["diff_lines"] = diff_lines

    # Load stored QA review if any
    qa_result = None
    with connect(settings.db_path) as conn:
        qa_row = conn.execute(
            "SELECT * FROM qa_reviews WHERE bug_id = ? ORDER BY created_at DESC LIMIT 1",
            (bug_internal_id,),
        ).fetchone()
    if qa_row:
        import json
        qa_result = json.loads(qa_row["review_json"])
        qa_result["_meta"] = {"model": qa_row["model"], "created_at": qa_row["created_at"], "patch_author": qa_row["patch_author"]}

    return templates.TemplateResponse(request=request, name="patches.html", context={
        "bug": dict(bug),
        "patches": patches,
        "qa_result": qa_result,
        "has_anthropic_key": bool(settings.anthropic_api_key),
        "has_bugzilla_key": bool(settings.bugzilla_api_key),
        "error": error,
        "qa_done": qa_done,
    })


@app.post("/bugs/{bug_internal_id}/qa-review")
def run_qa_review(request: Request, bug_internal_id: int, patch_index: int = Form(0)) -> RedirectResponse:
    """Run AI QA review on a Bugzilla patch."""
    if not settings.anthropic_api_key:
        return RedirectResponse(f"/bugs/{bug_internal_id}/patches?error=No+Anthropic+API+key", status_code=303)

    try:
        from .codegen import fetch_patches_from_bugzilla
        from .qa_review import review_patch

        all_patches = fetch_patches_from_bugzilla(
            _get_bug_id(bug_internal_id)
        )
        non_obsolete = [p for p in all_patches if not p["is_obsolete"]]
        if not non_obsolete:
            raise ValueError("No non-obsolete patches found")

        idx = min(patch_index, len(non_obsolete) - 1)
        patch = non_obsolete[idx]

        result = review_patch(
            settings.db_path,
            bug_internal_id,
            patch["data"],
            patch["creator"],
            settings.anthropic_api_key,
            settings.classification_model,
        )

        # Store the review
        import json
        now = _utc_now_iso()
        with connect(settings.db_path) as conn:
            conn.execute(
                """
                INSERT INTO qa_reviews (bug_id, patch_author, model, review_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (bug_internal_id, patch["creator"], settings.classification_model, result.model_dump_json(), now),
            )

    except Exception as e:
        return RedirectResponse(f"/bugs/{bug_internal_id}/patches?error={quote(str(e))}", status_code=303)

    return RedirectResponse(f"/bugs/{bug_internal_id}/patches?qa_done=1", status_code=303)


@app.post("/bugs/{bug_internal_id}/post-qa-comment")
def post_qa_comment(request: Request, bug_internal_id: int) -> RedirectResponse:
    """Post the QA review as a comment on Bugzilla."""
    if not settings.bugzilla_api_key:
        return RedirectResponse(f"/bugs/{bug_internal_id}/patches?error=No+Bugzilla+API+key+configured", status_code=303)

    try:
        import json
        from .qa_review import format_qa_comment, QAResult

        with connect(settings.db_path) as conn:
            qa_row = conn.execute(
                "SELECT * FROM qa_reviews WHERE bug_id = ? ORDER BY created_at DESC LIMIT 1",
                (bug_internal_id,),
            ).fetchone()
            bug = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()

        if qa_row is None:
            raise ValueError("No QA review found. Run one first.")

        result = QAResult.model_validate_json(qa_row["review_json"])
        user = request.state.user
        reviewer_name = user.get("name", "koha-triage") if user else "koha-triage"
        reviewer_email = user.get("email", "koha-triage@bywatersolutions.com") if user else "koha-triage@bywatersolutions.com"

        comment_text = format_qa_comment(result, bug["bug_id"], reviewer_name, reviewer_email)

        # Post to Bugzilla REST API
        import httpx
        resp = httpx.post(
            f"{BUGZILLA_URL}/rest/bug/{bug['bug_id']}/comment",
            json={"comment": comment_text, "Bugzilla_api_key": settings.bugzilla_api_key},
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()

    except Exception as e:
        return RedirectResponse(f"/bugs/{bug_internal_id}/patches?error={quote(str(e))}", status_code=303)

    return RedirectResponse(f"/bugs/{bug_internal_id}/patches?qa_done=1", status_code=303)


def _get_bug_id(bug_internal_id: int) -> int:
    """Helper to get the Bugzilla bug_id from internal id."""
    with connect(settings.db_path) as conn:
        row = conn.execute("SELECT bug_id FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
    if row is None:
        raise ValueError(f"Bug {bug_internal_id} not found")
    return row["bug_id"]


# ---------------------------------------------------------------------------
# KTD integration — generated scripts
# ---------------------------------------------------------------------------

@app.get("/bugs/{bug_internal_id}/ktd-apply", response_class=HTMLResponse)
def ktd_apply_page(request: Request, bug_internal_id: int) -> HTMLResponse:
    """Generate KTD commands to apply an AI-generated fix locally."""
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        bug = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
        meta = conn.execute("SELECT * FROM code_fix_meta WHERE bug_id = ?", (bug_internal_id,)).fetchone()
    if bug is None:
        return HTMLResponse("Bug not found", status_code=404)
    if meta is None or not meta["patch_data"]:
        return HTMLResponse("No patch available. Generate a code fix first.", status_code=404)

    bz_id = bug["bug_id"]
    patch_url = f"{request.base_url}bugs/{bug_internal_id}/patch"
    commit_msg = meta["commit_message"] or f"Bug {bz_id}"

    script = f"""#!/bin/bash
# KTD apply script for Bug {bz_id}
# Generated by koha-triage
set -e

cd "$SYNC_REPO"

# Create a new branch from the current default branch
git checkout -b bug_{bz_id} origin/main 2>/dev/null || git checkout -b bug_{bz_id} origin/master

# Download and apply the patch
curl -sL "{patch_url}" | git am --3way

echo ""
echo "Patch applied on branch bug_{bz_id}"
echo "Next steps:"
echo "  1. ktd --shell    (enter the KTD container)"
echo "  2. Test the fix manually"
echo "  3. Run: koha-qa.pl -c 1 -v 2"
echo "  4. If all good: git bz attach {bz_id} HEAD"
"""

    return templates.TemplateResponse(request=request, name="ktd_script.html", context={
        "bug": dict(bug),
        "script": script,
        "script_type": "apply",
        "title": f"Apply AI fix to KTD — Bug {bz_id}",
        "patch_url": patch_url,
    })


@app.get("/bugs/{bug_internal_id}/ktd-signoff", response_class=HTMLResponse)
def ktd_signoff_page(request: Request, bug_internal_id: int) -> HTMLResponse:
    """Generate KTD commands to test and sign off a patch from Bugzilla."""
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        bug = conn.execute("SELECT * FROM bugs WHERE id = ?", (bug_internal_id,)).fetchone()
    if bug is None:
        return HTMLResponse("Bug not found", status_code=404)

    bz_id = bug["bug_id"]

    # Fetch patches from Bugzilla to show which one we'll apply
    from .codegen import fetch_patches_from_bugzilla
    try:
        patches = fetch_patches_from_bugzilla(bz_id)
    except Exception:
        patches = []

    non_obsolete = [p for p in patches if not p["is_obsolete"]]
    latest_patch = non_obsolete[0] if non_obsolete else (patches[0] if patches else None)

    patch_info = None
    if latest_patch:
        patch_info = {
            "summary": latest_patch["summary"],
            "creator": latest_patch["creator"],
            "date": latest_patch["creation_time"][:10],
            "id": latest_patch["id"],
        }

    script = f"""#!/bin/bash
# KTD sign-off script for Bug {bz_id}
# Generated by koha-triage
set -e

cd "$SYNC_REPO"

# Create a new branch from the current default branch
git checkout -b bug_{bz_id} origin/main 2>/dev/null || git checkout -b bug_{bz_id} origin/master

# Apply the patch from Bugzilla using git-bz
git bz apply {bz_id}

# If git bz apply fails, the patch doesn't apply cleanly.
# In that case, run:
#   git checkout main && git branch -D bug_{bz_id}
# Then use koha-triage to rebase the patch.

echo ""
echo "Patch applied on branch bug_{bz_id}"
echo ""
echo "Sign-off checklist:"
echo "  1. ktd --shell    (enter the KTD container)"
echo "  2. Run the relevant tests:"
echo "     prove -v t/db_dependent/...  (find test files in the patch)"
echo "  3. Test the fix manually in the browser"
echo "  4. Run QA tools:"
echo "     /kohadevbox/qa-test-tools/koha-qa.pl -c 1 -v 2"
echo "  5. If all good, sign off:"
echo "     git commit --amend -s"
echo "     git bz attach {bz_id} HEAD"
echo "  6. Update bug status to 'Signed Off' on Bugzilla"
"""

    script_no_gitbz = f"""#!/bin/bash
# KTD sign-off script for Bug {bz_id} (without git-bz)
# Generated by koha-triage
set -e

cd "$SYNC_REPO"

# Create a new branch from the current default branch
git checkout -b bug_{bz_id} origin/main 2>/dev/null || git checkout -b bug_{bz_id} origin/master

# Download the patch from Bugzilla and apply it
# (If you have git-bz, use: git bz apply {bz_id})
curl -sL "https://bugs.koha-community.org/bugzilla3/attachment.cgi?id={latest_patch['id']}&action=diff" \\
  | git am --3way

echo ""
echo "Patch applied on branch bug_{bz_id}"
echo "Follow the sign-off checklist above."
""" if latest_patch else ""

    return templates.TemplateResponse(request=request, name="ktd_script.html", context={
        "bug": dict(bug),
        "script": script,
        "script_alt": script_no_gitbz,
        "script_type": "signoff",
        "title": f"Sign-off workflow — Bug {bz_id}",
        "patch_info": patch_info,
    })


# ---------------------------------------------------------------------------
# Bug group membership
# ---------------------------------------------------------------------------

@app.post("/bugs/{bug_internal_id}/add-to-group")
def add_bug_to_group(bug_internal_id: int, group_id: int = Form(...)) -> RedirectResponse:
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        try:
            conn.execute("INSERT INTO group_members (group_id, bug_id, added_at) VALUES (?, ?, ?)", (group_id, bug_internal_id, now))
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (now, group_id))
        except Exception:
            pass
    return RedirectResponse(f"/bugs/{bug_internal_id}", status_code=303)


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

@app.get("/groups", response_class=HTMLResponse)
def groups_list(request: Request) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        rows = conn.execute("SELECT g.*, COUNT(gm.id) AS member_count FROM groups g LEFT JOIN group_members gm ON gm.group_id = g.id GROUP BY g.id ORDER BY g.updated_at DESC").fetchall()
    return templates.TemplateResponse(request=request, name="groups.html", context={"groups": [dict(r) for r in rows]})


@app.post("/groups")
def create_group(name: str = Form(...), description: str = Form("")) -> RedirectResponse:
    init_db(settings.db_path)
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        cursor = conn.execute("INSERT INTO groups (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)", (name.strip(), description.strip(), now, now))
    return RedirectResponse(f"/groups/{cursor.lastrowid}", status_code=303)


@app.get("/groups/{group_id}", response_class=HTMLResponse)
def group_detail(request: Request, group_id: int) -> HTMLResponse:
    init_db(settings.db_path)
    with connect(settings.db_path) as conn:
        group = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        if group is None:
            return HTMLResponse("Group not found", status_code=404)
        members = conn.execute(
            """SELECT b.id, b.bug_id, b.summary, b.status, b.resolution, b.component, b.url, gm.added_at
               FROM group_members gm JOIN bugs b ON gm.bug_id = b.id
               WHERE gm.group_id = ? ORDER BY gm.added_at DESC""",
            (group_id,)
        ).fetchall()
    return templates.TemplateResponse(request=request, name="group_detail.html", context={"group": dict(group), "members": [dict(m) for m in members]})


@app.post("/groups/{group_id}/members")
def add_group_member(group_id: int, bug_id: int = Form(...)) -> RedirectResponse:
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        try:
            conn.execute("INSERT INTO group_members (group_id, bug_id, added_at) VALUES (?, ?, ?)", (group_id, bug_id, now))
            conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (now, group_id))
        except Exception:
            pass
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@app.post("/groups/{group_id}/members/{bug_id}/remove")
def remove_group_member(group_id: int, bug_id: int) -> RedirectResponse:
    now = _utc_now_iso()
    with connect(settings.db_path) as conn:
        conn.execute("DELETE FROM group_members WHERE group_id = ? AND bug_id = ?", (group_id, bug_id))
        conn.execute("UPDATE groups SET updated_at = ? WHERE id = ?", (now, group_id))
    return RedirectResponse(f"/groups/{group_id}", status_code=303)
