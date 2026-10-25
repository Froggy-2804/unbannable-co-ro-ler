"""Local web viewer server for crawled posts and hierarchical comments."""

from __future__ import annotations

import html
import json
import os
import re
from contextlib import contextmanager
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


@contextmanager
def _get_db(db_path: Path):
    if not db_path.exists():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def get_stats(db_path: Path) -> dict[str, Any]:
    with _get_db(db_path) as conn:
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        comments = conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0]
        obs = conn.execute("SELECT COUNT(*) FROM comment_observations").fetchone()[0]
        jobs = {
            row["status"]: row["cnt"]
            for row in conn.execute("SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status").fetchall()
        }
        recent_runs = [
            dict(row)
            for row in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 5").fetchall()
        ]
        return {
            "posts_count": posts,
            "comments_count": comments + obs,
            "jobs": jobs,
            "recent_runs": recent_runs,
        }


def get_posts_list(db_path: Path, search: str = "", sort: str = "newest") -> list[dict[str, Any]]:
    with _get_db(db_path) as conn:
        query = """
            SELECT p.post_key, p.text, p.published_text, p.published_at, p.first_seen_at,
                   p.last_seen_at, p.status, p.stop_reason, p.ui_comment_count,
                   j.source_url, j.page_url,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_key = p.post_key) +
                   (SELECT COUNT(*) FROM comment_observations co WHERE co.post_key = p.post_key) AS comment_count
            FROM posts p
            LEFT JOIN jobs j ON j.post_key = p.post_key
        """
        params: list[Any] = []
        if search:
            query += " WHERE p.text LIKE ? OR p.post_key LIKE ? "
            term = f"%{search}%"
            params.extend([term, term])

        if sort == "most_comments":
            query += " ORDER BY comment_count DESC, p.rowid DESC"
        elif sort == "oldest":
            query += " ORDER BY p.rowid ASC"
        else:  # newest
            query += " ORDER BY p.rowid DESC"

        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            text = r["text"] or ""
            # Extract confession tags like #3694, #3692
            tags = re.findall(r"#\d+", text)
            first_line = text.strip().split("\n")[0] if text else "(Bài viết không có nội dung chữ)"
            snippet = text.replace("\n", " ")[:200]
            result.append({
                "post_key": r["post_key"],
                "tags": tags[:3],
                "first_line": first_line[:100],
                "snippet": snippet,
                "published_text": r["published_text"],
                "published_at": r["published_at"],
                "first_seen_at": r["first_seen_at"],
                "status": r["status"],
                "stop_reason": r["stop_reason"],
                "source_url": r["source_url"],
                "comment_count": r["comment_count"],
                "ui_comment_count": r["ui_comment_count"],
            })
        return result


def get_post_detail(db_path: Path, post_key: str) -> dict[str, Any] | None:
    with _get_db(db_path) as conn:
        post_row = conn.execute(
            """
            SELECT p.*, j.source_url, j.page_url,
                   (SELECT COUNT(*) FROM comments c WHERE c.post_key = p.post_key) +
                   (SELECT COUNT(*) FROM comment_observations co WHERE co.post_key = p.post_key) AS comment_count
            FROM posts p
            LEFT JOIN jobs j ON j.post_key = p.post_key
            WHERE p.post_key = ?
            """,
            (post_key,),
        ).fetchone()

        if not post_row:
            return None

        # Fetch comments
        comment_columns = {r["name"] for r in conn.execute("PRAGMA table_info(comments)")}
        observation_columns = {r["name"] for r in conn.execute("PRAGMA table_info(comment_observations)")}
        comment_kind = "content_kind" if "content_kind" in comment_columns else "'unknown'"
        observation_kind = "content_kind" if "content_kind" in observation_columns else "'unknown'"
        comments_rows = conn.execute(
            f"""
            SELECT source_id, text, permalink, parent_source_id, published_text,
                   published_at, author_name, observation_status, {comment_kind} AS content_kind,
                   0 as is_anonymous
            FROM comments
            WHERE post_key = ?
            ORDER BY rowid ASC
            """,
            (post_key,),
        ).fetchall()

        obs_rows = conn.execute(
            f"""
            SELECT observation_id as source_id, text, permalink, parent_source_id, published_text,
                   published_at, author_name, observation_status, {observation_kind} AS content_kind, run_id,
                   1 as is_anonymous
            FROM comment_observations
            WHERE post_key = ?
            ORDER BY rowid ASC
            """,
            (post_key,),
        ).fetchall()
        all_comments = [dict(c) for c in comments_rows] + [dict(c) for c in obs_rows]

        # Fetch confessions if table exists
        confessions: list[dict[str, Any]] = []
        try:
            cfs_rows = conn.execute(
                """SELECT confession_key, confession_number, text, start_char, end_char
                   FROM confessions WHERE post_key = ? ORDER BY confession_index ASC""",
                (post_key,),
            ).fetchall()
            confessions = [dict(r) for r in cfs_rows]
        except sqlite3.OperationalError:
            pass

        # Fetch comment analyses and targets
        analyses_by_comment: dict[str, dict[str, Any]] = {}
        try:
            ca_rows = conn.execute(
                "SELECT * FROM comment_analyses WHERE post_key = ? AND comment_key IS NOT NULL", (post_key,)
            ).fetchall()
            for car in ca_rows:
                cad = dict(car)
                ck = cad["comment_key"]
                t_rows = conn.execute(
                    """SELECT confession_key, confession_number, sentiment, link_method,
                              link_evidence_json, sentiment_evidence_json
                       FROM comment_analysis_targets
                       WHERE post_key = ? AND comment_key = ? ORDER BY id ASC""",
                    (post_key, ck),
                ).fetchall()
                targets = []
                for tr in t_rows:
                    td = dict(tr)
                    td["link_evidence"] = json.loads(td.pop("link_evidence_json") or "[]")
                    td["sentiment_evidence"] = json.loads(td.pop("sentiment_evidence_json") or "[]")
                    targets.append(td)
                cad["targets"] = targets
                analyses_by_comment[ck] = cad
        except sqlite3.OperationalError:
            pass

        for c in all_comments:
            if c.get("is_anonymous"):
                c["comment_key"] = f"obs:{c.get('run_id', 0)}:{c['source_id']}"
            else:
                c["comment_key"] = f"fb:{c['source_id']}"
            c["analysis"] = analyses_by_comment.get(c["comment_key"])

        # Map to build reply tree
        by_id: dict[str, dict[str, Any]] = {}
        for c in all_comments:
            c["replies"] = []
            sid = c["source_id"]
            if sid:
                by_id[sid] = c

        top_level: list[dict[str, Any]] = []
        for c in all_comments:
            parent_id = c.get("parent_source_id")
            if parent_id and parent_id in by_id and parent_id != c.get("source_id"):
                parent = by_id[parent_id]
                c["parent_author_name"] = parent.get("author_name")
                parent["replies"].append(c)
            else:
                top_level.append(c)

        post_data = dict(post_row)
        post_data["tags"] = re.findall(r"#\d+", post_data.get("text") or "")
        post_data["confessions"] = confessions
        post_data["comment_analyses"] = analyses_by_comment
        post_data["comments_tree"] = top_level
        post_data["total_loaded_comments"] = len(all_comments)
        return post_data

class ViewerHandler(BaseHTTPRequestHandler):
    db_path: Path = Path("data/crawler.sqlite3")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy standard request logging
        pass

    def _send_json(self, data: Any, status: int = 200) -> None:
        content = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _send_html(self, content_str: str) -> None:
        content = content_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self._send_html(HTML_TEMPLATE)
            return

        if path == "/api/stats":
            self._send_json(get_stats(self.db_path))
            return

        if path == "/api/analysis/stats":
            if not self.db_path.exists():
                self._send_json({
                    "total_posts": 0,
                    "confession_posts": 0,
                    "skipped_posts": 0,
                    "review_posts": 0,
                    "total_confessions": 0,
                    "total_comments": 0,
                    "analyzed_comments": 0,
                    "review_comments": 0,
                    "skipped_empty": 0,
                    "error_comments": 0,
                    "stale_comments": 0,
                    "positive_targets": 0,
                    "negative_targets": 0,
                    "neutral_targets": 0,
                    "unassigned_targets": 0,
                    "scopes": {},
                    "review_targets": 0,
                    "stale_targets": 0,
                    "anonymous_comments": 0,
                    "anonymous_analyzed_comments": 0,
                    "anonymous_targets": 0,
                })
                return
            try:
                from .storage import Store
                with Store(self.db_path, read_only=True) as store:
                    self._send_json(store.get_analysis_stats())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        if path == "/api/posts":
            search = query.get("q", [""])[0]
            sort = query.get("sort", ["newest"])[0]
            posts = get_posts_list(self.db_path, search=search, sort=sort)
            self._send_json({"posts": posts, "total": len(posts)})
            return

        if path.startswith("/api/posts/"):
            post_key = path[len("/api/posts/"):]
            post_key = urllib.parse.unquote(post_key)
            detail = get_post_detail(self.db_path, post_key)
            if detail is None:
                self._send_json({"error": "Post not found"}, status=404)
            else:
                self._send_json(detail)
            return

        self.send_error(404, "Not Found")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>UIT Confession Viewer</title>
  <style>
    :root {
      --bg: #090d16;
      --surface: #131b2e;
      --surface-elevated: #1b2640;
      --border: #233252;
      --border-subtle: #1c2842;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.15);
      --accent-hover: #0ea5e9;
      --tag-bg: rgba(56, 189, 248, 0.12);
      --tag-text: #38bdf8;
      --success: #34d399;
      --success-bg: rgba(52, 211, 153, 0.12);
      --warning: #fbbf24;
      --warning-bg: rgba(251, 191, 36, 0.12);
      --media-badge: #c084fc;
      --media-bg: rgba(192, 132, 252, 0.12);
      --reply-line: #2d3f63;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --mono: ui-monospace, "SF Mono", "Cascadia Code", "Fira Code", monospace;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: var(--font);
      background-color: var(--bg);
      color: var(--text);
      height: 100vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      -webkit-font-smoothing: antialiased;
    }

    /* Header */
    header {
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      z-index: 20;
      flex-shrink: 0;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      text-decoration: none;
      color: var(--text);
    }

    .brand-badge {
      background: linear-gradient(135deg, #0284c7, #0369a1);
      color: #fff;
      font-weight: 700;
      font-size: 13px;
      padding: 4px 10px;
      border-radius: 6px;
      letter-spacing: 0.5px;
      box-shadow: 0 2px 8px rgba(2, 132, 199, 0.3);
    }

    .brand-title {
      font-size: 16px;
      font-weight: 600;
      letter-spacing: -0.2px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 12px;
      flex: 1;
      max-width: 650px;
      justify-content: flex-end;
    }

    .search-box {
      position: relative;
      flex: 1;
      max-width: 380px;
    }

    .search-input {
      width: 100%;
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 14px 8px 34px;
      color: var(--text);
      font-size: 13px;
      outline: none;
      transition: all 0.15s ease;
    }

    .search-input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-glow);
    }

    .search-icon {
      position: absolute;
      left: 11px;
      top: 50%;
      transform: translateY(-50%);
      color: var(--text-dim);
      font-size: 14px;
      pointer-events: none;
    }

    .btn {
      background: var(--surface-elevated);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
      text-decoration: none;
      white-space: nowrap;
    }

    .btn:hover {
      background: var(--border);
      border-color: var(--text-dim);
      color: #fff;
    }

    .btn-primary {
      background: #0284c7;
      border-color: #0369a1;
      color: #fff;
    }
    .btn-primary:hover {
      background: #0369a1;
    }

    .sort-select {
      background: var(--bg);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 13px;
      outline: none;
      cursor: pointer;
    }

    /* Main Container Split View */
    .app-body {
      display: flex;
      flex: 1;
      overflow: hidden;
      position: relative;
    }

    /* Left Sidebar: Post List */
    .posts-sidebar {
      width: 420px;
      border-right: 1px solid var(--border);
      background: var(--surface);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      height: 100%;
    }

    .sidebar-meta {
      padding: 10px 16px;
      font-size: 12px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border-subtle);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(15, 23, 42, 0.4);
    }

    .posts-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .posts-scroll::-webkit-scrollbar {
      width: 6px;
    }
    .posts-scroll::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 3px;
    }

    .post-card {
      background: var(--bg);
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      padding: 14px;
      cursor: pointer;
      transition: all 0.15s ease;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .post-card:hover {
      border-color: var(--border);
      background: var(--surface-elevated);
      transform: translateY(-1px);
    }

    .post-card.active {
      border-color: var(--accent);
      background: var(--surface-elevated);
      box-shadow: 0 0 0 1px var(--accent), 0 4px 12px rgba(0, 0, 0, 0.2);
    }

    .post-card-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .tag-badge {
      background: var(--tag-bg);
      color: var(--tag-text);
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 5px;
    }

    .card-time {
      font-size: 11px;
      color: var(--text-dim);
    }

    .card-snippet {
      font-size: 13px;
      line-height: 1.5;
      color: #cbd5e1;
      display: -webkit-box;
      -webkit-line-clamp: 3;
      -webkit-box-orient: vertical;
      overflow: hidden;
      word-break: break-word;
    }

    .card-footer {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
      font-size: 11px;
    }

    .chip {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 500;
    }

    .chip-comments {
      background: rgba(148, 163, 184, 0.1);
      color: var(--text-muted);
    }

    .chip-status-observed {
      background: var(--success-bg);
      color: var(--success);
    }

    .chip-status-partial {
      background: var(--warning-bg);
      color: var(--warning);
    }

    /* Right Detail View */
    .detail-view {
      flex: 1;
      overflow-y: auto;
      background: var(--bg);
      display: flex;
      flex-direction: column;
      height: 100%;
    }

    .detail-view::-webkit-scrollbar {
      width: 8px;
    }
    .detail-view::-webkit-scrollbar-thumb {
      background: var(--border);
      border-radius: 4px;
    }

    .detail-container {
      max-width: 860px;
      width: 100%;
      margin: 0 auto;
      padding: 28px 32px 60px 32px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }

    /* Post Main Content Box */
    .post-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.2);
    }

    .post-meta-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border-subtle);
      padding-bottom: 14px;
    }

    .post-meta-left {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .post-body-text {
      font-size: 15px;
      line-height: 1.7;
      color: #f1f5f9;
      white-space: pre-wrap;
      word-break: break-word;
    }

    /* Confessions Section */
    .confessions-card-grid {
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-top: 14px;
    }
    .confession-card {
      background: var(--surface-elevated);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .confession-card:hover {
      border-color: var(--accent);
      background: rgba(56, 189, 248, 0.08);
    }
    .confession-card.active {
      border-color: var(--accent);
      background: var(--accent-glow);
    }
    .confession-card-header {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 700;
      color: var(--accent);
      font-size: 13px;
      margin-bottom: 4px;
    }
    .confession-card-body {
      font-size: 13px;
      color: var(--text);
      line-height: 1.5;
    }
    /* Confession Filter Bar */
    .confession-filter-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
      margin-bottom: 14px;
      padding: 10px 14px;
      background: var(--surface);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
    }
    .filter-btn {
      background: var(--surface-elevated);
      color: var(--text-muted);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s;
    }
    .filter-btn:hover {
      color: var(--text);
      border-color: var(--accent);
    }
    .filter-btn.active {
      background: var(--accent);
      color: #090d16;
      border-color: var(--accent);
      font-weight: 600;
    }
    /* Sentiment Badges */
    .sentiment-badges-wrap {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 6px;
      margin-bottom: 4px;
    }
    .sentiment-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 12px;
    }
    .sentiment-badge.positive {
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
      border: 1px solid rgba(16, 185, 129, 0.35);
    }
    .sentiment-badge.negative {
      background: rgba(239, 68, 68, 0.15);
      color: #f87171;
      border: 1px solid rgba(239, 68, 68, 0.35);
    }
    .sentiment-badge.neutral {
      background: rgba(148, 163, 184, 0.15);
      color: #cbd5e1;
      border: 1px solid rgba(148, 163, 184, 0.35);
    }
    .sentiment-badge.unassigned {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.35);
    }
    .scope-badge {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 11px;
      font-weight: 500;
      padding: 2px 7px;
      border-radius: 10px;
      background: rgba(168, 85, 247, 0.15);
      color: #c084fc;
      border: 1px solid rgba(168, 85, 247, 0.3);
    }
    /* Evidence Details Box */
    .evidence-box {
      margin-top: 8px;
      padding: 6px 10px;
      background: rgba(19, 27, 46, 0.6);
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      font-size: 12px;
      color: var(--text-muted);
    }
    .evidence-box summary {
      cursor: pointer;
      font-weight: 500;
      color: var(--accent);
      user-select: none;
    }
    .evidence-box summary:hover {
      text-decoration: underline;
    }
    .evidence-content {
      margin-top: 6px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding-top: 4px;
      border-top: 1px solid var(--border-subtle);
    }
    .evidence-item {
      display: flex;
      gap: 6px;
      font-size: 11px;
      line-height: 1.4;
    }
    .evidence-label {
      color: var(--text-dim);
      font-weight: 600;
      min-width: 75px;
    }
    .evidence-quote {
      color: var(--text);
      font-style: italic;
    }

    /* Comment Section */
    .comments-section {
      display: flex;
      flex-direction: column;
      gap: 18px;
    }

    .comments-header-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid var(--border);
      padding-bottom: 10px;
    }

    .comments-count-title {
      font-size: 16px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .comments-thread-list {
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    /* Single Comment Item */
    .comment-item {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .comment-card {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }

    .avatar-circle {
      width: 36px;
      height: 36px;
      border-radius: 50%;
      background: #2563eb;
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 13px;
      font-weight: 700;
      flex-shrink: 0;
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
      text-transform: uppercase;
    }

    .comment-bubble {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-width: 95%;
      box-shadow: 0 1px 3px rgba(0,0,0,0.1);
    }

    .comment-author-name {
      font-size: 13px;
      font-weight: 700;
      color: #f8fafc;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .comment-author-name.anonymous {
      color: var(--text-dim);
      font-style: italic;
      font-weight: 500;
    }

    .comment-text {
      font-size: 14px;
      line-height: 1.5;
      color: #e2e8f0;
      white-space: pre-wrap;
      word-break: break-word;
    }

    .media-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--media-bg);
      color: var(--media-badge);
      border: 1px solid rgba(192, 132, 252, 0.3);
      padding: 4px 10px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      margin-top: 4px;
    }

    .comment-meta-footer {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 11px;
      color: var(--text-dim);
      padding-left: 48px;
    }

    .comment-time {
      color: var(--text-dim);
    }

    .comment-link-btn {
      color: var(--text-dim);
      text-decoration: none;
      transition: color 0.15s ease;
    }
    .comment-link-btn:hover {
      color: var(--accent);
      text-decoration: underline;
    }

    /* Replies Accordion Toggle */
    .replies-toggle-btn {
      background: none;
      border: none;
      color: var(--accent);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 6px;
      margin-left: 44px;
      align-self: flex-start;
      transition: background 0.15s ease;
    }

    .replies-toggle-btn:hover {
      background: var(--accent-glow);
    }

    .replies-toggle-btn .arrow {
      transition: transform 0.2s ease;
      font-size: 10px;
    }

    .replies-toggle-btn.open .arrow {
      transform: rotate(90deg);
    }

    /* Nested Replies Container */
    .replies-container {
      margin-left: 20px;
      padding-left: 20px;
      border-left: 2px solid var(--reply-line);
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 4px;
    }

    .replies-container.hidden {
      display: none;
    }

    .reply-item {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .reply-card {
      display: flex;
      gap: 10px;
      align-items: flex-start;
    }

    .reply-card .avatar-circle {
      width: 28px;
      height: 28px;
      font-size: 11px;
    }

    .reply-card .comment-bubble {
      padding: 8px 12px;
      border-radius: 10px;
      background: var(--surface-elevated);
    }

    .parent-tag {
      color: var(--accent);
      font-weight: 600;
      margin-right: 4px;
    }

    /* Empty States */
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 60px 20px;
      color: var(--text-dim);
      text-align: center;
      gap: 12px;
    }

    .empty-icon {
      font-size: 40px;
      opacity: 0.6;
    }

    .empty-title {
      font-size: 16px;
      font-weight: 600;
      color: var(--text-muted);
    }

    .back-to-list-btn {
      display: none;
      margin-bottom: 12px;
    }

    /* Responsive */
    @media (max-width: 820px) {
      .posts-sidebar {
        width: 100%;
        border-right: none;
      }
      .detail-view {
        display: none;
      }
      .app-body.show-detail .posts-sidebar {
        display: none;
      }
      .app-body.show-detail .detail-view {
        display: flex;
      }
      .back-to-list-btn {
        display: inline-flex;
      }
      .detail-container {
        padding: 16px 16px 40px 16px;
      }
    }
  </style>
</head>
<body>

  <!-- Top App Header -->
  <header>
    <a href="/" class="brand">
      <span class="brand-badge">UIT</span>
      <span class="brand-title">Confession Archive</span>
    </a>

    <div class="header-actions">
      <div class="search-box">
        <span class="search-icon">🔍</span>
        <input type="text" id="searchInput" class="search-input" placeholder="Tìm theo confession (#3690), nội dung..." />
      </div>

      <select id="sortSelect" class="sort-select">
        <option value="newest">Mới nhất trước</option>
        <option value="most_comments">Nhiều bình luận nhất</option>
        <option value="oldest">Cũ nhất trước</option>
      </select>

      <button id="refreshBtn" class="btn" title="Làm mới dữ liệu từ SQLite">
        <span>🔄</span> Làm mới
      </button>
    </div>
  </header>

  <!-- Main Body -->
  <div class="app-body" id="appBody">

    <!-- Left Posts Sidebar -->
    <aside class="posts-sidebar">
      <div class="sidebar-meta">
        <span id="postsCountText">Đang tải danh sách bài...</span>
        <span id="totalStatsText" style="font-family: var(--mono); color: var(--accent);"></span>
      </div>

      <div class="posts-scroll" id="postsContainer">
        <!-- Rendered Post Cards -->
      </div>
    </aside>

    <!-- Right Post & Comment Detail -->
    <main class="detail-view" id="detailView">
      <div class="detail-container" id="detailContainer">
        <div class="empty-state">
          <div class="empty-icon">👈</div>
          <div class="empty-title">Chọn một bài viết bên trái để xem nội dung và bình luận chi tiết</div>
          <p style="font-size: 13px; max-width: 420px; line-height: 1.5;">
            Các bài viết và bình luận đã được đồng bộ tự động từ database SQLite. Các bình luận trả lời (replies) có thể bấm mở ra xem như trên Facebook.
          </p>
        </div>
      </div>
    </main>

  </div>

  <script>
    // State
    let currentPosts = [];
    let selectedPostKey = null;
    let postDetailCache = {};

    // Elements
    const postsContainer = document.getElementById('postsContainer');
    const detailContainer = document.getElementById('detailContainer');
    const postsCountText = document.getElementById('postsCountText');
    const totalStatsText = document.getElementById('totalStatsText');
    const searchInput = document.getElementById('searchInput');
    const sortSelect = document.getElementById('sortSelect');
    const refreshBtn = document.getElementById('refreshBtn');
    const appBody = document.getElementById('appBody');

    // Deterministic avatar color based on name
    function getAvatarColor(name) {
      if (!name) return '#475569';
      const colors = ['#2563eb', '#7c3aed', '#db2777', '#ea580c', '#059669', '#0891b2', '#4f46e5', '#d97706'];
      let hash = 0;
      for (let i = 0; i < name.length; i++) {
        hash = name.charCodeAt(i) + ((hash << 5) - hash);
      }
      return colors[Math.abs(hash) % colors.length];
    }

    function getInitials(name) {
      if (!name) return '?';
      const parts = name.trim().split(/\s+/);
      if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
      return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
    }

    function escapeHtml(text) {
      if (!text) return '';
      return text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
    }

    function highlightTags(text) {
      const safe = escapeHtml(text);
      return safe.replace(/(#\d+)/g, '<span class="tag-badge" style="display:inline-block; margin: 0 2px;">$1</span>');
    }

    // Fetch Stats
    async function loadStats() {
      try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        totalStatsText.textContent = `${data.posts_count} bài · ${data.comments_count} cmt`;
      } catch (err) {
        console.error('Failed to load stats:', err);
      }
    }

    // Fetch Posts List
    async function loadPosts() {
      const q = encodeURIComponent(searchInput.value.trim());
      const sort = sortSelect.value;
      postsCountText.textContent = 'Đang tải...';

      try {
        const res = await fetch(`/api/posts?q=${q}&sort=${sort}`);
        const data = await res.json();
        currentPosts = data.posts || [];
        renderPostsList();
      } catch (err) {
        postsCountText.textContent = 'Lỗi tải danh sách';
        postsContainer.innerHTML = '<div class="empty-state"><p>Không thể kết nối API</p></div>';
      }
    }

    // Render Posts Sidebar
    function renderPostsList() {
      postsCountText.textContent = `${currentPosts.length} bài viết`;
      if (currentPosts.length === 0) {
        postsContainer.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">📭</div>
            <div class="empty-title">Không tìm thấy bài viết</div>
            <p style="font-size: 12px;">Thử tìm với từ khóa khác hoặc bấm Làm mới</p>
          </div>
        `;
        return;
      }

      postsContainer.innerHTML = currentPosts.map(p => {
        const isActive = p.post_key === selectedPostKey ? 'active' : '';
        const tag = p.tags && p.tags.length ? p.tags[0] : (p.post_key ? p.post_key.slice(0, 10) : 'Post');
        const time = p.published_text || '';
        const statusClass = p.status === 'observed' ? 'chip-status-observed' : 'chip-status-partial';
        const statusLabel = p.status === 'observed' ? '✓ Đã crawl' : '⏳ Chưa xong';

        return `
          <div class="post-card ${isActive}" data-key="${escapeHtml(p.post_key)}" onclick="selectPost('${escapeHtml(p.post_key)}')">
            <div class="post-card-header">
              <span class="tag-badge">${escapeHtml(tag)}</span>
              <span class="card-time">${escapeHtml(time)}</span>
            </div>
            <div class="card-snippet">${escapeHtml(p.snippet || p.first_line)}</div>
            <div class="card-footer">
              <span class="chip chip-comments">💬 ${p.comment_count} bình luận</span>
              <span class="chip ${statusClass}">${statusLabel}</span>
            </div>
          </div>
        `;
      }).join('');
    }

    // Select Post & Load Detail
    async function selectPost(postKey) {
      selectedPostKey = postKey;
      renderPostsList(); // Update active class
      appBody.classList.add('show-detail'); // For mobile toggle

      detailContainer.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">⏳</div>
          <div class="empty-title">Đang tải nội dung và cây bình luận...</div>
        </div>
      `;

      try {
        let post = postDetailCache[postKey];
        if (!post) {
          const res = await fetch(`/api/posts/${encodeURIComponent(postKey)}`);
          if (!res.ok) throw new Error('Không tìm thấy bài viết');
          post = await res.json();
          postDetailCache[postKey] = post;
        }
        renderPostDetail(post);
      } catch (err) {
        detailContainer.innerHTML = `
          <div class="empty-state">
            <div class="empty-icon">⚠️</div>
            <div class="empty-title">Lỗi tải bài viết</div>
            <p style="font-size: 13px;">${escapeHtml(err.message)}</p>
          </div>
        `;
      }
    }

    function backToList() {
      appBody.classList.remove('show-detail');
    }

    function renderAnalysisBadges(analysis) {
      if (!analysis) return '';
      if (analysis.status === 'stale') {
        let html = '<div class="sentiment-badges-wrap">';
        html += '<span class="scope-badge" style="color:#94a3b8; background:rgba(148,163,184,0.15); border-color:rgba(148,163,184,0.3);">Kết quả cũ (hết hạn)</span>';
        html += '</div>';
        const targets = analysis.targets || [];
        if (targets.length > 0) {
          html += `<details class="evidence-box"><summary>🔍 Bằng chứng cũ (hết hạn)</summary><div class="evidence-content">`;
          for (const t of targets) {
            let methodLabel = t.link_method;
            if (t.link_method === 'explicit_reference') methodLabel = 'nhắc mã trực tiếp';
            else if (t.link_method === 'conversation_context') methodLabel = 'ngữ cảnh hội thoại';
            else if (t.link_method === 'semantic_inference') methodLabel = 'suy luận ngữ nghĩa';
            const sent = t.sentiment || 'chưa xác định';
            html += `<div style="font-weight:600; color:var(--text); margin-top:2px;">#${escapeHtml(t.confession_number)} (${escapeHtml(methodLabel)}) - ${escapeHtml(sent)}</div>`;
            if (t.link_evidence && t.link_evidence.length > 0) {
              for (const le of t.link_evidence) {
                html += `<div class="evidence-item"><span class="evidence-label">Liên kết:</span> <span class="evidence-quote">"${escapeHtml(le.quote || '')}"</span></div>`;
              }
            }
            if (t.sentiment_evidence && t.sentiment_evidence.length > 0) {
              for (const se of t.sentiment_evidence) {
                html += `<div class="evidence-item"><span class="evidence-label">Cảm xúc:</span> <span class="evidence-quote">"${escapeHtml(se.quote || '')}"</span></div>`;
              }
            }
          }
          html += `</div></details>`;
        }
        return html;
      }
      let html = '<div class="sentiment-badges-wrap">';
      const targets = analysis.targets || [];
      if (targets.length > 0) {
        for (const t of targets) {
          const sent = t.sentiment || 'unassigned';
          let sentLabel = 'chưa xác định';
          if (sent === 'positive') sentLabel = 'tích cực';
          else if (sent === 'negative') sentLabel = 'tiêu cực';
          else if (sent === 'neutral') sentLabel = 'trung tính';
          html += `<span class="sentiment-badge ${escapeHtml(sent)}">#${escapeHtml(t.confession_number)} · ${escapeHtml(sentLabel)}</span>`;
        }
      } else {
        const scope = analysis.scope;
        if (scope === 'post_general') html += `<span class="scope-badge">Chung cho post</span>`;
        else if (scope === 'commenter') html += `<span class="scope-badge">Về người bình luận</span>`;
        else if (scope === 'off_topic') html += `<span class="scope-badge">Ngoài chủ đề</span>`;
        else if (scope === 'unresolved') html += `<span class="scope-badge" style="color:#fbbf24; background:rgba(251,191,36,0.15); border-color:rgba(251,191,36,0.3);">Chờ kiểm tra</span>`;
        if (analysis.status === 'skipped_empty') html += `<span class="scope-badge" style="color:#94a3b8;">Không có chữ</span>`;
      }
      html += '</div>';

      if (targets.length > 0) {
        html += `<details class="evidence-box"><summary>🔍 Bằng chứng phân tích</summary><div class="evidence-content">`;
        for (const t of targets) {
          let methodLabel = t.link_method;
          if (t.link_method === 'explicit_reference') methodLabel = 'nhắc mã trực tiếp';
          else if (t.link_method === 'conversation_context') methodLabel = 'ngữ cảnh hội thoại';
          else if (t.link_method === 'semantic_inference') methodLabel = 'suy luận ngữ nghĩa';
          html += `<div style="font-weight:600; color:var(--text); margin-top:2px;">#${escapeHtml(t.confession_number)} (${escapeHtml(methodLabel)})</div>`;
          if (t.link_evidence && t.link_evidence.length > 0) {
            for (const le of t.link_evidence) {
              html += `<div class="evidence-item"><span class="evidence-label">Liên kết:</span> <span class="evidence-quote">"${escapeHtml(le.quote || '')}"</span></div>`;
            }
          }
          if (t.sentiment_evidence && t.sentiment_evidence.length > 0) {
            for (const se of t.sentiment_evidence) {
              html += `<div class="evidence-item"><span class="evidence-label">Cảm xúc:</span> <span class="evidence-quote">"${escapeHtml(se.quote || '')}"</span></div>`;
            }
          }
        }
        html += `</div></details>`;
      } else if (analysis.reason_code) {
        html += `<details class="evidence-box"><summary>ℹ️ Lý do: ${escapeHtml(analysis.reason_code)}</summary><div class="evidence-content"><div class="evidence-item"><span class="evidence-quote">${escapeHtml(analysis.error_message || analysis.reason_code)}</span></div></div></details>`;
      }
      return html;
    }

    function filterCommentsByConfession(cfsNum) {
      document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
      const activeBtn = document.getElementById('filter-cfs-' + cfsNum);
      if (activeBtn) activeBtn.classList.add('active');

      const items = document.querySelectorAll('.comment-item');
      items.forEach(item => {
        if (cfsNum === 'all') {
          item.style.display = '';
          return;
        }
        const itemCfs = item.getAttribute('data-cfs') || '';
        const hasDirectMatch = itemCfs.split(',').includes(cfsNum);

        let hasChildMatch = false;
        const replies = item.querySelectorAll('.reply-item');
        replies.forEach(r => {
          const rCfs = r.getAttribute('data-cfs') || '';
          if (rCfs.split(',').includes(cfsNum)) {
            hasChildMatch = true;
          }
        });

        if (hasDirectMatch || hasChildMatch) {
          item.style.display = '';
          if (hasChildMatch) {
            const toggleBtn = item.querySelector('.replies-toggle-btn');
            const container = item.querySelector('.replies-container');
            if (toggleBtn && container && container.classList.contains('hidden')) {
              container.classList.remove('hidden');
              toggleBtn.classList.add('open');
            }
          }
        } else {
          item.style.display = 'none';
        }
      });
    }

    // Render Post Detail and Comment Tree
    function renderPostDetail(post) {
      const fbLink = post.source_url ? `
        <a href="${escapeHtml(post.source_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-primary" style="font-size:12px; padding:6px 12px;">
          Mở trên Facebook ↗
        </a>
      ` : '';

      const tagsHtml = (post.tags || []).map(t => `<span class="tag-badge">${escapeHtml(t)}</span>`).join(' ');
      const timeHtml = post.published_text ? `<span style="font-size:13px; color:var(--text-dim);">${escapeHtml(post.published_text)}</span>` : '';

      const commentsCount = post.comments_tree ? post.comments_tree.length : 0;
      const totalCount = post.total_loaded_comments || 0;

      const confessions = post.confessions || [];
      let confessionsHtml = '';
      let filterBarHtml = '';
      if (confessions.length > 0) {
        confessionsHtml = `
          <div style="margin-top:14px; border-top:1px solid var(--border-subtle); padding-top:14px;">
            <div style="font-size:12px; font-weight:700; color:var(--accent); text-transform:uppercase; letter-spacing:0.5px; margin-bottom:8px;">
              📋 Danh sách Confessions trong bài (${confessions.length})
            </div>
            <div class="confessions-card-grid">
              ${confessions.map(cfs => `
                <div class="confession-card" onclick="filterCommentsByConfession('${escapeHtml(cfs.confession_number)}')">
                  <div class="confession-card-header">
                    <span>#${escapeHtml(cfs.confession_number)}</span>
                  </div>
                  <div class="confession-card-body">${escapeHtml(cfs.text || '')}</div>
                </div>
              `).join('')}
            </div>
          </div>
        `;

        filterBarHtml = `
          <div class="confession-filter-bar">
            <span style="font-size:12px; color:var(--text-dim); margin-right:4px;">Lọc theo confession:</span>
            <button class="filter-btn active" id="filter-cfs-all" onclick="filterCommentsByConfession('all')">Tất cả</button>
            ${confessions.map(cfs => `
              <button class="filter-btn" id="filter-cfs-${escapeHtml(cfs.confession_number)}" onclick="filterCommentsByConfession('${escapeHtml(cfs.confession_number)}')">#${escapeHtml(cfs.confession_number)}</button>
            `).join('')}
          </div>
        `;
      }

      let commentsHtml = '';
      if (!post.comments_tree || post.comments_tree.length === 0) {
        commentsHtml = `
          <div class="empty-state" style="padding: 30px;">
            <p>Bài viết này chưa có bình luận nào được lưu.</p>
          </div>
        `;
      } else {
        commentsHtml = `
          <div class="comments-thread-list">
            ${post.comments_tree.map(comment => renderCommentThread(comment)).join('')}
          </div>
        `;
      }

      detailContainer.innerHTML = `
        <button class="btn back-to-list-btn" onclick="backToList()">← Quay lại danh sách bài</button>

        <article class="post-box">
          <div class="post-meta-row">
            <div class="post-meta-left">
              ${tagsHtml}
              ${timeHtml}
            </div>
            ${fbLink}
          </div>

          <div class="post-body-text">${highlightTags(post.text || '(Bài viết không có chữ)')}</div>
          ${confessionsHtml}
        </article>

        <section class="comments-section">
          <div class="comments-header-row">
            <h3 class="comments-count-title">
              💬 Bình luận (${totalCount})
            </h3>
            ${post.ui_comment_count ? `<span style="font-size:12px; color:var(--text-dim);">${escapeHtml(post.ui_comment_count)} trên Facebook</span>` : ''}
          </div>
          ${filterBarHtml}
          ${commentsHtml}
        </section>
      `;
    }

    // Render a Single Comment + its Replies Accordion
    function renderCommentThread(comment) {
      const author = comment.author_name || 'Người dùng Facebook';
      const isAnon = !comment.author_name;
      const color = getAvatarColor(comment.author_name);
      const initials = getInitials(comment.author_name);
      const time = comment.published_text || '';
      const permalink = comment.permalink ? `
        <a href="${escapeHtml(comment.permalink)}" target="_blank" rel="noopener noreferrer" class="comment-link-btn" title="Mở comment này trên Facebook">🔗 Xem</a>
      ` : '';

      let bodyContent = '';
      if (comment.content_kind === 'media') {
        bodyContent = `<div class="media-badge">🖼️ [Bình luận bằng ảnh / Sticker / GIF]</div>`;
        if (comment.text) {
          bodyContent += `<div class="comment-text" style="margin-top:4px;">${escapeHtml(comment.text)}</div>`;
        }
      } else {
        bodyContent = `<div class="comment-text">${escapeHtml(comment.text || '')}</div>`;
      }

      const replies = comment.replies || [];
      const hasReplies = replies.length > 0;
      const toggleId = 'replies-' + (comment.source_id || Math.random().toString(36).slice(2));

      let repliesHtml = '';
      if (hasReplies) {
        repliesHtml = `
          <button class="replies-toggle-btn" onclick="toggleReplies('${toggleId}', this)">
            <span class="arrow">▶</span>
            <span class="toggle-text">Xem ${replies.length} câu trả lời</span>
          </button>

          <div class="replies-container hidden" id="${toggleId}">
            ${replies.map(reply => renderReplyItem(reply)).join('')}
          </div>
        `;
      }

      const cfsNums = ((comment.analysis && comment.analysis.status !== 'stale' && comment.analysis.targets) || []).map(t => t.confession_number).join(',');
      const analysisBadges = renderAnalysisBadges(comment.analysis);
      return `
        <div class="comment-item" data-cfs="${escapeHtml(cfsNums)}">
          <div class="comment-card">
            <div class="avatar-circle" style="background:${color};">${initials}</div>
            <div class="comment-bubble">
              <div class="comment-author-name ${isAnon ? 'anonymous' : ''}">
                ${escapeHtml(author)}
              </div>
              ${bodyContent}
              ${analysisBadges}
            </div>
          </div>

          <div class="comment-meta-footer">
            <span class="comment-time">${escapeHtml(time)}</span>
            ${permalink}
          </div>

          ${repliesHtml}
        </div>
      `;
    }

    function renderReplyItem(reply) {
      const author = reply.author_name || 'Người dùng Facebook';
      const isAnon = !reply.author_name;
      const color = getAvatarColor(reply.author_name);
      const initials = getInitials(reply.author_name);
      const time = reply.published_text || '';
      const permalink = reply.permalink ? `
        <a href="${escapeHtml(reply.permalink)}" target="_blank" rel="noopener noreferrer" class="comment-link-btn" title="Mở reply này trên Facebook">🔗</a>
      ` : '';

      let bodyContent = '';
      if (reply.content_kind === 'media') {
        bodyContent = `<div class="media-badge" style="font-size:11px; padding:3px 7px;">🖼️ [Ảnh/Sticker]</div>`;
        if (reply.text) {
          bodyContent += `<div class="comment-text" style="margin-top:3px;">${escapeHtml(reply.text)}</div>`;
        }
      } else {
        const parentTag = reply.parent_author_name ? `<span class="parent-tag">@${escapeHtml(reply.parent_author_name)}</span> ` : '';
        bodyContent = `<div class="comment-text">${parentTag}${escapeHtml(reply.text || '')}</div>`;
      }

      const replyCfsNums = ((reply.analysis && reply.analysis.status !== 'stale' && reply.analysis.targets) || []).map(t => t.confession_number).join(',');
      const replyAnalysisBadges = renderAnalysisBadges(reply.analysis);
      return `
        <div class="reply-item" data-cfs="${escapeHtml(replyCfsNums)}">
          <div class="reply-card">
            <div class="avatar-circle" style="background:${color};">${initials}</div>
            <div class="comment-bubble">
              <div class="comment-author-name ${isAnon ? 'anonymous' : ''}" style="font-size:12px;">
                ${escapeHtml(author)}
              </div>
              ${bodyContent}
              ${replyAnalysisBadges}
            </div>
          </div>
          <div class="comment-meta-footer" style="padding-left: 38px;">
            <span class="comment-time">${escapeHtml(time)}</span>
            ${permalink}
          </div>
        </div>
      `;
    }

    // Accordion Toggle for Replies
    function toggleReplies(id, btn) {
      const el = document.getElementById(id);
      if (!el) return;
      const isHidden = el.classList.contains('hidden');
      if (isHidden) {
        el.classList.remove('hidden');
        btn.classList.add('open');
        const count = el.querySelectorAll('.reply-item').length;
        btn.querySelector('.toggle-text').textContent = `Thu gọn ${count} câu trả lời`;
      } else {
        el.classList.add('hidden');
        btn.classList.remove('open');
        const count = el.querySelectorAll('.reply-item').length;
        btn.querySelector('.toggle-text').textContent = `Xem ${count} câu trả lời`;
      }
    }

    // Event Listeners
    let searchDebounce;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(loadPosts, 250);
    });

    sortSelect.addEventListener('change', loadPosts);

    refreshBtn.addEventListener('click', () => {
      postDetailCache = {};
      loadStats();
      loadPosts();
      if (selectedPostKey) selectPost(selectedPostKey);
    });

    // Keyboard Shortcuts
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && appBody.classList.contains('show-detail')) {
        backToList();
      }
    });

    // Initial Load
    loadStats();
    loadPosts();
  </script>
</body>
</html>
"""


def run_server(db_path: Path, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}. Run crawl or check --data-dir first.")

    resolved_path = db_path.resolve()

    class ConfiguredHandler(ViewerHandler):
        db_path = resolved_path
    server = ThreadingHTTPServer((host, port), ConfiguredHandler)
    url = f"http://{host}:{port}/"
    print(f"UIT Confession Viewer đang chạy tại: {url}")
    print("Nhấn Ctrl+C để dừng server.")

    if open_browser:
        import threading
        import webbrowser
        def _open():
            import time
            time.sleep(0.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng server.")
    finally:
        server.server_close()
