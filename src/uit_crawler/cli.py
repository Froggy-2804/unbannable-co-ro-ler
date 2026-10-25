"""Command-line workflows for manual login, durable crawling, and export."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from playwright.async_api import Error as PlaywrightError

from .facebook import discover_posts, extract_post
from .models import CrawlLimits, DiscoveryResult, PostRef, StopCrawl
from .runtime import clear_pause, exclusive_run, load_env_file, read_pause, save_pause
from .session import open_session
from .storage import Store
from .urls import DEFAULT_PAGE, normalize_page_url, normalize_post_url


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, nonnegative number")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uit-crawler",
        description="Read Facebook's visible UI with a manually logged-in browser. No ban or completeness guarantee.",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Private state directory; default: ./data (place before command)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login = subparsers.add_parser("login", help="Open the dedicated browser and log in manually")
    login.add_argument("--page", default=DEFAULT_PAGE)
    login.add_argument("--timeout", type=_positive_int, default=600, help="Seconds to allow for manual login")
    login.add_argument("--channel", choices=("chrome", "msedge"), help="Use installed Chrome/Edge instead of Playwright Chromium")

    crawl = subparsers.add_parser("crawl", help="Discover posts and process a bounded, resumable batch")
    crawl.add_argument("--page", default=DEFAULT_PAGE)
    crawl.add_argument("--post", action="append", default=[], metavar="URL", help="Explicit Page post to refresh; repeatable, skips feed discovery")
    crawl.add_argument("--skip-discovery", action="store_true", help="Process the saved queue without scrolling the Page")
    crawl.add_argument("--max-posts", type=_positive_int, default=20)
    crawl.add_argument("--max-scrolls", type=_nonnegative_int, default=40)
    crawl.add_argument("--max-actions", type=_positive_int, default=200, help="Maximum permalink hovers during discovery and expansion/scroll actions per post")
    crawl.add_argument("--idle-rounds", type=_positive_int, default=3)
    crawl.add_argument("--action-delay", type=_nonnegative_float, default=2.0, help="Minimum action interval in seconds, NOT a safe/anti-ban threshold")
    crawl.add_argument("--revisit-hours", type=_nonnegative_float, default=24.0, help="Revisit previously observed posts after this age; pending jobs go first")
    crawl.add_argument("--retry-failed", action="store_true", help="Include previously failed jobs after manual review")
    crawl.add_argument("--include-authors", action="store_true", default=True, help="Store visible comment author names (default: True)")
    crawl.add_argument("--no-authors", dest="include_authors", action="store_false", help="Do not store comment author names")
    crawl.add_argument("--channel", choices=("chrome", "msedge"))

    status = subparsers.add_parser("status", help="Show saved queue, coverage, and pause state")
    status.add_argument("--page", help="Filter by Page; omitted means all saved Pages")

    export = subparsers.add_parser("export", help="Export stored observations without opening Facebook")
    export.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    export.add_argument("--output", type=Path, required=True, help="New JSONL file or new/empty CSV directory")
    export.add_argument("--page", help="Filter by Page; omitted means all saved Pages")
    export.add_argument("--include-analysis", action="store_true", help="Include confession and sentiment analysis records in export")

    analyze = subparsers.add_parser("analyze", help="Classify comment sentiment using local Ollama model")
    analyze.add_argument("--model", default="gpt-oss:20b", help="Model name in local or cloud Ollama (default: gpt-oss:20b)")
    analyze.add_argument("--ollama-url", default=None, help="Ollama base URL (default: http://127.0.0.1:11434, or https://ollama.com with --cloud)")
    analyze.add_argument("--cloud", action="store_true", help="Use hosted Ollama Cloud (https://ollama.com) instead of local daemon")
    analyze.add_argument("--api-key", help="Ollama Cloud API key (or set OLLAMA_API_KEY environment variable)")
    analyze.add_argument("--max-comments", type=_nonnegative_int, default=100, help="Max comments to attempt in this batch (0 = tất cả, default: 100)")
    analyze.add_argument("--all", dest="all_comments", action="store_true", help="Chạy toàn bộ comment còn lại trong hàng đợi")
    analyze.add_argument("--batch-size", type=_positive_int, default=8, help="Số comment gom vào mỗi request gọi LLM (default: 8, đặt 1 để chạy từng comment riêng)")
    analyze.add_argument("--post-key", action="append", default=[], help="Specific post key to analyze (repeatable)")
    analyze.add_argument("--num-ctx", type=_positive_int, default=16384, help="Context token limit (default: 16384)")
    analyze.add_argument("--num-predict", type=_positive_int, default=4096, help="Output token limit (default: 4096)")
    analyze.add_argument("--think", choices=("low", "medium", "high", "none"), default="low", help="Thinking level (default: low)")
    analyze.add_argument("--timeout", type=_positive_int, default=300, help="Per-request timeout in seconds (default: 300)")
    analyze.add_argument("--retry-failed", action="store_true", help="Retry previously failed analyses")
    analyze.add_argument("--force", action="store_true", help="Force re-analyzing even current successful analyses")
    analyze.add_argument("--dry-run", action="store_true", help="Inspect queued work and model availability without inference or DB writes")

    analysis_status = subparsers.add_parser("analysis-status", help="Show sentiment analysis coverage and target sentiment breakdown")

    analysis_eval = subparsers.add_parser("analysis-evaluate", help="Evaluate sentiment predictions against a gold standard JSONL")
    analysis_eval.add_argument("--gold", type=Path, required=True, help="Path to gold JSONL file")
    analysis_eval.add_argument("--output", type=Path, help="Path to write evaluation JSON report")
    ui = subparsers.add_parser("ui", help="Open local web UI to browse crawled posts and comments")
    ui.add_argument("--port", type=_positive_int, default=8765, help="Port to bind (default: 8765)")
    ui.add_argument("--host", default="127.0.0.1", help="Host interface to bind (default: 127.0.0.1)")
    ui.add_argument("--no-browser", action="store_true", help="Do not automatically open default web browser")
    reset = subparsers.add_parser("reset", help="Delete crawled database to start crawling again from scratch")
    reset.add_argument("--all", action="store_true", help="Also delete browser profile (requires logging in to Facebook again)")
    reset.add_argument("-y", "--yes", action="store_true", help="Confirm deletion without prompt")
    return parser


def _print_json(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _safe_error(exc: BaseException, secret: str | None = None) -> str:
    text = str(exc)
    if secret:
        text = text.replace(secret, "[REDACTED]")
    env_secret = os.environ.get("OLLAMA_API_KEY")
    if env_secret:
        text = text.replace(env_secret, "[REDACTED]")
    text = re.sub(r"(?i)(access_token|password|cookie|authorization|api[_-]?key|secret|token)=([^\s&]+)", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)bearer\s+([^\s,;]+)", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(authorization|api[_-]?key):\s*([^\r\n]+)", r"\1: [REDACTED]", text)
    return text

async def _login(args, data_dir: Path) -> int:
    print("Log in manually in the dedicated browser. Do not send passwords or cookies to anyone.", flush=True)
    print("Only an authenticated page with no active security prompt clears a saved pause.", flush=True)
    try:
        async with open_session(data_dir / "browser-profile", channel=args.channel) as session:
            await session.manual_login(args.page, args.timeout)
        clear_pause(data_dir)
        print("Browser session saved locally. You may now run crawl.")
        return 0
    except StopCrawl as exc:
        save_pause(data_dir, exc.reason)
        print(str(exc), file=sys.stderr)
        return 2


async def _crawl(args, data_dir: Path, store: Store) -> int:
    paused = read_pause(data_dir)
    if paused:
        print(f"Crawling is paused ({paused.get('reason', 'unknown')}). Run login and inspect Facebook manually first.", file=sys.stderr)
        return 2
    recovered = store.recover_jobs()
    if recovered:
        print(f"Recovered {recovered} interrupted job(s).", flush=True)
    post_limits = CrawlLimits(
        max_scrolls=args.max_scrolls,
        max_actions=args.max_actions,
        idle_rounds=args.idle_rounds,
        action_delay=args.action_delay,
    )
    discovery_limits = CrawlLimits(
        max_scrolls=args.max_scrolls,
        max_actions=max(args.max_actions, args.max_posts * 3),
        idle_rounds=args.idle_rounds,
        action_delay=args.action_delay,
    )
    explicit: list[PostRef] = []
    seen = set()
    for value in args.post:
        ref = normalize_post_url(value, args.page)
        if ref is None:
            raise ValueError(f"Not a supported post URL for this Page: {value}")
        if ref.key not in seen:
            explicit.append(ref)
            seen.add(ref.key)
    run_id = store.start_run(args.page)
    discovery = DiscoveryResult(stop_reason="skipped")
    active: PostRef | None = None
    processed = 0
    incomplete = 0
    try:
        async with open_session(data_dir / "browser-profile", action_delay=args.action_delay, channel=args.channel) as session:
            # Check the saved login before enqueueing work or scrolling the feed.
            await session.goto(args.page)
            if explicit:
                for ref in explicit:
                    store.enqueue(ref, args.page)
                jobs = explicit[: args.max_posts]
            else:
                if not args.skip_discovery and args.max_scrolls:
                    discovery = await discover_posts(session, args.page, discovery_limits, lambda ref: store.enqueue(ref, args.page))
                    print(f"Discovery stopped: {discovery.stop_reason}; observed {discovery.found} post links.", flush=True)
                jobs = store.select_jobs(args.page, args.max_posts, args.revisit_hours, args.retry_failed)
            for ref in jobs:
                active = ref
                store.claim(ref, run_id)
                print(f"Reading post {ref.key}", flush=True)
                snapshot = await extract_post(session, ref, post_limits, lambda value: store.save_snapshot(run_id, value), include_authors=args.include_authors)
                store.save_snapshot(run_id, snapshot)
                store.finish_job(ref, snapshot.status, snapshot.stop_reason)
                processed += 1
                incomplete += snapshot.status != "observed"
                print(f"  {snapshot.status}: {len(snapshot.comments)} comment observations; {snapshot.stop_reason}", flush=True)
                active = None
        store.finish_run(run_id, "partial" if incomplete else "finished", f"processed={processed}; incomplete={incomplete}; visible UI observations only", discovery)
        _print_json({"run_id": run_id, "processed": processed, "incomplete_posts": incomplete, "coverage": "visible_ui_only_not_full_facebook_history", "state": store.status(args.page)})
        return 3 if incomplete else 0
    except StopCrawl as exc:
        if active:
            store.finish_job(active, "pending", exc.reason)
        store.finish_run(run_id, "paused", exc.reason, discovery)
        save_pause(data_dir, exc.reason)
        print(str(exc), file=sys.stderr)
        return 2
    except asyncio.CancelledError:
        if active:
            store.finish_job(active, "pending", "interrupted")
        store.finish_run(run_id, "interrupted", "user_interrupt", discovery)
        raise
    except Exception as exc:
        if active:
            store.finish_job(active, "error", type(exc).__name__)
        store.finish_run(run_id, "failed", type(exc).__name__, discovery)
        raise


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    load_env_file(data_dir / ".env")
    try:
        if getattr(args, "page", None):
            args.page = normalize_page_url(args.page)
        if args.command == "ui":
            from .server import run_server
            db_path = data_dir / "crawler.sqlite3"
            run_server(db_path, host=args.host, port=args.port, open_browser=not args.no_browser)
            return 0
        if args.command == "reset":
            with exclusive_run(data_dir):
                if not args.yes and sys.stdin.isatty():
                    prompt = "Bạn có chắc muốn xóa TOÀN BỘ dữ liệu và đăng nhập lại? [y/N]: " if args.all else "Bạn có chắc muốn xóa database bài đã crawl (giữ lại đăng nhập Facebook)? [y/N]: "
                    choice = input(prompt).strip().lower()
                    if choice not in ("y", "yes"):
                        print("Đã hủy thao tác.")
                        return 0
                import shutil
                for suffix in ("", "-wal", "-shm"):
                    (data_dir / f"crawler.sqlite3{suffix}").unlink(missing_ok=True)
                (data_dir / "paused.json").unlink(missing_ok=True)
                if args.all:
                    shutil.rmtree(data_dir / "browser-profile", ignore_errors=True)
                target = "toàn bộ database và phiên đăng nhập" if args.all else "database bài viết/bình luận (phiên đăng nhập Facebook được giữ lại)"
                print(f"Đã xóa thành công {target}. Bạn có thể bắt đầu crawl lại.")
                return 0
        if args.command in {"login", "crawl"}:
            with exclusive_run(data_dir):
                if args.command == "login":
                    return asyncio.run(_login(args, data_dir))
                with Store(data_dir / "crawler.sqlite3") as store:
                    return asyncio.run(_crawl(args, data_dir, store))
        if args.command == "analysis-status":
            with Store(data_dir / "crawler.sqlite3") as store:
                _print_json(store.get_analysis_stats())
            return 0
        if args.command == "analysis-evaluate":
            from .analysis import evaluate_gold
            with Store(data_dir / "crawler.sqlite3") as store:
                report = evaluate_gold(args.gold, store, output_path=args.output)
                _print_json(report)
            return 0
        if args.command == "analyze":
            from .analysis import exclusive_analysis, run_analysis
            from .ollama_client import (
                DEFAULT_CLOUD_OLLAMA_URL,
                DEFAULT_OLLAMA_URL,
                OllamaAPIError,
                OllamaConnectionError,
                OllamaError,
                OllamaModelNotFoundError,
                OllamaTimeoutError,
                is_cloud_origin,
                is_loopback_url,
            )
            think_val = None if args.think == "none" else args.think

            # Config error if --cloud is combined with custom non-Cloud URL
            if args.cloud and args.ollama_url is not None and not is_cloud_origin(args.ollama_url):
                print(
                    f"Lỗi cấu hình: --cloud không thể kết hợp với URL không phải Ollama Cloud ({args.ollama_url}).",
                    file=sys.stderr,
                )
                return 2

            if args.cloud:
                ollama_url = args.ollama_url or DEFAULT_CLOUD_OLLAMA_URL
            else:
                ollama_url = args.ollama_url or DEFAULT_OLLAMA_URL

            # Explicit --api-key for custom endpoint requires HTTPS unless loopback
            if args.api_key:
                parsed_url = urllib.parse.urlsplit(ollama_url)
                if parsed_url.scheme.lower() != "https" and not is_loopback_url(ollama_url):
                    print(
                        "Lỗi cấu hình: Truyền --api-key cho endpoint từ xa yêu cầu giao thức HTTPS (chỉ cho phép HTTP đối với loopback/localhost).",
                        file=sys.stderr,
                    )
                    return 2

            # Ambient key is only retrieved for exact trusted Cloud origin
            api_key = args.api_key
            if not api_key and is_cloud_origin(ollama_url):
                api_key = os.environ.get("OLLAMA_API_KEY")

            if (args.cloud or is_cloud_origin(ollama_url)) and not api_key:
                print(
                    "Lỗi: Ollama Cloud yêu cầu API key. Vui lòng truyền --api-key <key> hoặc đặt biến môi trường OLLAMA_API_KEY.",
                    file=sys.stderr,
                )
                return 2

            max_comments = 0 if args.all_comments else args.max_comments
            post_keys = args.post_key or None

            if args.dry_run:
                db_path = data_dir / "crawler.sqlite3"
                if not db_path.exists():
                    print(f"Lỗi: Không tìm thấy file database tại {db_path}", file=sys.stderr)
                    return 1
                try:
                    with Store(db_path, read_only=True) as store:
                        res = run_analysis(
                            store=store,
                            model=args.model,
                            ollama_url=ollama_url,
                            api_key=api_key,
                            post_keys=post_keys,
                            max_comments=max_comments,
                            batch_size=args.batch_size,
                            num_ctx=args.num_ctx,
                            num_predict=args.num_predict,
                            think=think_val,
                            timeout=float(args.timeout),
                            retry_failed=args.retry_failed,
                            force=args.force,
                            dry_run=True,
                        )
                        _print_json(res)
                    return 0
                except (OllamaConnectionError, OllamaModelNotFoundError, OllamaTimeoutError, OllamaAPIError) as pre_err:
                    print(f"Preflight error: {_safe_error(pre_err, api_key)}", file=sys.stderr)
                    return 2

            with exclusive_analysis(data_dir):
                with Store(data_dir / "crawler.sqlite3") as store:
                    try:
                        res = run_analysis(
                            store=store,
                            model=args.model,
                            ollama_url=ollama_url,
                            api_key=api_key,
                            post_keys=post_keys,
                            max_comments=max_comments,
                            batch_size=args.batch_size,
                            num_ctx=args.num_ctx,
                            num_predict=args.num_predict,
                            think=think_val,
                            timeout=float(args.timeout),
                            retry_failed=args.retry_failed,
                            force=args.force,
                            dry_run=False,
                        )
                        _print_json(res)
                        if res.get("error", 0) > 0:
                            return 1
                        if res.get("needs_review", 0) > 0 or res.get("stale_conflict", 0) > 0:
                            return 3
                        return 0
                    except (OllamaConnectionError, OllamaModelNotFoundError, OllamaTimeoutError, OllamaAPIError) as pre_err:
                        print(f"Preflight error: {_safe_error(pre_err, api_key)}", file=sys.stderr)
                        return 2
        with Store(data_dir / "crawler.sqlite3") as store:
            if args.command == "status":
                _print_json({**store.status(args.page), "pause": read_pause(data_dir)})
            else:
                output = args.output.expanduser().resolve()
                if output.exists() and (output.is_file() or any(output.iterdir())):
                    raise ValueError("Export destination already exists and is not empty. Choose a new path; existing files are not overwritten.")
                counts = store.export(
                    output,
                    args.format,
                    args.page,
                    include_analysis=getattr(args, "include_analysis", False),
                )
                _print_json({"output": str(output), "format": args.format, "records": counts})
        return 0
    except KeyboardInterrupt:
        print("Stopped. Saved observations remain available; interrupted jobs resume on the next crawl.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, sqlite3.Error, PlaywrightError) as exc:
        secret = getattr(args, "api_key", None) if "args" in locals() and args is not None else None
        print(f"{type(exc).__name__}: {_safe_error(exc, secret)}", file=sys.stderr)
        return 1
    except Exception as exc:
        secret = getattr(args, "api_key", None) if "args" in locals() and args is not None else None
        print(f"{type(exc).__name__}: {_safe_error(exc, secret)}", file=sys.stderr)
        return 1
