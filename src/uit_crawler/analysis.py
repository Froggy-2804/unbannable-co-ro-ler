"""Orchestration, context building, SHA-256 fingerprinting, lock, and evaluation for sentiment analysis."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .analysis_budget import check_context_budget, estimate_tokens
from .analysis_models import (
    ANALYSIS_OUTPUT_JSON_SCHEMA,
    BATCH_ANALYSIS_OUTPUT_JSON_SCHEMA,
    AncestorCommentItem,
    AnalysisInput,
    BatchAnalysisInput,
    BatchCommentTarget,
    ConfessionItem,
    TargetCommentItem,
    validate_analysis_output,
    validate_batch_analysis_output,
)
from .analysis_prompts import (
    PROMPT_VERSION,
    build_batch_chat_messages,
    build_chat_messages,
)

try:
    from .analysis_prompts import ANALYSIS_CONTRACT_HASH
except ImportError:
    ANALYSIS_CONTRACT_HASH = hashlib.sha256(PROMPT_VERSION.encode("utf-8")).hexdigest()

from .confessions import PARSER_VERSION, parse_confessions_full
from .ollama_client import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    OllamaAPIError,
    OllamaChatResult,
    OllamaClient,
    OllamaConnectionError,
    OllamaError,
    OllamaLengthExceededError,
    OllamaModelNotFoundError,
    OllamaTimeoutError,
)
from .storage import Store


@contextmanager
def exclusive_analysis(data_dir: Path):
    """Acquire single-worker lock on .analysis.lock in data_dir."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        data_dir.chmod(0o700)
    lock_file = (data_dir / ".analysis.lock").open("a+b")
    locked = False
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError(
                "Another analysis process is already running on this data directory."
            ) from exc
        yield
    finally:
        if locked:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def build_comment_context(
    post_key: str,
    comment_dict: dict[str, Any],
    all_comments: list[dict[str, Any]],
    confessions: list[dict[str, Any]],
) -> AnalysisInput:
    """Build the AnalysisInput object for a target comment with full ancestry and confessions."""
    by_key = {c["comment_key"]: c for c in all_comments}

    ancestors: list[AncestorCommentItem] = []
    curr_parent_key = comment_dict.get("parent_comment_key")
    visited = {comment_dict["comment_key"]}
    warnings: list[str] = []

    while curr_parent_key:
        if curr_parent_key in visited:
            warnings.append(f"Cycle detected at ancestor {curr_parent_key}")
            break
        visited.add(curr_parent_key)
        parent_c = by_key.get(curr_parent_key)
        if not parent_c:
            warnings.append(f"Missing ancestor comment: {curr_parent_key}")
            break
        if not bool(parent_c.get("content_expanded", 1)):
            warnings.append(f"Incomplete ancestor comment: {curr_parent_key}")
        ancestors.append(
            AncestorCommentItem(
                comment_key=parent_c["comment_key"],
                parent_comment_key=parent_c.get("parent_comment_key"),
                text=parent_c.get("text", ""),
            )
        )
        curr_parent_key = parent_c.get("parent_comment_key")

    ancestors.reverse()

    confession_items = [
        ConfessionItem(
            confession_key=c["confession_key"],
            confession_number=c["confession_number"],
            text=c["text"],
        )
        for c in confessions
    ]

    target_item = TargetCommentItem(
        comment_key=comment_dict["comment_key"],
        parent_comment_key=comment_dict.get("parent_comment_key"),
        text=comment_dict.get("text", ""),
    )

    return AnalysisInput(
        post_key=post_key,
        confessions=confession_items,
        ancestor_comments=ancestors,
        target_comment=target_item,
        context_warnings=warnings,
    )


def compute_input_hash(
    analysis_input: AnalysisInput,
    model: str,
    model_digest: str | None,
    prompt_version: str,
    options: dict[str, Any],
    think: str | None = None,
    contract_hash: str | None = None,
    target_content_expanded: bool = True,
    post_expanded: bool = True,
    post_content_expanded: bool | None = None,
) -> str:
    """Compute deterministic SHA-256 hash across all input content and generation options.

    Operational batch_size is excluded from generation options to avoid cache invalidation
    on batching differences. Think mode, contract hash, and source completeness are included.
    """
    clean_options = {k: v for k, v in options.items() if k != "batch_size"}
    effective_contract_hash = contract_hash or ANALYSIS_CONTRACT_HASH
    effective_post_content = post_content_expanded if post_content_expanded is not None else post_expanded

    canonical_dict = {
        "target": {
            "comment_key": analysis_input.target_comment.comment_key,
            "parent_comment_key": analysis_input.target_comment.parent_comment_key,
            "text": analysis_input.target_comment.text,
            "content_expanded": bool(target_content_expanded),
        },
        "ancestors": [
            {
                "comment_key": a.comment_key,
                "parent_comment_key": a.parent_comment_key,
                "text": a.text,
            }
            for a in analysis_input.ancestor_comments
        ],
        "context_warnings": sorted(analysis_input.context_warnings),
        "confessions": [
            {
                "confession_key": c.confession_key,
                "confession_number": c.confession_number,
                "text": c.text,
            }
            for c in analysis_input.confessions
        ],
        "post_content_expanded": bool(effective_post_content),
        "parser_version": PARSER_VERSION,
        "prompt_version": prompt_version,
        "contract_hash": effective_contract_hash,
        "think": think if think is not None else "",
        "model": model,
        "model_digest": model_digest or "",
        "options": clean_options,
    }
    serialized = json.dumps(canonical_dict, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _is_terminal_error(exc: BaseException) -> bool:
    """Identify terminal authentication, quota, or network connection failures that should stop the run."""
    if isinstance(exc, OllamaConnectionError):
        return True
    if isinstance(exc, OllamaAPIError) and exc.status_code in (401, 403, 429):
        return True
    status = getattr(exc, "status_code", None)
    if status in (401, 403, 429):
        return True
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) in (401, 403, 429):
        return True
    return False


def _process_comment_dict(
    out_dict: dict[str, Any],
    analysis_input: AnalysisInput,
) -> tuple[bool, str | None, str, str, list[dict[str, Any]], str | None]:
    """Validate parsed output dictionary and determine item status, scope, targets, and reason_code.

    Returns:
        (is_valid, validation_error, item_status, scope, targets, reason_code)
    """
    is_valid, val_err = validate_analysis_output(out_dict, analysis_input)
    if not is_valid:
        return (
            False,
            val_err,
            "error",
            "unresolved",
            [],
            None,
        )

    scope = out_dict.get("scope", "unresolved")
    review_required = bool(out_dict.get("review_required", False)) or (scope == "unresolved")
    reason_code = out_dict.get("reason_code")

    # Ancestor warnings indicate missing or incomplete context: cannot accept as unreviewed success
    if analysis_input.context_warnings:
        review_required = True
        if not reason_code:
            reason_code = "missing_context"
    elif scope == "unresolved" and not reason_code:
        reason_code = "missing_context"

    item_status = "needs_review" if review_required else "success"

    targets = out_dict.get("targets", [])
    cfs_num_by_key = {
        c.confession_key: c.confession_number for c in analysis_input.confessions
    }
    for t in targets:
        if "confession_number" not in t:
            t["confession_number"] = cfs_num_by_key.get(
                t.get("confession_key", ""),
                t.get("confession_key", "").split(":")[-1],
            )

    return True, None, item_status, scope, targets, reason_code


def _save_comment_result(store: Store, **kwargs) -> bool:
    """Save only when the source snapshot still matches; never bypass freshness."""
    return store.save_comment_analysis(**kwargs)


class AnalysisRunError(RuntimeError):
    """An inference run stopped after preflight; committed observations are retained."""


def run_analysis(
    store: Store,
    client: OllamaClient | None = None,
    model: str = DEFAULT_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    api_key: str | None = None,
    post_keys: list[str] | None = None,
    max_comments: int = 100,
    batch_size: int = 8,
    num_ctx: int = 16384,
    num_predict: int = 4096,
    think: str | None = "low",
    temperature: float = 0.0,
    timeout: float = 300.0,
    retry_failed: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute sentiment and confession linkage analysis over queued posts and comments.

    Dry-run mode parses posts and comments locally in memory without acquiring write locks
    or mutating the database, using read-only Store access.
    """
    clean_options = {
        "temperature": temperature,
        "num_ctx": num_ctx,
        "num_predict": num_predict,
    }

    own_client = False
    if client is None:
        client = OllamaClient(base_url=ollama_url, api_key=api_key, timeout=timeout)
        own_client = True

    model_digest: str | None = None
    if not dry_run:
        try:
            model_info = client.preflight(model=model)
            model_digest = model_info.digest
        except Exception:
            if own_client:
                client.close()
            raise
    else:
        try:
            model_info = client.preflight(model=model)
            model_digest = model_info.digest
        except Exception:
            model_digest = None

    try:
        # 1. Fetch target posts including completeness status. Convert rows to dicts to prevent AttributeError.
        if post_keys:
            placeholders = ",".join("?" for _ in post_keys)
            raw_post_rows = store._connection.execute(
                f"""SELECT post_key, text, post_expanded, content_expanded, status
                    FROM posts WHERE post_key IN ({placeholders})""",
                tuple(post_keys),
            ).fetchall()
        else:
            raw_post_rows = store._connection.execute(
                """SELECT post_key, text, post_expanded, content_expanded, status
                   FROM posts WHERE status != 'unavailable' ORDER BY post_key ASC"""
            ).fetchall()

        post_rows = [dict(r) for r in raw_post_rows]

        total_posts_considered = len(post_rows)
        skipped_posts = 0
        confession_posts = 0
        review_posts = 0

        # Elements: (post_key, comment_dict, analysis_input, input_hash, is_empty, is_incomplete_target, src_fp)
        work_items: list[tuple[str, dict[str, Any], AnalysisInput, str, bool, bool, str | None]] = []
        cached_count = 0
        skipped_empty_count = 0
        skipped_error_count = 0

        for p_row in post_rows:
            p_key = p_row["post_key"]
            connection = store._connection
            owns_snapshot = not connection.in_transaction
            if owns_snapshot:
                connection.execute("BEGIN")
            try:
                current_post = connection.execute(
                    "SELECT * FROM posts WHERE post_key = ?", (p_key,)
                ).fetchone()
                if current_post is None:
                    continue
                p_row = dict(current_post)
                all_comments = store.get_comments_for_post(p_key)
                src_fp = store.get_analysis_source_fingerprint(p_key)
            finally:
                if owns_snapshot:
                    connection.rollback()
            p_text = p_row.get("text") or ""
            p_status = p_row.get("status")

            if p_status == "unavailable":
                skipped_posts += 1
                continue

            # Gate authoritative stored post content_expanded only, not partial or UI post_expanded
            is_post_incomplete = not bool(p_row.get("content_expanded", 1))

            if dry_run:
                # Local in-memory parsing without ensuring or writing DB state
                parse_res = parse_confessions_full(p_text, is_incomplete=is_post_incomplete)
                if parse_res.status == "skipped_no_confession":
                    skipped_posts += 1
                    continue
                if parse_res.status == "needs_review":
                    review_posts += 1
                    continue
                confessions = [
                    {
                        "confession_key": f"{p_key}:{seg.confession_number}",
                        "confession_number": seg.confession_number,
                        "text": seg.text,
                    }
                    for seg in parse_res.segments
                ]
            else:
                confessions = store.ensure_post_confessions(
                    p_key, p_text, is_incomplete=is_post_incomplete, force=force
                )
                p_state = store.get_post_analysis_state(p_key)
                if not p_state:
                    continue
                if p_state["status"] == "skipped_no_confession":
                    skipped_posts += 1
                    continue
                if p_state["status"] == "needs_review":
                    review_posts += 1
                    continue

            confession_posts += 1


            for c_dict in all_comments:
                c_key = c_dict["comment_key"]
                c_text = c_dict.get("text", "")

                is_empty = (c_dict.get("content_kind") == "media") or not c_text.strip()
                is_incomplete_target = not bool(c_dict.get("content_expanded", 1))
                target_expanded = bool(c_dict.get("content_expanded", 1))
                post_content_exp = bool(p_row.get("content_expanded", 1))

                analysis_input = build_comment_context(
                    p_key, c_dict, all_comments, confessions
                )
                input_hash = compute_input_hash(
                    analysis_input,
                    model=model,
                    model_digest=model_digest,
                    prompt_version=PROMPT_VERSION,
                    options=clean_options,
                    think=think,
                    contract_hash=ANALYSIS_CONTRACT_HASH,
                    target_content_expanded=target_expanded,
                    post_content_expanded=post_content_exp,
                )

                existing = store.get_comment_analysis(p_key, c_key)
                # Stale analyses are never valid cache hits; missing model digest does not silently certify cache
                if existing and not force and existing.get("status") != "stale":
                    cached_digest = existing.get("model_digest")
                    can_certify_digest = bool(model_digest) and bool(cached_digest) and (cached_digest == model_digest)
                    if can_certify_digest and existing["input_hash"] == input_hash:
                        if existing["status"] in ("success", "needs_review", "skipped_empty"):
                            cached_count += 1
                            continue
                        if existing["status"] == "error" and not retry_failed:
                            skipped_error_count += 1
                            continue

                work_items.append(
                    (p_key, c_dict, analysis_input, input_hash, is_empty, is_incomplete_target, src_fp)
                )

        selected_items = (
            work_items
            if not max_comments or max_comments <= 0
            else work_items[:max_comments]
        )

        if dry_run:
            return {
                "dry_run": True,
                "total_posts_considered": total_posts_considered,
                "confession_posts": confession_posts,
                "skipped_posts": skipped_posts,
                "review_posts": review_posts,
                "queued_comments": len(work_items),
                "max_comments_budget": (
                    max_comments if max_comments and max_comments > 0 else "all"
                ),
                "would_attempt": len(selected_items),
                "cached_comments": cached_count,
                "skipped_empty_comments": skipped_empty_count,
                "skipped_error_comments": skipped_error_count,
                "stale_conflict": 0,
                "model": model,
                "model_digest": model_digest,
                "ollama_url": ollama_url,
            }

        # 2. Execute analysis run
        run_id = store.start_analysis_run(
            model=model,
            model_digest=model_digest,
            prompt_version=PROMPT_VERSION,
            options=clean_options,
        )

        counts = {
            "attempted": 0,
            "success": 0,
            "needs_review": 0,
            "skipped_empty": 0,
            "cached": cached_count,
            "error": 0,
            "stale_conflict": 0,
        }

        total_to_attempt = len(selected_items)
        eff_batch_size = max(1, batch_size)
        print(
            f"Bắt đầu phân tích {total_to_attempt} bình luận (model: {model}, batch_size: {eff_batch_size})...",
            flush=True,
        )

        try:
            # Step A: Request-free items (empty/media or truncated target comments)
            non_request_free_items: list[tuple[str, dict[str, Any], AnalysisInput, str, bool, bool, str | None]] = []
            for item in selected_items:
                p_key, c_dict, analysis_input, input_hash, is_empty, is_incomplete_target, src_fp = item
                c_key = c_dict["comment_key"]
                is_obs = bool(c_dict.get("is_observation", 0))

                if is_empty:
                    counts["attempted"] += 1
                    saved = _save_comment_result(
                        store,
                        post_key=p_key,
                        comment_key=c_key,
                        is_observation=is_obs,
                        scope="unresolved",
                        input_hash=input_hash,
                        status="skipped_empty",
                        model=model,
                        prompt_version=PROMPT_VERSION,
                        targets=[],
                        reason_code="incomplete_source",
                        model_digest=model_digest,
                        run_id=run_id,
                        source_fingerprint=src_fp,
                    )
                    if not saved:
                        counts["stale_conflict"] += 1
                    else:
                        counts["skipped_empty"] += 1
                    print(
                        f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: skipped_empty (ảnh/rỗng)",
                        flush=True,
                    )
                elif is_incomplete_target:
                    # Truncated source: gate inference request-free
                    counts["attempted"] += 1
                    saved = _save_comment_result(
                        store,
                        post_key=p_key,
                        comment_key=c_key,
                        is_observation=is_obs,
                        scope="unresolved",
                        input_hash=input_hash,
                        status="needs_review",
                        model=model,
                        prompt_version=PROMPT_VERSION,
                        targets=[],
                        reason_code="incomplete_source",
                        error_message="Target comment text truncated or incomplete",
                        model_digest=model_digest,
                        run_id=run_id,
                        source_fingerprint=src_fp,
                    )
                    if not saved:
                        counts["stale_conflict"] += 1
                    else:
                        counts["needs_review"] += 1
                    print(
                        f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: needs_review (incomplete_source)",
                        flush=True,
                    )
                else:
                    non_request_free_items.append(item)

            # Step B: Chunk non-empty comments by post_key up to eff_batch_size
            chunks: list[list[Any]] = []
            curr_post: str | None = None
            curr_chunk: list[Any] = []
            for item in non_request_free_items:
                p_key = item[0]
                if p_key != curr_post or len(curr_chunk) >= eff_batch_size:
                    if curr_chunk:
                        chunks.append(curr_chunk)
                    curr_chunk = [item]
                    curr_post = p_key
                else:
                    curr_chunk.append(item)
            if curr_chunk:
                chunks.append(curr_chunk)

            # Step C: Execute chunks
            for chunk in chunks:
                if len(chunk) == 1:
                    # Single comment execution: use retained snapshot source fingerprint
                    p_key, c_dict, analysis_input, input_hash, _, _, src_fp = chunk[0]
                    c_key = c_dict["comment_key"]
                    is_obs = bool(c_dict.get("is_observation", 0))

                    messages = build_chat_messages(analysis_input, few_shot=True)
                    fits, est_tokens, max_allowed = check_context_budget(
                        messages, num_ctx, num_predict, think
                    )
                    if not fits:
                        # Oversized single: classify needs_review/context_limit with no request
                        counts["attempted"] += 1
                        saved = _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=c_key,
                            is_observation=is_obs,
                            scope="unresolved",
                            input_hash=input_hash,
                            status="needs_review",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            reason_code="context_limit",
                            error_message=f"Context budget exceeded: {est_tokens} > {max_allowed}",
                            model_digest=model_digest,
                            run_id=run_id,
                            source_fingerprint=src_fp,
                        )
                        if not saved:
                            counts["stale_conflict"] += 1
                        else:
                            counts["needs_review"] += 1
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: needs_review (context_limit: {est_tokens} > {max_allowed})",
                            flush=True,
                        )
                        continue

                    counts["attempted"] += 1
                    try:
                        chat_result = client.chat(
                            model=model,
                            messages=messages,
                            schema=ANALYSIS_OUTPUT_JSON_SCHEMA,
                            think=think,
                            options=clean_options,
                            timeout=timeout,
                        )
                    except OllamaLengthExceededError:
                        counts["error"] += 1
                        _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=c_key,
                            is_observation=is_obs,
                            scope="unresolved",
                            input_hash=input_hash,
                            status="error",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            error_message="Output token limit exceeded",
                            reason_code="context_limit",
                            model_digest=model_digest,
                            run_id=run_id,
                            source_fingerprint=src_fp,
                        )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: error (context_limit)",
                            flush=True,
                        )
                        continue
                    except OllamaError as exc:
                        if _is_terminal_error(exc):
                            store.finish_analysis_run(
                                run_id=run_id, status="error", reason=str(exc), counts=counts
                            )
                            raise
                        counts["error"] += 1
                        _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=c_key,
                            is_observation=is_obs,
                            scope="unresolved",
                            input_hash=input_hash,
                            status="error",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            error_message=str(exc),
                            model_digest=model_digest,
                            run_id=run_id,
                            source_fingerprint=src_fp,
                        )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: error ({exc})",
                            flush=True,
                        )
                        continue

                    raw_text = chat_result.content
                    try:
                        parsed_json = json.loads(raw_text)
                    except json.JSONDecodeError as j_err:
                        counts["error"] += 1
                        _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=c_key,
                            is_observation=is_obs,
                            scope="unresolved",
                            input_hash=input_hash,
                            status="error",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            error_message=f"JSON parse error: {j_err}",
                            model_digest=model_digest,
                            raw_response=raw_text,
                            run_id=run_id,
                            source_fingerprint=src_fp,
                        )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: error (JSON parse error)",
                            flush=True,
                        )
                        continue

                    is_valid, val_err, item_status, scope, targets, reason_code = (
                        _process_comment_dict(parsed_json, analysis_input)
                    )
                    if not is_valid:
                        counts["error"] += 1
                        _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=c_key,
                            is_observation=is_obs,
                            scope=scope,
                            input_hash=input_hash,
                            status="error",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            error_message=f"Invariant validation error: {val_err}",
                            reason_code=reason_code,
                            model_digest=model_digest,
                            raw_response=raw_text,
                            run_id=run_id,
                            source_fingerprint=src_fp,
                        )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: error ({val_err})",
                            flush=True,
                        )
                        continue

                    saved = _save_comment_result(
                        store,
                        post_key=p_key,
                        comment_key=c_key,
                        is_observation=is_obs,
                        scope=scope,
                        input_hash=input_hash,
                        status=item_status,
                        model=model,
                        prompt_version=PROMPT_VERSION,
                        targets=targets,
                        reason_code=reason_code,
                        model_digest=model_digest,
                        raw_response=raw_text,
                        run_id=run_id,
                        source_fingerprint=src_fp,
                    )
                    if not saved:
                        counts["stale_conflict"] += 1
                        print(f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: stale_conflict", flush=True)
                        continue
                    else:
                        counts[item_status] += 1
                    targets_desc = (
                        ", ".join(
                            f"#{t['confession_number']}:{t.get('sentiment')}"
                            for t in targets
                        )
                        if targets
                        else scope
                    )
                    print(
                        f"  [{counts['attempted']}/{total_to_attempt}] {c_key}: {item_status} ({targets_desc})",
                        flush=True,
                    )

                else:
                    # Grouped batch mode: use retained snapshot source fingerprint
                    p_key = chunk[0][0]
                    confessions_list = chunk[0][2].confessions
                    batch_input = BatchAnalysisInput(
                        post_key=p_key,
                        confessions=confessions_list,
                        comments=[
                            BatchCommentTarget(
                                comment_key=item[1]["comment_key"],
                                parent_comment_key=item[1].get("parent_comment_key"),
                                text=item[1].get("text", ""),
                                ancestor_comments=item[2].ancestor_comments,
                                context_warnings=item[2].context_warnings,
                            )
                            for item in chunk
                        ],
                    )
                    messages = build_batch_chat_messages(batch_input, few_shot=True)
                    fits, est_tokens, max_allowed = check_context_budget(
                        messages, num_ctx, num_predict, think
                    )

                    # If batch exceeds budget, safely split down to singles
                    if not fits:
                        for item in chunk:
                            chunks.append([item])
                        continue

                    try:
                        chat_result = client.chat(
                            model=model,
                            messages=messages,
                            schema=BATCH_ANALYSIS_OUTPUT_JSON_SCHEMA,
                            think=think,
                            options=clean_options,
                            timeout=timeout,
                        )
                    except OllamaLengthExceededError:
                        for p_k, c_d, _, h_i, _, _, s_fp in chunk:
                            counts["attempted"] += 1
                            counts["error"] += 1
                            _save_comment_result(
                                store,
                                post_key=p_k,
                                comment_key=c_d["comment_key"],
                                is_observation=bool(c_d.get("is_observation", 0)),
                                scope="unresolved",
                                input_hash=h_i,
                                status="error",
                                model=model,
                                prompt_version=PROMPT_VERSION,
                                targets=[],
                                error_message="Output token limit exceeded",
                                reason_code="context_limit",
                                model_digest=model_digest,
                                run_id=run_id,
                                source_fingerprint=s_fp,
                            )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] batch {len(chunk)} comments: error (context_limit)",
                            flush=True,
                        )
                        continue
                    except OllamaError as exc:
                        if _is_terminal_error(exc):
                            store.finish_analysis_run(
                                run_id=run_id, status="error", reason=str(exc), counts=counts
                            )
                            raise
                        for p_k, c_d, _, h_i, _, _, s_fp in chunk:
                            counts["attempted"] += 1
                            counts["error"] += 1
                            _save_comment_result(
                                store,
                                post_key=p_k,
                                comment_key=c_d["comment_key"],
                                is_observation=bool(c_d.get("is_observation", 0)),
                                scope="unresolved",
                                input_hash=h_i,
                                status="error",
                                model=model,
                                prompt_version=PROMPT_VERSION,
                                targets=[],
                                error_message=str(exc),
                                model_digest=model_digest,
                                run_id=run_id,
                                source_fingerprint=s_fp,
                            )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] batch {len(chunk)} comments: error ({exc})",
                            flush=True,
                        )
                        continue

                    raw_text = chat_result.content
                    try:
                        parsed_batch = json.loads(raw_text)
                    except json.JSONDecodeError as j_err:
                        for p_k, c_d, _, h_i, _, _, s_fp in chunk:
                            counts["attempted"] += 1
                            counts["error"] += 1
                            _save_comment_result(
                                store,
                                post_key=p_k,
                                comment_key=c_d["comment_key"],
                                is_observation=bool(c_d.get("is_observation", 0)),
                                scope="unresolved",
                                input_hash=h_i,
                                status="error",
                                model=model,
                                prompt_version=PROMPT_VERSION,
                                targets=[],
                                error_message=f"JSON parse error: {j_err}",
                                model_digest=model_digest,
                                raw_response=raw_text,
                                run_id=run_id,
                                source_fingerprint=s_fp,
                            )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] batch {len(chunk)} comments: error (JSON parse error)",
                            flush=True,
                        )
                        continue

                    valid_by_key, errors_by_key = validate_batch_analysis_output(
                        parsed_batch, batch_input
                    )
                    item_map = {
                        item[1]["comment_key"]: (item[1], item[3], item[2], item[6])
                        for item in chunk
                    }

                    # Preserve valid batch outputs per item
                    for ck, valid_out in valid_by_key.items():
                        counts["attempted"] += 1
                        c_dict_item, hash_item, ana_inp, s_fp = item_map[ck]
                        is_valid, val_err, item_status, scope, targets, reason_code = (
                            _process_comment_dict(valid_out, ana_inp)
                        )
                        saved = _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=ck,
                            is_observation=bool(c_dict_item.get("is_observation", 0)),
                            scope=scope,
                            input_hash=hash_item,
                            status=item_status,
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=targets,
                            reason_code=reason_code,
                            model_digest=model_digest,
                            raw_response=raw_text,
                            run_id=run_id,
                            source_fingerprint=s_fp,
                        )
                        if not saved:
                            counts["stale_conflict"] += 1
                            print(f"  [{counts['attempted']}/{total_to_attempt}] {ck}: stale_conflict", flush=True)
                            continue
                        else:
                            counts[item_status] += 1
                        targets_desc = (
                            ", ".join(
                                f"#{t['confession_number']}:{t.get('sentiment')}"
                                for t in targets
                            )
                            if targets
                            else scope
                        )
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {ck}: {item_status} ({targets_desc})",
                            flush=True,
                        )

                    # Bounded single fallback for omitted items only; non-omitted errors recorded directly
                    for ck, err_msg in errors_by_key:
                        c_dict_item, hash_item, ana_inp, s_fp = item_map[ck]
                        actual_err = err_msg
                        reason_c = None

                        if err_msg == "Comment omitted from model response":
                            s_msgs = build_chat_messages(ana_inp, few_shot=True)
                            s_fits, s_est, s_max = check_context_budget(
                                s_msgs, num_ctx, num_predict, think
                            )
                            if not s_fits:
                                counts["attempted"] += 1
                                saved = _save_comment_result(
                                    store,
                                    post_key=p_key,
                                    comment_key=ck,
                                    is_observation=bool(c_dict_item.get("is_observation", 0)),
                                    scope="unresolved",
                                    input_hash=hash_item,
                                    status="needs_review",
                                    model=model,
                                    prompt_version=PROMPT_VERSION,
                                    targets=[],
                                    reason_code="context_limit",
                                    error_message=f"Context budget exceeded in fallback: {s_est} > {s_max}",
                                    model_digest=model_digest,
                                    run_id=run_id,
                                    source_fingerprint=s_fp,
                                )
                                if not saved:
                                    counts["stale_conflict"] += 1
                                else:
                                    counts["needs_review"] += 1
                                continue

                            try:
                                s_chat = client.chat(
                                    model=model,
                                    messages=s_msgs,
                                    schema=ANALYSIS_OUTPUT_JSON_SCHEMA,
                                    think=think,
                                    options=clean_options,
                                    timeout=timeout,
                                )
                                s_json = json.loads(s_chat.content)
                                is_v, val_e, s_status, s_scope, s_targets, s_rc = (
                                    _process_comment_dict(s_json, ana_inp)
                                )
                                if is_v:
                                    counts["attempted"] += 1
                                    saved = _save_comment_result(
                                        store,
                                        post_key=p_key,
                                        comment_key=ck,
                                        is_observation=bool(c_dict_item.get("is_observation", 0)),
                                        scope=s_scope,
                                        input_hash=hash_item,
                                        status=s_status,
                                        model=model,
                                        prompt_version=PROMPT_VERSION,
                                        targets=s_targets,
                                        reason_code=s_rc,
                                        model_digest=model_digest,
                                        raw_response=s_chat.content,
                                        run_id=run_id,
                                        source_fingerprint=s_fp,
                                    )
                                    if not saved:
                                        counts["stale_conflict"] += 1
                                        print(f"  [{counts['attempted']}/{total_to_attempt}] {ck}: stale_conflict", flush=True)
                                        continue
                                    else:
                                        counts[s_status] += 1
                                    t_desc = (
                                        ", ".join(
                                            f"#{t['confession_number']}:{t.get('sentiment')}"
                                            for t in s_targets
                                        )
                                        if s_targets
                                        else s_scope
                                    )
                                    print(
                                        f"  [{counts['attempted']}/{total_to_attempt}] {ck}: {s_status} (fallback 1-1: {t_desc})",
                                        flush=True,
                                    )
                                    continue
                                else:
                                    actual_err = f"Invariant validation error: {val_e}"
                            except OllamaLengthExceededError:
                                actual_err = "Output token limit exceeded"
                                reason_c = "context_limit"
                            except (OllamaError, json.JSONDecodeError) as fb_exc:
                                if _is_terminal_error(fb_exc):
                                    store.finish_analysis_run(
                                        run_id=run_id, status="error", reason=str(fb_exc), counts=counts
                                    )
                                    raise
                                actual_err = str(fb_exc)

                        counts["attempted"] += 1
                        counts["error"] += 1
                        saved = _save_comment_result(
                            store,
                            post_key=p_key,
                            comment_key=ck,
                            is_observation=bool(c_dict_item.get("is_observation", 0)),
                            scope="unresolved",
                            input_hash=hash_item,
                            status="error",
                            model=model,
                            prompt_version=PROMPT_VERSION,
                            targets=[],
                            error_message=actual_err,
                            reason_code=reason_c,
                            model_digest=model_digest,
                            raw_response=raw_text,
                            run_id=run_id,
                            source_fingerprint=s_fp,
                        )
                        if not saved:
                            counts["stale_conflict"] += 1
                        print(
                            f"  [{counts['attempted']}/{total_to_attempt}] {ck}: error ({actual_err})",
                            flush=True,
                        )

            store.finish_analysis_run(
                run_id=run_id,
                status="partial" if counts["error"] or counts["stale_conflict"] else "completed",
                reason=None,
                counts=counts,
            )
        except BaseException as b_exc:
            store.finish_analysis_run(
                run_id=run_id,
                status="interrupted" if isinstance(b_exc, KeyboardInterrupt) else "error",
                reason=str(b_exc),
                counts=counts,
            )
            if isinstance(b_exc, OllamaError):
                raise AnalysisRunError(str(b_exc)) from b_exc
            raise

        return {
            "dry_run": False,
            "run_id": run_id,
            "total_posts_considered": total_posts_considered,
            "confession_posts": confession_posts,
            "skipped_posts": skipped_posts,
            "review_posts": review_posts,
            **counts,
        }
    finally:
        if own_client:
            client.close()


def evaluate_gold(
    gold_path: Path | str,
    store: Store,
    output_path: Path | str | None = None,
    allow_unreviewed: bool = False,
) -> dict[str, Any]:
    """Evaluate stored sentiment predictions against a gold standard JSONL file.

    Default official evaluation scores accepted 'success' predictions only. Predictions marked
    needs_review, error, skipped_empty, stale, or missing are counted as false negatives in
    the end-to-end extraction denominator, and coverage is reported separately.
    Separates target-matched sentiment evaluation from linking and end-to-end tuple metrics.
    Prevents overwriting the gold dataset or existing report files.
    """
    gold_p = Path(gold_path).resolve()
    if not gold_p.exists():
        raise FileNotFoundError(f"Gold dataset not found: {gold_path}")

    if output_path is not None:
        out_p = Path(output_path).resolve()
        if out_p == gold_p:
            raise ValueError(f"Output path cannot be identical to gold path: {gold_path}")
        if out_p.exists():
            raise FileExistsError(f"Report output path already exists: {out_p}")

    gold_items: list[dict[str, Any]] = []
    with gold_p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                gold_items.append(json.loads(line))

    total = len(gold_items)
    accepted_success_predictions = 0
    missing_predictions = 0
    stale_predictions = 0
    review_predictions = 0
    error_predictions = 0
    skipped_empty_predictions = 0
    scope_matches = 0

    linking_tp = 0
    linking_fp = 0
    linking_fn = 0
    exact_target_set_matches = 0

    sentiment_tp = {"positive": 0, "negative": 0, "neutral": 0}
    sentiment_fp = {"positive": 0, "negative": 0, "neutral": 0}
    sentiment_fn = {"positive": 0, "negative": 0, "neutral": 0}

    tuple_tp = 0
    tuple_fp = 0
    tuple_fn = 0

    for item in gold_items:
        p_key = item["post_key"]
        c_key = item["comment_key"]
        expected_scope = item.get("scope", "confession")
        expected_targets = item.get("targets", [])

        pred = store.get_comment_analysis(p_key, c_key)
        pred_status = pred.get("status") if pred else None

        # Track category counts
        if pred is None:
            missing_predictions += 1
        elif pred_status == "stale":
            stale_predictions += 1
        elif pred_status == "needs_review":
            review_predictions += 1
        elif pred_status == "skipped_empty":
            skipped_empty_predictions += 1
        elif pred_status == "error":
            error_predictions += 1

        is_accepted = (pred_status == "success") or (allow_unreviewed and pred_status in ("success", "needs_review"))

        if not is_accepted or pred is None:
            # Unaccepted predictions count all gold tuples as false negatives in the end-to-end recall denominator
            for et in expected_targets:
                linking_fn += 1
                tuple_fn += 1
            continue

        accepted_success_predictions += 1

        pred_scope = pred.get("scope", "unresolved")
        if pred_scope == expected_scope:
            scope_matches += 1

        pred_targets = pred.get("targets", [])

        gold_cfs_nums = {str(t["confession_number"]) for t in expected_targets}
        pred_cfs_nums = {str(t["confession_number"]) for t in pred_targets}

        if gold_cfs_nums == pred_cfs_nums:
            exact_target_set_matches += 1

        for cfs in pred_cfs_nums:
            if cfs in gold_cfs_nums:
                linking_tp += 1
            else:
                linking_fp += 1
        for cfs in gold_cfs_nums:
            if cfs not in pred_cfs_nums:
                linking_fn += 1

        # Target-matched sentiment evaluation: evaluated only on intersection of predicted & gold targets
        common_cfs_nums = gold_cfs_nums & pred_cfs_nums
        gold_sent_by_cfs = {
            str(t["confession_number"]): t.get("sentiment") for t in expected_targets
        }
        pred_sent_by_cfs = {
            str(t["confession_number"]): t.get("sentiment") for t in pred_targets
        }
        for cfs in common_cfs_nums:
            g_s = gold_sent_by_cfs.get(cfs)
            p_s = pred_sent_by_cfs.get(cfs)
            if g_s == p_s:
                if p_s in sentiment_tp:
                    sentiment_tp[p_s] += 1
            else:
                if p_s in sentiment_fp:
                    sentiment_fp[p_s] += 1
                if g_s in sentiment_fn:
                    sentiment_fn[g_s] += 1

        # End-to-end tuple evaluation
        gold_tuples = {
            (str(t["confession_number"]), t.get("sentiment")) for t in expected_targets
        }
        pred_tuples = {
            (str(t["confession_number"]), t.get("sentiment")) for t in pred_targets
        }

        for tup in pred_tuples:
            if tup in gold_tuples:
                tuple_tp += 1
            else:
                tuple_fp += 1

        for tup in gold_tuples:
            if tup not in pred_tuples:
                tuple_fn += 1

    link_prec = (
        linking_tp / (linking_tp + linking_fp) if (linking_tp + linking_fp) > 0 else 0.0
    )
    link_rec = (
        linking_tp / (linking_tp + linking_fn) if (linking_tp + linking_fn) > 0 else 0.0
    )
    link_f1 = (
        (2 * link_prec * link_rec) / (link_prec + link_rec)
        if (link_prec + link_rec) > 0
        else 0.0
    )
    exact_set_acc = exact_target_set_matches / total if total > 0 else 0.0

    tup_prec = (
        tuple_tp / (tuple_tp + tuple_fp) if (tuple_tp + tuple_fp) > 0 else 0.0
    )
    tup_rec = (
        tuple_tp / (tuple_tp + tuple_fn) if (tuple_tp + tuple_fn) > 0 else 0.0
    )
    tup_f1 = (
        (2 * tup_prec * tup_rec) / (tup_prec + tup_rec)
        if (tup_prec + tup_rec) > 0
        else 0.0
    )

    per_class_sentiment: dict[str, dict[str, float]] = {}
    f1_list = []
    for cls in ("positive", "negative", "neutral"):
        tp = sentiment_tp[cls]
        fp = sentiment_fp[cls]
        fn = sentiment_fn[cls]
        p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * p * r) / (p + r) if (p + r) > 0 else 0.0
        per_class_sentiment[cls] = {
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f1, 4),
        }
        f1_list.append(f1)

    macro_f1 = sum(f1_list) / len(f1_list) if f1_list else 0.0
    scope_acc = scope_matches / total if total > 0 else 0.0
    success_rate = accepted_success_predictions / total if total > 0 else 0.0
    overall_coverage = (
        (accepted_success_predictions + review_predictions) / total if total > 0 else 0.0
    )

    report = {
        "gold_samples": total,
        "accepted_success_predictions": accepted_success_predictions,
        "matched_predictions": accepted_success_predictions,
        "review_predictions": review_predictions,
        "error_predictions": error_predictions,
        "skipped_empty_predictions": skipped_empty_predictions,
        "stale_predictions": stale_predictions,
        "missing_predictions": missing_predictions,
        "review_rate": round(review_predictions / total, 4) if total > 0 else 0.0,
        "coverage": {
            "success_rate": round(success_rate, 4),
            "overall_coverage": round(overall_coverage, 4),
            "review_rate": round(review_predictions / total, 4) if total > 0 else 0.0,
            "error_rate": round(error_predictions / total, 4) if total > 0 else 0.0,
            "missing_rate": round(missing_predictions / total, 4) if total > 0 else 0.0,
            "stale_rate": round(stale_predictions / total, 4) if total > 0 else 0.0,
        },
        "scope_accuracy": round(scope_acc, 4),
        "linking": {
            "precision": round(link_prec, 4),
            "recall": round(link_rec, 4),
            "f1": round(link_f1, 4),
            "exact_set_accuracy": round(exact_set_acc, 4),
        },
        "sentiment_when_target_matched": {
            "macro_f1": round(macro_f1, 4),
            "per_class": per_class_sentiment,
        },
        "end_to_end_tuple": {
            "precision": round(tup_prec, 4),
            "recall": round(tup_rec, 4),
            "f1": round(tup_f1, 4),
        },
    }

    if output_path is not None:
        out_p = Path(output_path).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    return report
