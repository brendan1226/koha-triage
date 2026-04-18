import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS bugs (
    id INTEGER PRIMARY KEY,
    bug_id INTEGER NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL,
    resolution TEXT,
    product TEXT NOT NULL,
    component TEXT NOT NULL,
    severity TEXT,
    priority TEXT,
    creator TEXT,
    assigned_to TEXT,
    creation_time TEXT NOT NULL,
    last_change_time TEXT NOT NULL,
    url TEXT NOT NULL,
    keywords TEXT,
    harvested_at TEXT NOT NULL,
    embedding BLOB,
    embedded_at TEXT,
    embed_text_hash TEXT
);
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    bz_comment_id INTEGER NOT NULL UNIQUE,
    count INTEGER NOT NULL,
    author TEXT,
    body TEXT,
    creation_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    added_at TEXT NOT NULL,
    UNIQUE(group_id, bug_id)
);
CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    model TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(bug_id)
);
CREATE TABLE IF NOT EXISTS code_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    file_path TEXT NOT NULL,
    original_content TEXT,
    fixed_content TEXT NOT NULL,
    explanation TEXT,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS code_fix_meta (
    bug_id INTEGER PRIMARY KEY REFERENCES bugs(id),
    commit_message TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT NOT NULL,
    patch_data TEXT
);
CREATE TABLE IF NOT EXISTS harvest_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_harvested_at TEXT,
    total_bugs INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    picture_url TEXT,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    github_token TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bugs_status ON bugs(status);
CREATE INDEX IF NOT EXISTS idx_bugs_component ON bugs(component);
CREATE INDEX IF NOT EXISTS idx_bugs_embed_hash ON bugs(embed_text_hash);
CREATE INDEX IF NOT EXISTS idx_comments_bug ON comments(bug_id);
CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id);
CREATE INDEX IF NOT EXISTS idx_group_members_bug ON group_members(bug_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_bug ON recommendations(bug_id);
CREATE INDEX IF NOT EXISTS idx_code_fixes_bug ON code_fixes(bug_id);
CREATE TABLE IF NOT EXISTS qa_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id INTEGER NOT NULL REFERENCES bugs(id),
    patch_author TEXT,
    model TEXT NOT NULL,
    review_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qa_reviews_bug ON qa_reviews(bug_id);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current < 2:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS qa_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bug_id INTEGER NOT NULL,
                patch_author TEXT,
                model TEXT NOT NULL,
                review_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_qa_reviews_bug ON qa_reviews(bug_id);
        """)
    if current < SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
