"""Durable UI observations and queue; absence never means deletion.

Post/comment rows retain the latest known data, not an invented complete history.
Their first/last run IDs identify provenance. Coverage describes visible UI only.
Anonymous identities are scoped to a run and post and never promoted to source IDs.
"""

from __future__ import annotations

import csv
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import hashlib
from .models import DiscoveryResult, PostRef, PostSnapshot, utc_now
from .confessions import PARSER_VERSION, parse_confessions, parse_confessions_full

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    page_url TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    discovery_found INTEGER,
    discovery_scrolls INTEGER,
    discovery_stop_reason TEXT,
    history_complete INTEGER NOT NULL DEFAULT 0 CHECK (history_complete = 0)
);
CREATE TABLE IF NOT EXISTS jobs (
    post_key TEXT PRIMARY KEY,
    source_id TEXT,
    source_url TEXT NOT NULL,
    page_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'observed', 'partial', 'unavailable', 'error')),
    reason TEXT,
    enqueued_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    claimed_at TEXT,
    finished_at TEXT,
    last_snapshot_at TEXT,
    run_id INTEGER REFERENCES runs(id),
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_page_state ON jobs(page_url, status, finished_at);
CREATE TABLE IF NOT EXISTS posts (
    post_key TEXT PRIMARY KEY REFERENCES jobs(post_key),
    text TEXT NOT NULL,
    attachments TEXT NOT NULL,
    published_text TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    content_observed_at TEXT,
    first_run_id INTEGER NOT NULL REFERENCES runs(id),
    last_run_id INTEGER NOT NULL REFERENCES runs(id),
    status TEXT NOT NULL,
    stop_reason TEXT,
    sort_order TEXT,
    post_expanded INTEGER NOT NULL,
    comments_expanded INTEGER NOT NULL,
    content_expanded INTEGER NOT NULL,
    ui_comment_count TEXT,
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete = 0)
);
CREATE TABLE IF NOT EXISTS comments (
    post_key TEXT NOT NULL REFERENCES posts(post_key),
    source_id TEXT NOT NULL,
    text TEXT NOT NULL,
    permalink TEXT,
    parent_source_id TEXT,
    published_text TEXT,
    published_at TEXT,
    author_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_run_id INTEGER NOT NULL REFERENCES runs(id),
    last_run_id INTEGER NOT NULL REFERENCES runs(id),
    observation_status TEXT NOT NULL,
    post_expanded INTEGER NOT NULL,
    comments_expanded INTEGER NOT NULL,
    content_expanded INTEGER NOT NULL DEFAULT 0,
    content_kind TEXT NOT NULL DEFAULT 'text' CHECK (content_kind IN ('text', 'media', 'unknown')),
    PRIMARY KEY (post_key, source_id)
);
CREATE TABLE IF NOT EXISTS comment_observations (
    post_key TEXT NOT NULL REFERENCES posts(post_key),
    run_id INTEGER NOT NULL REFERENCES runs(id),
    observation_id TEXT NOT NULL,
    source_id TEXT CHECK (source_id IS NULL),
    text TEXT NOT NULL,
    permalink TEXT,
    parent_source_id TEXT,
    published_text TEXT,
    published_at TEXT,
    author_name TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    observation_status TEXT NOT NULL,
    post_expanded INTEGER NOT NULL,
    comments_expanded INTEGER NOT NULL,
    content_expanded INTEGER NOT NULL DEFAULT 0,
    content_kind TEXT NOT NULL DEFAULT 'text' CHECK (content_kind IN ('text', 'media', 'unknown')),
    PRIMARY KEY (post_key, run_id, observation_id)
);

CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    reason TEXT,
    model TEXT NOT NULL,
    model_digest TEXT,
    prompt_version TEXT NOT NULL,
    options_json TEXT NOT NULL,
    attempted_count INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    review_count INTEGER NOT NULL DEFAULT 0,
    skipped_empty_count INTEGER NOT NULL DEFAULT 0,
    cached_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS post_analysis_state (
    post_key TEXT PRIMARY KEY REFERENCES posts(post_key),
    is_confession INTEGER NOT NULL,
    confession_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('parsed', 'skipped_no_confession', 'needs_review')),
    reason TEXT,
    post_text_hash TEXT,
    parser_version TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confessions (
    confession_key TEXT PRIMARY KEY,
    post_key TEXT NOT NULL REFERENCES posts(post_key),
    confession_index INTEGER NOT NULL,
    confession_number TEXT NOT NULL,
    text TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (post_key, confession_number)
);
CREATE INDEX IF NOT EXISTS idx_confessions_post ON confessions(post_key);
CREATE TABLE IF NOT EXISTS comment_analyses (
    post_key TEXT NOT NULL REFERENCES posts(post_key),
    comment_key TEXT NOT NULL,
    is_observation INTEGER NOT NULL DEFAULT 0,
    scope TEXT NOT NULL CHECK (scope IN ('confession', 'post_general', 'commenter', 'off_topic', 'unresolved')),
    input_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'needs_review', 'skipped_empty', 'error', 'stale')),
    error_message TEXT,
    reason_code TEXT,
    model TEXT NOT NULL,
    model_digest TEXT,
    prompt_version TEXT NOT NULL,
    raw_response TEXT,
    run_id INTEGER REFERENCES analysis_runs(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (post_key, comment_key)
);
CREATE INDEX IF NOT EXISTS idx_ca_post_status ON comment_analyses(post_key, status);
CREATE TABLE IF NOT EXISTS comment_analysis_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_key TEXT NOT NULL,
    comment_key TEXT NOT NULL,
    confession_key TEXT NOT NULL,
    confession_number TEXT NOT NULL,
    sentiment TEXT CHECK (sentiment IN ('positive', 'negative', 'neutral')),
    link_method TEXT NOT NULL CHECK (link_method IN ('explicit_reference', 'conversation_context', 'semantic_inference')),
    link_evidence_json TEXT NOT NULL,
    sentiment_evidence_json TEXT NOT NULL,
    FOREIGN KEY (post_key, comment_key) REFERENCES comment_analyses(post_key, comment_key) ON DELETE CASCADE,
    UNIQUE (post_key, comment_key, confession_key)
);
CREATE INDEX IF NOT EXISTS idx_cat_post_cfs ON comment_analysis_targets(post_key, confession_number);
CREATE INDEX IF NOT EXISTS idx_cat_post_comment ON comment_analysis_targets(post_key, comment_key);
"""

_EXPORTS = (
    ("post", "posts.csv", """
        SELECT p.*, j.source_id, j.source_url, j.page_url,
               j.status AS job_status, j.reason AS job_reason
        FROM posts p JOIN jobs j USING (post_key)
    """, "j.page_url", "p.post_key"),
    ("comment", "comments.csv", """
        SELECT c.*, j.source_id AS post_source_id, j.source_url AS post_url,
               j.page_url, p.status AS post_status, p.stop_reason AS post_stop_reason,
               p.complete, j.status AS job_status
        FROM comments c JOIN jobs j USING (post_key) JOIN posts p USING (post_key)
    """, "j.page_url", "c.post_key, c.source_id"),
    ("comment_observation", "comment_observations.csv", """
        SELECT c.*, j.source_id AS post_source_id, j.source_url AS post_url,
               j.page_url, p.status AS post_status, p.stop_reason AS post_stop_reason,
               p.complete, j.status AS job_status
        FROM comment_observations c JOIN jobs j USING (post_key)
        JOIN posts p USING (post_key)
    """, "j.page_url", "c.post_key, c.run_id, c.observation_id"),
    ("run", "runs.csv", "SELECT * FROM runs", "page_url", "id"),
    ("job", "jobs.csv", "SELECT * FROM jobs", "page_url", "post_key"),
)

_ANALYSIS_EXPORTS = (
    ("confession", "confessions.csv", """
        SELECT c.*, j.page_url
        FROM confessions c JOIN posts p USING (post_key) JOIN jobs j USING (post_key)
    """, "j.page_url", "c.post_key, c.confession_index"),
    ("comment_analysis", "comment_analyses.csv", """
        SELECT ca.*, j.page_url
        FROM comment_analyses ca JOIN posts p USING (post_key) JOIN jobs j USING (post_key)
    """, "j.page_url", "ca.post_key, ca.comment_key"),
    ("comment_analysis_target", "comment_analysis_targets.csv", """
        SELECT cat.*, j.page_url
        FROM comment_analysis_targets cat JOIN posts p USING (post_key) JOIN jobs j USING (post_key)
    """, "j.page_url", "cat.post_key, cat.comment_key, cat.confession_number"),
    ("analysis_run", "analysis_runs.csv", "SELECT * FROM analysis_runs", None, "id"),
)


def _csv_cell(value: Any) -> Any:
    """Neutralize formulas, including prefixes hidden behind whitespace/control text."""
    if isinstance(value, str):
        stripped = value.lstrip(" \t\r\n\v\f\ufeff\x00")
        if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
            return "'" + value
    return value


class Store:
    def __init__(self, path: str | Path, read_only: bool = False):
        self.path = Path(path)
        self.read_only = bool(read_only)
        if self.read_only:
            if str(path) != ":memory:":
                if not self.path.exists():
                    raise FileNotFoundError(f"Database does not exist: {self.path}")
                uri = f"file:{self.path.resolve().as_posix()}?mode=ro"
                self._connection = sqlite3.connect(uri, uri=True, timeout=30)
            else:
                self._connection = sqlite3.connect(":memory:", timeout=30)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA query_only = ON")
            return

        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path), timeout=30)
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not tables:
                self._connection.executescript(_SCHEMA)
                self._connection.execute("PRAGMA user_version = 3")
            else:
                self._migrate_comment_kinds()
                self._migrate_analysis_tables()
                self._migrate_analysis_v3()
                self._connection.executescript(_SCHEMA)
        except BaseException:
            self._connection.close()
            raise

    def _check_writable(self) -> None:
        if self.read_only:
            raise sqlite3.OperationalError("Cannot mutate store in read-only mode")

    def _migrate_comment_kinds(self) -> None:
        if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        with self._connection:
            # Serialize first-open migrations with status/export processes too.
            self._connection.execute("BEGIN IMMEDIATE")
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 1:
                return
            existing_tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table in ("comments", "comment_observations"):
                if table not in existing_tables:
                    continue
                columns = {row["name"] for row in self._connection.execute(f"PRAGMA table_info({table})")}
                if "content_kind" not in columns:
                    self._connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'text' "
                        "CHECK (content_kind IN ('text', 'media', 'unknown'))"
                    )
                    self._connection.execute(
                        f"UPDATE {table} SET content_kind = 'unknown' WHERE text = '' AND content_expanded = 0"
                    )
            self._connection.execute("PRAGMA user_version = 1")

    def _migrate_analysis_tables(self) -> None:
        if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
            return
        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            if self._connection.execute("PRAGMA user_version").fetchone()[0] >= 2:
                return
            tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "comment_analyses" not in tables and "post_analysis_state" not in tables:
                self._connection.execute("PRAGMA user_version = 2")
                return
            self._connection.execute("PRAGMA user_version = 2")

    def _migrate_analysis_v3(self) -> None:
        user_ver = self._connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        ca_cols = set()
        if "comment_analyses" in tables:
            ca_cols = {row["name"] for row in self._connection.execute("PRAGMA table_info(comment_analyses)")}

        needs_migration = user_ver < 3 or ("comment_analyses" in tables and "comment_key" not in ca_cols)
        if not needs_migration:
            return

        # SQLite online backup before destructive table rebuild; do not back up :memory:
        if str(self.path) != ":memory:":
            backup_path = self.path.with_name(f"{self.path.name}.bak")
            backup_index = 0
            while True:
                try:
                    with backup_path.open("xb"):
                        pass
                    break
                except FileExistsError:
                    backup_index += 1
                    backup_path = self.path.with_name(f"{self.path.name}.bak.{backup_index}")
            backup_conn = sqlite3.connect(str(backup_path))
            try:
                self._connection.backup(backup_conn)
            finally:
                backup_conn.close()

        with self._connection:
            self._connection.execute("BEGIN IMMEDIATE")
            user_ver = self._connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row[0]
                for row in self._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "comment_analyses" in tables:
                ca_cols = {row["name"] for row in self._connection.execute("PRAGMA table_info(comment_analyses)")}
            if user_ver >= 3 and ("comment_analyses" not in tables or "comment_key" in ca_cols):
                return

            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS analysis_runs (
                    id INTEGER PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    reason TEXT,
                    model TEXT NOT NULL,
                    model_digest TEXT,
                    prompt_version TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    attempted_count INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    review_count INTEGER NOT NULL DEFAULT 0,
                    skipped_empty_count INTEGER NOT NULL DEFAULT 0,
                    cached_count INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            if "post_analysis_state" in tables:
                pas_cols = {row["name"] for row in self._connection.execute("PRAGMA table_info(post_analysis_state)")}
                if "post_text_hash" not in pas_cols:
                    self._connection.execute("ALTER TABLE post_analysis_state ADD COLUMN post_text_hash TEXT")
                if "parser_version" not in pas_cols:
                    self._connection.execute("ALTER TABLE post_analysis_state ADD COLUMN parser_version TEXT")
                if "reason" not in pas_cols:
                    self._connection.execute("ALTER TABLE post_analysis_state ADD COLUMN reason TEXT")

            # Rebuild confessions table to change UNIQUE constraint from (post_key, confession_index) to (post_key, confession_number)
            if "confessions" in tables:
                self._connection.execute(
                    """CREATE TABLE confessions_v3 (
                        confession_key TEXT PRIMARY KEY,
                        post_key TEXT NOT NULL REFERENCES posts(post_key),
                        confession_index INTEGER NOT NULL,
                        confession_number TEXT NOT NULL,
                        text TEXT NOT NULL,
                        start_char INTEGER NOT NULL,
                        end_char INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        UNIQUE (post_key, confession_number)
                    )"""
                )
                cfs_rows = self._connection.execute(
                    "SELECT * FROM confessions ORDER BY confession_index ASC"
                ).fetchall()
                for cr in cfs_rows:
                    cd = dict(cr)
                    cfs_key = f"{cd['post_key']}:{cd['confession_number']}"
                    self._connection.execute(
                        """INSERT OR REPLACE INTO confessions_v3
                           (confession_key, post_key, confession_index, confession_number,
                            text, start_char, end_char, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            cfs_key,
                            cd["post_key"],
                            cd["confession_index"],
                            cd["confession_number"],
                            cd["text"],
                            cd["start_char"],
                            cd["end_char"],
                            cd["created_at"],
                        ),
                    )
                self._connection.execute("DROP TABLE confessions")
                self._connection.execute("ALTER TABLE confessions_v3 RENAME TO confessions")
                self._connection.execute("CREATE INDEX IF NOT EXISTS idx_confessions_post ON confessions(post_key)")

            if "comment_analyses" in tables and "comment_key" not in ca_cols:
                self._connection.execute(
                    """CREATE TABLE comment_analyses_v3 (
                        post_key TEXT NOT NULL REFERENCES posts(post_key),
                        comment_key TEXT NOT NULL,
                        is_observation INTEGER NOT NULL DEFAULT 0,
                        scope TEXT NOT NULL CHECK (scope IN ('confession', 'post_general', 'commenter', 'off_topic', 'unresolved')),
                        input_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('success', 'needs_review', 'skipped_empty', 'error', 'stale')),
                        error_message TEXT,
                        reason_code TEXT,
                        model TEXT NOT NULL,
                        model_digest TEXT,
                        prompt_version TEXT NOT NULL,
                        raw_response TEXT,
                        run_id INTEGER REFERENCES analysis_runs(id),
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (post_key, comment_key)
                    )"""
                )
                self._connection.execute(
                    """CREATE TABLE comment_analysis_targets_v3 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        post_key TEXT NOT NULL,
                        comment_key TEXT NOT NULL,
                        confession_key TEXT NOT NULL,
                        confession_number TEXT NOT NULL,
                        sentiment TEXT CHECK (sentiment IN ('positive', 'negative', 'neutral')),
                        link_method TEXT NOT NULL CHECK (link_method IN ('explicit_reference', 'conversation_context', 'semantic_inference')),
                        link_evidence_json TEXT NOT NULL,
                        sentiment_evidence_json TEXT NOT NULL,
                        FOREIGN KEY (post_key, comment_key) REFERENCES comment_analyses_v3(post_key, comment_key) ON DELETE CASCADE,
                        UNIQUE (post_key, comment_key, confession_key)
                    )"""
                )

                v2_analyses = self._connection.execute("SELECT * FROM comment_analyses").fetchall()
                v2_targets_by_key: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
                if "comment_analysis_targets" in tables:
                    cat_rows = self._connection.execute("SELECT * FROM comment_analysis_targets").fetchall()
                    for tr in cat_rows:
                        td = dict(tr)
                        k = (td["post_key"], str(td["comment_id"]), int(td.get("is_observation", 0)))
                        v2_targets_by_key.setdefault(k, []).append(td)

                for ar in v2_analyses:
                    ad = dict(ar)
                    pkey = ad["post_key"]
                    cid = str(ad["comment_id"])
                    is_obs = int(ad.get("is_observation", 0))

                    if is_obs == 1:
                        ckey = f"obs:legacy:{cid}"
                        status = "stale"
                        scope = "unresolved"
                        reason_code = "legacy_anonymous_unresolved"
                        error_msg = ad.get("error_message") or "Legacy anonymous analysis lacks run_id"
                    else:
                        ckey = f"fb:{cid}"
                        status = ad.get("status") or "needs_review"
                        if status not in {"success", "needs_review", "skipped_empty", "error", "stale"}:
                            status = "needs_review"
                        scope = ad.get("scope") or "unresolved"
                        if scope not in {"confession", "post_general", "commenter", "off_topic", "unresolved"}:
                            scope = "unresolved"
                        reason_code = None
                        error_msg = ad.get("error_message")

                    self._connection.execute(
                        """INSERT OR REPLACE INTO comment_analyses_v3
                           (post_key, comment_key, is_observation, scope, input_hash,
                            status, error_message, reason_code, model, model_digest,
                            prompt_version, raw_response, run_id, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            pkey,
                            ckey,
                            is_obs,
                            scope,
                            ad.get("input_hash") or "legacy_v2_migrated",
                            status,
                            error_msg,
                            reason_code,
                            ad.get("model") or "legacy",
                            None,
                            "v2_migrated",
                            ad.get("raw_response"),
                            None,
                            ad.get("created_at") or utc_now(),
                            ad.get("updated_at") or utc_now(),
                        ),
                    )

                    targets = v2_targets_by_key.get((pkey, cid, is_obs), [])
                    if is_obs == 0 and status == "success" and (
                        scope == "unresolved"
                        or (scope == "confession" and not targets)
                        or any(t.get("sentiment") not in {"positive", "negative", "neutral"} for t in targets)
                    ):
                        self._connection.execute(
                            """UPDATE comment_analyses_v3
                               SET status = 'needs_review', reason_code = 'incomplete_source'
                               WHERE post_key = ? AND comment_key = ?""",
                            (pkey, ckey),
                        )
                    for tgt in targets:
                        cfs_num = str(tgt["confession_number"])
                        cfs_key = f"{pkey}:{cfs_num}"
                        sent = tgt.get("sentiment")
                        if sent not in {"positive", "negative", "neutral"}:
                            sent = None

                        lm = ad.get("link_method")
                        if lm not in {"explicit_reference", "conversation_context", "semantic_inference"}:
                            lm = "semantic_inference"

                        link_ev = tgt.get("link_evidence")
                        if link_ev:
                            try:
                                parsed = json.loads(link_ev)
                                link_ev_json = (
                                    json.dumps(parsed, ensure_ascii=False)
                                    if not isinstance(parsed, str)
                                    else json.dumps([{"source_type": "target_comment", "source_key": ckey, "quote": parsed}], ensure_ascii=False)
                                )
                            except Exception:
                                link_ev_json = json.dumps([{"source_type": "target_comment", "source_key": ckey, "quote": str(link_ev)}], ensure_ascii=False)
                        else:
                            link_ev_json = "[]"

                        sent_ev = tgt.get("sentiment_evidence")
                        if sent_ev:
                            try:
                                parsed = json.loads(sent_ev)
                                sent_ev_json = (
                                    json.dumps(parsed, ensure_ascii=False)
                                    if not isinstance(parsed, str)
                                    else json.dumps([{"source_type": "target_comment", "source_key": ckey, "quote": parsed}], ensure_ascii=False)
                                )
                            except Exception:
                                sent_ev_json = json.dumps([{"source_type": "target_comment", "source_key": ckey, "quote": str(sent_ev)}], ensure_ascii=False)
                        else:
                            sent_ev_json = "[]"

                        self._connection.execute(
                            """INSERT OR REPLACE INTO comment_analysis_targets_v3
                               (post_key, comment_key, confession_key, confession_number,
                                 sentiment, link_method, link_evidence_json, sentiment_evidence_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                pkey,
                                ckey,
                                cfs_key,
                                cfs_num,
                                sent,
                                lm,
                                link_ev_json,
                                sent_ev_json,
                            ),
                        )

                self._connection.execute("DROP TABLE IF EXISTS comment_analysis_targets")
                self._connection.execute("DROP TABLE IF EXISTS comment_analyses")
                self._connection.execute("ALTER TABLE comment_analyses_v3 RENAME TO comment_analyses")
                self._connection.execute("ALTER TABLE comment_analysis_targets_v3 RENAME TO comment_analysis_targets")
                self._connection.execute("CREATE INDEX IF NOT EXISTS idx_ca_post_status ON comment_analyses(post_key, status)")
                self._connection.execute("CREATE INDEX IF NOT EXISTS idx_cat_post_cfs ON comment_analysis_targets(post_key, confession_number)")
                self._connection.execute("CREATE INDEX IF NOT EXISTS idx_cat_post_comment ON comment_analysis_targets(post_key, comment_key)")

            self._connection.execute("PRAGMA user_version = 3")
    def __enter__(self) -> Store:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def start_run(self, page_url: str) -> int:
        self._check_writable()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO runs(page_url, started_at, status) VALUES (?, ?, 'running')",
                (page_url, utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self, run_id: int, status: str, reason: str | None = None,
        discovery: DiscoveryResult | None = None,
    ) -> None:
        self._check_writable()
        if not status or status == "running":
            raise ValueError("A finished run needs a terminal status.")
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE runs SET finished_at = ?, status = ?, reason = ?,
                   discovery_found = COALESCE(?, discovery_found),
                   discovery_scrolls = COALESCE(?, discovery_scrolls),
                   discovery_stop_reason = COALESCE(?, discovery_stop_reason)
                   WHERE id = ?""",
                (utc_now(), status, reason,
                 discovery.found if discovery else None,
                 discovery.scrolls if discovery else None,
                 discovery.stop_reason if discovery else None, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown run: {run_id}")

    def recover_jobs(self) -> int:
        """Recover after acquiring the process lock, including interrupted discovery."""
        self._check_writable()
        now = utc_now()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE jobs SET status = 'pending', reason = 'interrupted',
                   finished_at = ? WHERE status = 'running'""", (now,),
            )
            recovered = cursor.rowcount
            self._connection.execute(
                """UPDATE runs SET status = 'interrupted', reason = 'process_interrupted',
                   finished_at = ? WHERE status = 'running'""", (now,),
            )
        return recovered

    def enqueue(self, ref: PostRef, page_url: str) -> bool:
        self._check_writable()
        now = utc_now()
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO jobs(post_key, source_id, source_url, page_url,
                   enqueued_at, discovered_at) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(post_key) DO NOTHING""",
                (ref.key, ref.source_id, ref.url, page_url, now, now),
            )
            inserted = cursor.rowcount == 1
            if not inserted:
                self._connection.execute(
                    """UPDATE jobs SET discovered_at = ?, source_url = ?,
                       source_id = COALESCE(?, source_id) WHERE post_key = ?""",
                    (now, ref.url, ref.source_id, ref.key),
                )
        return inserted

    def select_jobs(
        self, page_url: str, limit: int, revisit_after_hours: float,
        retry_failed: bool = False,
    ) -> list[PostRef]:
        if limit < 0 or not math.isfinite(revisit_after_hours) or revisit_after_hours < 0:
            raise ValueError("Job limit and revisit interval must be finite and nonnegative.")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=revisit_after_hours)).isoformat()
        rows = self._connection.execute(
            """SELECT source_url, source_id FROM jobs WHERE page_url = ? AND (
                status = 'pending'
                OR (status IN ('observed', 'partial', 'unavailable')
                    AND (finished_at IS NULL OR julianday(finished_at) <= julianday(?)))
                OR (status = 'error' AND ?))
                ORDER BY CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                         COALESCE(finished_at, enqueued_at), post_key LIMIT ?""",
            (page_url, cutoff, int(retry_failed), limit),
        )
        return [PostRef(row["source_url"], row["source_id"]) for row in rows]

    def claim(self, ref: PostRef, run_id: int) -> None:
        self._check_writable()
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE jobs SET status = 'running', reason = NULL, claimed_at = ?,
                   finished_at = NULL, run_id = ?, attempts = attempts + 1
                   WHERE post_key = ? AND status != 'running'
                   AND EXISTS (SELECT 1 FROM runs r WHERE r.id = ?
                               AND r.status = 'running' AND r.page_url = jobs.page_url)""",
                (utc_now(), run_id, ref.key, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Claim requires an enqueued, nonrunning job and an active matching run.")

    def save_snapshot(self, run_id: int, snapshot: PostSnapshot) -> None:
        self._check_writable()
        if snapshot.status not in {"observed", "partial", "unavailable"}:
            raise ValueError(f"Invalid snapshot status: {snapshot.status}")
        key = snapshot.ref.key
        with self._connection:
            if not self._connection.in_transaction:
                self._connection.execute("BEGIN IMMEDIATE")
            old_fp = self.get_analysis_source_fingerprint(key)
            cursor = self._connection.execute(
                """UPDATE jobs SET last_snapshot_at = ? WHERE post_key = ?
                   AND run_id = ? AND status = 'running'
                   AND EXISTS (SELECT 1 FROM runs WHERE id = ? AND status = 'running')""",
                (snapshot.observed_at, key, run_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("A snapshot requires the job to be claimed by its active run.")
            previous = self._connection.execute(
                "SELECT * FROM posts WHERE post_key = ?", (key,),
            ).fetchone()
            attachments = json.loads(previous["attachments"]) if previous else []
            known_attachments = set(attachments)
            for attachment in snapshot.attachments:
                if attachment not in known_attachments:
                    attachments.append(attachment)
                    known_attachments.add(attachment)
            # An unavailable/collapsed view cannot erase previously expanded content.
            replace_text = snapshot.status != "unavailable" and (
                snapshot.post_expanded or (
                    bool(snapshot.text) and (
                        previous is None or not previous["text"] or not previous["content_expanded"]
                    )
                )
            )
            text = snapshot.text if replace_text else (previous["text"] if previous else "")
            content_at = snapshot.observed_at if replace_text else (
                previous["content_observed_at"] if previous else None
            )
            content_expanded = int(snapshot.post_expanded) if replace_text else (
                previous["content_expanded"] if previous else 0
            )
            self._connection.execute(
                """INSERT INTO posts(
                    post_key, text, attachments, published_text, published_at,
                    first_seen_at, last_seen_at, content_observed_at, first_run_id,
                    last_run_id, status, stop_reason, sort_order, post_expanded,
                    comments_expanded, content_expanded, ui_comment_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(post_key) DO UPDATE SET text = excluded.text,
                    attachments = excluded.attachments,
                    published_text = COALESCE(excluded.published_text, posts.published_text),
                    published_at = COALESCE(excluded.published_at, posts.published_at),
                    last_seen_at = excluded.last_seen_at,
                    content_observed_at = excluded.content_observed_at,
                    last_run_id = excluded.last_run_id, status = excluded.status,
                    stop_reason = excluded.stop_reason, sort_order = excluded.sort_order,
                    post_expanded = excluded.post_expanded,
                    comments_expanded = excluded.comments_expanded,
                    content_expanded = excluded.content_expanded,
                    ui_comment_count = COALESCE(excluded.ui_comment_count, posts.ui_comment_count)""",
                (key, text, json.dumps(attachments, ensure_ascii=False),
                 snapshot.published_text, snapshot.published_at, snapshot.observed_at,
                 snapshot.observed_at, content_at, run_id, run_id, snapshot.status,
                 snapshot.stop_reason, snapshot.sort_order, int(snapshot.post_expanded),
                 int(snapshot.comments_expanded), content_expanded, snapshot.ui_comment_count),
            )
            for comment in snapshot.comments:
                values = (
                    comment.text, comment.permalink, comment.parent_source_id,
                    comment.published_text, comment.published_at, comment.author_name,
                    snapshot.observed_at, snapshot.observed_at,
                )
                coverage = (snapshot.status, int(snapshot.post_expanded), int(snapshot.comments_expanded), int(comment.text_expanded), comment.content_kind)
                if comment.source_id:
                    self._connection.execute(
                        """INSERT INTO comments(
                            post_key, source_id, text, permalink, parent_source_id,
                            published_text, published_at, author_name, first_seen_at,
                            last_seen_at, first_run_id, last_run_id, observation_status,
                            post_expanded, comments_expanded, content_expanded, content_kind)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(post_key, source_id) DO UPDATE SET
                            text = CASE WHEN excluded.content_expanded OR
                                (excluded.text != '' AND comments.content_expanded = 0)
                                THEN excluded.text ELSE comments.text END,
                            content_kind = CASE WHEN excluded.content_expanded OR
                                (excluded.text != '' AND comments.content_expanded = 0)
                                THEN excluded.content_kind ELSE comments.content_kind END,
                            permalink = COALESCE(excluded.permalink, comments.permalink),
                            parent_source_id = COALESCE(excluded.parent_source_id, comments.parent_source_id),
                            published_text = COALESCE(excluded.published_text, comments.published_text),
                            published_at = COALESCE(excluded.published_at, comments.published_at),
                            author_name = COALESCE(excluded.author_name, comments.author_name),
                            last_seen_at = excluded.last_seen_at, last_run_id = excluded.last_run_id,
                            observation_status = excluded.observation_status,
                            post_expanded = excluded.post_expanded,
                            comments_expanded = excluded.comments_expanded,
                            content_expanded = MAX(comments.content_expanded, excluded.content_expanded)""",
                        (key, comment.source_id, *values, run_id, run_id, *coverage),
                    )
                else:
                    if not comment.observation_id:
                        raise ValueError("An anonymous comment requires a local observation_id.")
                    self._connection.execute(
                        """INSERT INTO comment_observations(
                            post_key, run_id, observation_id, text, permalink, parent_source_id,
                            published_text, published_at, author_name, first_seen_at, last_seen_at,
                            observation_status, post_expanded, comments_expanded, content_expanded, content_kind)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(post_key, run_id, observation_id) DO UPDATE SET
                            text = CASE WHEN excluded.content_expanded OR
                                (excluded.text != '' AND comment_observations.content_expanded = 0)
                                THEN excluded.text ELSE comment_observations.text END,
                            content_kind = CASE WHEN excluded.content_expanded OR
                                (excluded.text != '' AND comment_observations.content_expanded = 0)
                                THEN excluded.content_kind ELSE comment_observations.content_kind END,
                            permalink = COALESCE(excluded.permalink, comment_observations.permalink),
                            parent_source_id = COALESCE(excluded.parent_source_id, comment_observations.parent_source_id),
                            published_text = COALESCE(excluded.published_text, comment_observations.published_text),
                            published_at = COALESCE(excluded.published_at, comment_observations.published_at),
                            author_name = COALESCE(excluded.author_name, comment_observations.author_name),
                            last_seen_at = excluded.last_seen_at,
                            observation_status = excluded.observation_status,
                            post_expanded = excluded.post_expanded,
                            comments_expanded = excluded.comments_expanded,
                            content_expanded = MAX(comment_observations.content_expanded, excluded.content_expanded)""",
                        (key, run_id, comment.observation_id, *values, *coverage),
                    )
            new_fp = self.get_analysis_source_fingerprint(key)
            if old_fp and old_fp != new_fp:
                try:
                    self._connection.execute(
                        """UPDATE comment_analyses
                           SET status = 'stale', updated_at = ?
                           WHERE post_key = ? AND status != 'stale'""",
                        (snapshot.observed_at, key),
                    )
                except sqlite3.OperationalError:
                    pass

    def finish_job(self, ref: PostRef, status: str, reason: str | None = None) -> None:
        self._check_writable()
        if status not in {"observed", "partial", "unavailable", "error", "pending"}:
            raise ValueError(f"Invalid finished job status: {status}")
        with self._connection:
            cursor = self._connection.execute(
                """UPDATE jobs SET status = ?, reason = ?, finished_at = ?
                   WHERE post_key = ? AND status = 'running'""",
                (status, reason, utc_now(), ref.key),
            )
            if cursor.rowcount != 1:
                raise ValueError("Only a running job can be finished.")

    def status(self, page_url: str | None = None) -> dict:
        counts: dict[str, Any] = {}
        where = " WHERE j.page_url = ?" if page_url is not None else ""
        parameters = (page_url,) if page_url is not None else ()
        try:
            self._connection.execute("BEGIN")
            try:
                for label, table in (("posts", "posts"), ("comments", "comments"),
                                     ("anonymous_observations", "comment_observations")):
                    counts[label] = self._connection.execute(
                        f"SELECT count(*) FROM {table} t JOIN jobs j USING (post_key){where}", parameters,
                    ).fetchone()[0]
                counts["jobs"] = {row["status"]: row["count"] for row in self._connection.execute(
                    f"SELECT j.status, count(*) AS count FROM jobs j{where} GROUP BY j.status", parameters,
                )}
                counts["recent_runs"] = [dict(row) for row in self._connection.execute(
                    "SELECT * FROM runs" + (" WHERE page_url = ?" if page_url is not None else "")
                    + " ORDER BY id DESC LIMIT 10", parameters,
                )]
            finally:
                self._connection.rollback()
        except sqlite3.OperationalError:
            return {"posts": 0, "comments": 0, "anonymous_observations": 0, "jobs": {}, "recent_runs": []}
        return counts
    def export(
        self,
        output: Path,
        format: str,
        page_url: str | None = None,
        include_analysis: bool = False,
    ) -> dict:
        if format not in {"jsonl", "csv"}:
            raise ValueError("Export format must be jsonl or csv.")
        output = Path(output)
        active_exports = _EXPORTS + _ANALYSIS_EXPORTS if include_analysis else _EXPORTS
        if str(self.path) != ":memory:":
            protected = {self.path.resolve(), Path(str(self.path) + "-wal").resolve(),
                         Path(str(self.path) + "-shm").resolve()}
            destinations = [output] if format == "jsonl" else [output / item[1] for item in active_exports]
            if any(destination.resolve() in protected for destination in destinations):
                raise ValueError("An export cannot overwrite its source database.")
        if format == "csv":
            output.mkdir(parents=True, exist_ok=True)
            if any(output.iterdir()):
                raise FileExistsError(f"CSV export directory is not empty: {output}")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
        counts = {item[0]: 0 for item in active_exports}
        parameters = (page_url,) if page_url is not None else ()
        self._connection.execute("BEGIN")
        json_file = None
        try:
            if format == "jsonl":
                json_file = output.open("x", encoding="utf-8", newline="\n")
            for record_type, filename, query, page_column, ordering in active_exports:
                if page_url is not None and page_column is not None:
                    cursor = self._connection.execute(f"{query} WHERE {page_column} = ? ORDER BY {ordering}", (page_url,))
                else:
                    cursor = self._connection.execute(f"{query} ORDER BY {ordering}")
                if json_file is not None:
                    for row in cursor:
                        record = dict(row)
                        if record_type == "post":
                            record["attachments"] = json.loads(record["attachments"])
                        json_file.write(json.dumps({"type": record_type, **record}, ensure_ascii=False) + "\n")
                        counts[record_type] += 1
                else:
                    with (output / filename).open("x", encoding="utf-8-sig", newline="") as csv_file:
                        writer = csv.writer(csv_file)
                        writer.writerow(column[0] for column in cursor.description)
                        for row in cursor:
                            writer.writerow(_csv_cell(value) for value in row)
                            counts[record_type] += 1
        finally:
            try:
                if json_file is not None:
                    json_file.close()
            finally:
                self._connection.rollback()
        return counts

    def ensure_post_confessions(
        self,
        post_key: str,
        text: str,
        is_incomplete: bool = False,
        force: bool = False,
    ) -> list[dict[str, Any]]:
        """Extract and persist confession segments for a post if not already analyzed."""
        if self.read_only:
            try:
                cfs_rows = self._connection.execute(
                    "SELECT * FROM confessions WHERE post_key = ? ORDER BY confession_index ASC",
                    (post_key,),
                ).fetchall()
                return [dict(r) for r in cfs_rows]
            except sqlite3.OperationalError:
                return []

        text_hash = hashlib.sha256(
            json.dumps([text, bool(is_incomplete)], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        row = self._connection.execute(
            """SELECT is_confession, confession_count, status, reason, post_text_hash, parser_version
               FROM post_analysis_state WHERE post_key = ?""",
            (post_key,),
        ).fetchone()

        if (
            row is not None
            and not force
            and row["post_text_hash"] == text_hash
            and row["parser_version"] == PARSER_VERSION
        ):
            cfs_rows = self._connection.execute(
                "SELECT * FROM confessions WHERE post_key = ? ORDER BY confession_index ASC",
                (post_key,),
            ).fetchall()
            return [dict(r) for r in cfs_rows]

        parse_res = parse_confessions_full(text, is_incomplete=is_incomplete)
        now = utc_now()
        is_cfs = 1 if parse_res.is_confession else 0
        with self._connection:
            try:
                old_segs = [
                    (r["confession_number"], r["text"])
                    for r in self._connection.execute(
                        "SELECT confession_number, text FROM confessions WHERE post_key = ? ORDER BY confession_index ASC",
                        (post_key,),
                    ).fetchall()
                ]
            except sqlite3.OperationalError:
                old_segs = []

            self._connection.execute(
                """INSERT OR REPLACE INTO post_analysis_state
                   (post_key, is_confession, confession_count, status, reason,
                    post_text_hash, parser_version, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post_key,
                    is_cfs,
                    len(parse_res.segments),
                    parse_res.status,
                    parse_res.reason,
                    text_hash,
                    parse_res.parser_version,
                    now,
                ),
            )
            self._connection.execute("DELETE FROM confessions WHERE post_key = ?", (post_key,))
            new_segs = []
            if parse_res.status == "parsed":
                for seg in parse_res.segments:
                    cfs_key = f"{post_key}:{seg.confession_number}"
                    new_segs.append((seg.confession_number, seg.text))
                    self._connection.execute(
                        """INSERT OR REPLACE INTO confessions
                           (confession_key, post_key, confession_index, confession_number,
                            text, start_char, end_char, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            cfs_key,
                            post_key,
                            seg.index,
                            seg.confession_number,
                            seg.text,
                            seg.start_char,
                            seg.end_char,
                            now,
                        ),
                    )

            if old_segs and old_segs != new_segs:
                try:
                    self._connection.execute(
                        """UPDATE comment_analyses
                           SET status = 'stale', updated_at = ?
                           WHERE post_key = ? AND status != 'stale'""",
                        (now, post_key),
                    )
                except sqlite3.OperationalError:
                    pass

        cfs_rows = self._connection.execute(
            "SELECT * FROM confessions WHERE post_key = ? ORDER BY confession_index ASC",
            (post_key,),
        ).fetchall()
        return [dict(r) for r in cfs_rows]

    def get_post_confessions(self, post_key: str) -> list[dict[str, Any]]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM confessions WHERE post_key = ? ORDER BY confession_index ASC",
                (post_key,),
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def get_post_analysis_state(self, post_key: str) -> dict[str, Any] | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM post_analysis_state WHERE post_key = ?",
                (post_key,),
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None

    def start_analysis_run(
        self,
        model: str,
        model_digest: str | None,
        prompt_version: str,
        options: dict[str, Any],
    ) -> int:
        self._check_writable()
        now = utc_now()
        with self._connection:
            cursor = self._connection.execute(
                """INSERT INTO analysis_runs
                   (started_at, status, model, model_digest, prompt_version, options_json)
                   VALUES (?, 'running', ?, ?, ?, ?)""",
                (now, model, model_digest, prompt_version, json.dumps(options)),
            )
            return int(cursor.lastrowid)

    def finish_analysis_run(
        self,
        run_id: int,
        status: str,
        reason: str | None,
        counts: dict[str, int],
    ) -> None:
        self._check_writable()
        now = utc_now()
        with self._connection:
            self._connection.execute(
                """UPDATE analysis_runs
                   SET finished_at = ?, status = ?, reason = ?,
                       attempted_count = ?, success_count = ?, review_count = ?,
                       skipped_empty_count = ?, cached_count = ?, error_count = ?
                   WHERE id = ?""",
                (
                    now,
                    status,
                    reason,
                    counts.get("attempted", 0),
                    counts.get("success", 0),
                    counts.get("needs_review", 0),
                    counts.get("skipped_empty", 0),
                    counts.get("cached", 0),
                    counts.get("error", 0),
                    run_id,
                ),
            )

    def get_analysis_run(self, run_id: int) -> dict[str, Any] | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM analysis_runs WHERE id = ?", (run_id,)
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.OperationalError:
            return None

    def get_comments_for_post(self, post_key: str) -> list[dict[str, Any]]:
        """Fetch all visible comments and observations for a post, with uniform keys."""
        columns = {
            table: {r["name"] for r in self._connection.execute(f"PRAGMA table_info({table})")}
            for table in ("comments", "comment_observations")
        }
        comment_kind = "c.content_kind" if "content_kind" in columns["comments"] else (
            "CASE WHEN c.text = '' THEN 'unknown' ELSE 'text' END"
        )
        observation_kind = "co.content_kind" if "content_kind" in columns["comment_observations"] else (
            "CASE WHEN co.text = '' THEN 'unknown' ELSE 'text' END"
        )
        query = f"""
            SELECT c.post_key, 'fb:' || c.source_id AS comment_key,
                   0 AS is_observation, c.source_id AS source_id,
                   CASE WHEN c.parent_source_id IS NOT NULL AND c.parent_source_id != ''
                        THEN 'fb:' || c.parent_source_id ELSE NULL END AS parent_comment_key,
                   c.text, c.author_name, {comment_kind} AS content_kind, c.content_expanded,
                   c.first_seen_at, c.published_at
            FROM comments c
            WHERE c.post_key = ?
            UNION ALL
            SELECT co.post_key, 'obs:' || co.run_id || ':' || co.observation_id AS comment_key,
                   1 AS is_observation, co.observation_id AS source_id,
                   CASE WHEN co.parent_source_id IS NOT NULL AND co.parent_source_id != ''
                        THEN 'fb:' || co.parent_source_id ELSE NULL END AS parent_comment_key,
                   co.text, co.author_name, {observation_kind} AS content_kind, co.content_expanded,
                   co.first_seen_at, co.published_at
            FROM comment_observations co
            WHERE co.post_key = ?
            ORDER BY first_seen_at ASC
        """
        try:
            rows = self._connection.execute(query, (post_key, post_key)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def get_comment_analysis(
        self, post_key: str, comment_key: str
     ) -> dict[str, Any] | None:
        try:
            row = self._connection.execute(
                "SELECT * FROM comment_analyses WHERE post_key = ? AND comment_key = ?",
                (post_key, comment_key),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            t_rows = self._connection.execute(
                """SELECT confession_key, confession_number, sentiment, link_method,
                          link_evidence_json, sentiment_evidence_json
                   FROM comment_analysis_targets
                   WHERE post_key = ? AND comment_key = ?
                   ORDER BY id ASC""",
                (post_key, comment_key),
            ).fetchall()
            targets = []
            for tr in t_rows:
                td = dict(tr)
                td["link_evidence"] = json.loads(td.pop("link_evidence_json") or "[]")
                td["sentiment_evidence"] = json.loads(td.pop("sentiment_evidence_json") or "[]")
                targets.append(td)
            d["targets"] = targets
            return d
        except sqlite3.OperationalError:
            return None

    def get_analysis_source_fingerprint(self, post_key: str) -> str:
        """Return a deterministic fingerprint of effective current post content and comment tree.

        Hashes effective current post text, completeness, availability, and sorted comment
        identities, text, parent, kind, completeness (omits observation times and authors).
        """
        try:
            post_row = self._connection.execute(
                "SELECT text, status, content_expanded FROM posts WHERE post_key = ?",
                (post_key,),
            ).fetchone()
            if not post_row:
                return ""

            comments_query = """
                SELECT 'fb:' || source_id AS comment_key, text, parent_source_id, content_kind, content_expanded
                FROM comments
                WHERE post_key = ?
                UNION ALL
                SELECT 'obs:' || run_id || ':' || observation_id AS comment_key, text, parent_source_id, content_kind, content_expanded
                FROM comment_observations
                WHERE post_key = ?
                ORDER BY comment_key ASC
            """
            comment_rows = self._connection.execute(comments_query, (post_key, post_key)).fetchall()

            data = {
                "post_key": post_key,
                "text": post_row["text"] or "",
                "available": post_row["status"] != "unavailable",
                "content_expanded": int(post_row["content_expanded"] or 0),
                "comments": [
                    {
                        "key": r["comment_key"],
                        "text": r["text"] or "",
                        "parent": r["parent_source_id"] or "",
                        "kind": r["content_kind"] or "text",
                        "content_expanded": int(r["content_expanded"] or 0),
                    }
                    for r in comment_rows
                ],
            }
            serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
            return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        except sqlite3.OperationalError:
            return ""

    def save_comment_analysis(
        self,
        post_key: str,
        comment_key: str,
        is_observation: bool,
        scope: str,
        input_hash: str,
        status: str,
        model: str,
        prompt_version: str,
        targets: list[dict[str, Any]],
        error_message: str | None = None,
        reason_code: str | None = None,
        model_digest: str | None = None,
        raw_response: str | None = None,
        run_id: int | None = None,
        source_fingerprint: str | None = None,
    ) -> bool:
        """Atomically persist a comment analysis result and its confession targets."""
        self._check_writable()
        now = utc_now()
        obs_int = 1 if is_observation else 0
        with self._connection:
            if not self._connection.in_transaction:
                self._connection.execute("BEGIN IMMEDIATE")
            if source_fingerprint is not None:
                current_fp = self.get_analysis_source_fingerprint(post_key)
                if not current_fp or current_fp != source_fingerprint:
                    return False

            self._connection.execute(
                """INSERT OR REPLACE INTO comment_analyses
                   (post_key, comment_key, is_observation, scope, input_hash,
                    status, error_message, reason_code, model, model_digest,
                    prompt_version, raw_response, run_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    post_key,
                    comment_key,
                    obs_int,
                    scope,
                    input_hash,
                    status,
                    error_message,
                    reason_code,
                    model,
                    model_digest,
                    prompt_version,
                    raw_response,
                    run_id,
                    now,
                    now,
                ),
            )
            self._connection.execute(
                "DELETE FROM comment_analysis_targets WHERE post_key = ? AND comment_key = ?",
                (post_key, comment_key),
            )
            for target in targets:
                link_ev_json = (
                    json.dumps(target.get("link_evidence", []), ensure_ascii=False)
                    if not isinstance(target.get("link_evidence"), str)
                    else target.get("link_evidence")
                )
                sent_ev_json = (
                    json.dumps(target.get("sentiment_evidence", []), ensure_ascii=False)
                    if not isinstance(target.get("sentiment_evidence"), str)
                    else target.get("sentiment_evidence")
                )
                cfs_num = target.get("confession_number") or target["confession_key"].split(":")[-1]
                self._connection.execute(
                    """INSERT INTO comment_analysis_targets
                       (post_key, comment_key, confession_key, confession_number,
                        sentiment, link_method, link_evidence_json, sentiment_evidence_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        post_key,
                        comment_key,
                        target["confession_key"],
                        cfs_num,
                        target.get("sentiment"),
                        target["link_method"],
                        link_ev_json,
                        sent_ev_json,
                    ),
                )
            return True

    def get_analysis_stats(self) -> dict[str, Any]:
        """Return aggregated stats of confessions and comment sentiment analyses."""
        def _count(query: str, params: tuple = ()) -> int:
            try:
                row = self._connection.execute(query, params).fetchone()
                return int(row[0]) if row else 0
            except sqlite3.OperationalError:
                return 0

        total_posts = _count("SELECT COUNT(*) FROM posts")
        confession_posts = _count(
            "SELECT COUNT(*) FROM post_analysis_state WHERE is_confession = 1 AND status = 'parsed'"
        )
        skipped_posts = _count(
            "SELECT COUNT(*) FROM post_analysis_state WHERE status = 'skipped_no_confession'"
        )
        review_posts = _count(
            "SELECT COUNT(*) FROM post_analysis_state WHERE status = 'needs_review'"
        )
        total_confessions = _count("SELECT COUNT(*) FROM confessions")

        total_comments = (
            _count("SELECT COUNT(*) FROM comments")
            + _count("SELECT COUNT(*) FROM comment_observations")
        )
        analyzed_comments = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE status = 'success'"
        )
        review_comments = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE status = 'needs_review'"
        )
        skipped_empty = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE status = 'skipped_empty'"
        )
        error_comments = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE status = 'error'"
        )
        stale_comments = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE status = 'stale'"
        )

        pos_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE cat.sentiment = 'positive' AND ca.status = 'success'"""
        )
        neg_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE cat.sentiment = 'negative' AND ca.status = 'success'"""
        )
        neu_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE cat.sentiment = 'neutral' AND ca.status = 'success'"""
        )
        unassigned_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE cat.sentiment IS NULL AND ca.status = 'success'"""
        )

        review_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE ca.status = 'needs_review'"""
        )
        stale_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE ca.status = 'stale'"""
        )
        anonymous_comments = _count("SELECT COUNT(*) FROM comment_observations")
        anonymous_analyzed_comments = _count(
            "SELECT COUNT(*) FROM comment_analyses WHERE is_observation = 1 AND status = 'success'"
        )
        anonymous_targets = _count(
            """SELECT COUNT(*) FROM comment_analysis_targets cat
               JOIN comment_analyses ca ON ca.post_key = cat.post_key AND ca.comment_key = cat.comment_key
               WHERE ca.is_observation = 1 AND ca.status = 'success'"""
        )

        try:
            scope_rows = self._connection.execute(
                "SELECT scope, COUNT(*) as cnt FROM comment_analyses WHERE status = 'success' GROUP BY scope"
            ).fetchall()
            scopes = {r["scope"]: r["cnt"] for r in scope_rows}
        except sqlite3.OperationalError:
            scopes = {}

        return {
            "total_posts": total_posts,
            "confession_posts": confession_posts,
            "skipped_posts": skipped_posts,
            "review_posts": review_posts,
            "total_confessions": total_confessions,
            "total_comments": total_comments,
            "analyzed_comments": analyzed_comments,
            "review_comments": review_comments,
            "skipped_empty": skipped_empty,
            "error_comments": error_comments,
            "stale_comments": stale_comments,
            "positive_targets": pos_targets,
            "negative_targets": neg_targets,
            "neutral_targets": neu_targets,
            "unassigned_targets": unassigned_targets,
            "scopes": scopes,
            "review_targets": review_targets,
            "stale_targets": stale_targets,
            "anonymous_comments": anonymous_comments,
            "anonymous_analyzed_comments": anonymous_analyzed_comments,
            "anonymous_targets": anonymous_targets,
        }

    def get_post_analysis_detail(self, post_key: str) -> dict[str, Any]:
        """Fetch confessions and comment analyses with targets for a single post."""
        try:
            confessions = self.get_post_confessions(post_key)
            ca_rows = self._connection.execute(
                "SELECT * FROM comment_analyses WHERE post_key = ?", (post_key,)
            ).fetchall()
            analyses: dict[str, dict[str, Any]] = {}
            for r in ca_rows:
                d = dict(r)
                ck = d["comment_key"]
                t_rows = self._connection.execute(
                    """SELECT confession_key, confession_number, sentiment, link_method,
                              link_evidence_json, sentiment_evidence_json
                       FROM comment_analysis_targets
                       WHERE post_key = ? AND comment_key = ?
                       ORDER BY id ASC""",
                    (post_key, ck),
                ).fetchall()
                targets = []
                for tr in t_rows:
                    td = dict(tr)
                    td["link_evidence"] = json.loads(td.pop("link_evidence_json") or "[]")
                    td["sentiment_evidence"] = json.loads(td.pop("sentiment_evidence_json") or "[]")
                    targets.append(td)
                d["targets"] = targets
                analyses[ck] = d

            return {
                "confessions": confessions,
                "analyses": analyses,
            }
        except sqlite3.OperationalError:
            return {
                "confessions": [],
                "analyses": {},
            }

    def get_pending_comment_analyses(
        self,
        post_key: str | None = None,
        limit: int | None = None,
        retry_failed: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch comments for valid confession posts that need sentiment analysis."""
        status_filter = "('success', 'skipped_empty')" if retry_failed else "('success', 'skipped_empty', 'error')"
        query = f"""
            SELECT c.post_key, 'fb:' || c.source_id AS comment_key, 0 AS is_observation,
                   c.text, c.parent_source_id, c.author_name, c.content_kind
            FROM comments c
            JOIN post_analysis_state pas ON pas.post_key = c.post_key
            LEFT JOIN comment_analyses ca
                   ON ca.post_key = c.post_key AND ca.comment_key = ('fb:' || c.source_id)
            WHERE pas.is_confession = 1 AND pas.status = 'parsed'
              AND (ca.status IS NULL OR ca.status NOT IN {status_filter})
              {"AND c.post_key = ?" if post_key else ""}
            UNION ALL
            SELECT co.post_key, 'obs:' || co.run_id || ':' || co.observation_id AS comment_key, 1 AS is_observation,
                   co.text, co.parent_source_id, co.author_name, co.content_kind
            FROM comment_observations co
            JOIN post_analysis_state pas ON pas.post_key = co.post_key
            LEFT JOIN comment_analyses ca
                   ON ca.post_key = co.post_key AND ca.comment_key = ('obs:' || co.run_id || ':' || co.observation_id)
            WHERE pas.is_confession = 1 AND pas.status = 'parsed'
              AND (ca.status IS NULL OR ca.status NOT IN {status_filter})
              {"AND co.post_key = ?" if post_key else ""}
            ORDER BY post_key ASC
            {"LIMIT ?" if limit is not None else ""}
        """
        params: list[Any] = []
        if post_key:
            params.extend([post_key, post_key])
        if limit is not None:
            params.append(limit)

        try:
            rows = self._connection.execute(query, tuple(params)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
