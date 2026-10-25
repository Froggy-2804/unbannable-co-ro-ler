"""Bounded, authenticated, read-only traversal of Facebook's visible UI."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

from .dom import CONTROL, SAFE_CONTROL, SCAN, SCROLL
from .models import CommentRecord, CrawlLimits, DiscoveryResult, LayoutChanged, PostRef, PostSnapshot, utc_now
from .urls import comment_identity, normalize_post_url

if TYPE_CHECKING:
    from .session import BrowserSession

_SEE_MORE = re.compile(r"^(?:See more|Xem thêm)$", re.IGNORECASE)
_ALL_COMMENTS = re.compile(r"^(?:All comments|Tất cả bình luận)$", re.IGNORECASE)
_SORT = re.compile(r"^(?:Most relevant|Newest|Most recent|Phù hợp nhất|Bình luận phù hợp nhất|Mới nhất|Bình luận mới nhất)$", re.IGNORECASE)
_EXPAND = re.compile(
    r"^(?:"
    r"(?:View|See|Load) (?:more|previous|older) comments?"
    r"|(?:View|See|Load) (?:(?:all )?\d[\d,.]* )?(?:all |more |previous |older )?repl(?:y|ies)"
    r"|(?:View|See|Load) (?:all )?\d[\d,.]* (?:more )?comments?"
    r"|(?:View|See|Load) all comments?"
    r"|\d[\d,.]* repl(?:y|ies)"
    r"|Xem (?:thêm |các )?(?:bình luận|câu trả lời|phản hồi)(?: trước| trước đó| cũ hơn| khác)?"
    r"|Xem (?:thêm |tất cả )?\d[\d.,]* (?:bình luận|câu trả lời|phản hồi)(?: khác)?"
    r"|\d[\d.,]* (?:câu trả lời|phản hồi)"
    r")$", re.IGNORECASE,
)
_POSSIBLE_EXPANSION = re.compile(
    r"^(?:View|See|Load|Xem)\b.*(?:comments?|repl(?:y|ies)|bình luận|câu trả lời|phản hồi)",
    re.IGNORECASE,
)
_UNAVAILABLE = re.compile(
    r"(?:This content isn't available(?: right now)?|This content is not available|"
    r"This page isn't available|Content not found|Bài viết này không còn tồn tại|"
    r"Nội dung này hiện không hiển thị|Nội dung này không còn tồn tại|"
    r"Trang này hiện không hiển thị)", re.IGNORECASE,
)


async def _scan(session: BrowserSession, **arguments: Any) -> dict[str, Any]:
    await session.check()
    try:
        result = await session.page.evaluate(SCAN, arguments)
    except PlaywrightError as exc:
        await session.check()
        raise LayoutChanged(f"Could not read the visible Facebook layout: {exc}") from exc
    await session.check()
    return result


async def _wait_for_post(session: BrowserSession, ref: PostRef, limits: CrawlLimits) -> None:
    """Wait for the SPA's targeted post, not just its initial HTML shell."""
    await session.check()
    try:
        await session.page.wait_for_function(
            "args => { const data = (" + SCAN + ")(args); return data.ready || "
            "(data.missing && new RegExp(args.unavailable, 'i').test(data.visibleText)); }",
            arg={"mode": "ready", "target": ref.url, "sourceId": ref.source_id,
                 "includeAuthors": False, "unavailable": _UNAVAILABLE.pattern},
            polling=200,
            timeout=limits.settle_timeout_ms,
        )
    except PlaywrightTimeout as exc:
        await session.check()
        raise LayoutChanged("The target post did not render a supported body before the UI timeout.") from exc
    await session.check()


async def _scroll(session: BrowserSession, *, discovery: bool) -> bool:
    await session.check()
    try:
        moved = await session.page.evaluate(SCROLL, discovery)
    except PlaywrightError as exc:
        await session.check()
        raise LayoutChanged(f"Could not scroll the visible Facebook layout: {exc}") from exc
    await session.settle()
    return bool(moved)


async def _click(session: BrowserSession, token: str) -> bool:
    await session.check()
    try:
        handle = await session.page.evaluate_handle(SAFE_CONTROL, token)
    except PlaywrightError:
        await session.check()
        return False
    try:
        element = handle.as_element()
        if element is None:
            await session.check()
            return False
        try:
            await element.click(timeout=5000)
        except PlaywrightError:
            await session.check()
            return False
        await session.settle()
        return True
    finally:
        await handle.dispose()


async def _hover_permalink(session: BrowserSession, token: str) -> str | None:
    """Read a lazy header link after normal hover; never click it."""
    await session.check()
    try:
        handle = await session.page.evaluate_handle(CONTROL, token)
    except PlaywrightError:
        await session.check()
        return None
    try:
        element = handle.as_element()
        if element is None:
            return None
        try:
            await element.hover(timeout=5000)
            await session.settle()
            return await element.get_attribute("href")
        except PlaywrightError:
            await session.check()
            return None
    finally:
        await handle.dispose()


async def _select_all_comments(session: BrowserSession) -> bool:
    """Only called immediately after opening this post's sort control."""
    await session.check()
    # The menu is portalled outside the article on Facebook. Do not search
    # arbitrary page buttons: a visible semantic menu/listbox is required.
    menus = session.page.locator('[role="menu"], [role="listbox"]')
    for menu_index in range(await menus.count()):
        menu = menus.nth(menu_index)
        if not await menu.is_visible():
            continue
        for role in ("menuitem", "menuitemradio", "option"):
            options = menu.get_by_role(role)
            for index in range(await options.count()):
                option = options.nth(index)
                if await option.is_visible():
                    # Accessible names include the explanatory paragraph in
                    # Facebook's menu. Match its visible heading, not that paragraph.
                    lines = (await option.inner_text()).strip().splitlines()
                    if not lines or not _ALL_COMMENTS.fullmatch(lines[0].strip()):
                        continue
                    await session.check()
                    if not await option.evaluate("node => !node.closest('a[href], [role=\"link\"]')"):
                        continue
                    try:
                        await option.click(timeout=5000)
                    except PlaywrightError:
                        await session.check()
                        return False
                    await session.settle()
                    return True
    await session.check()
    return False


async def discover_posts(
    session: BrowserSession,
    page_url: str,
    limits: CrawlLimits,
    on_post: Callable[[PostRef], None],
) -> DiscoveryResult:
    """Checkpoint Page-owned links, resolving lazy timestamp hrefs by hover."""
    await session.goto(page_url)
    result = DiscoveryResult(stop_reason="max_scrolls")
    seen: set[str] = set()
    hovered: set[str] = set()
    idle = 0
    previous_height: int | None = None
    saw_articles = False

    def capture(data: dict[str, Any]) -> None:
        for article in data["articles"]:
            for link in article["links"]:
                ref = normalize_post_url(link["url"], page_url)
                if ref is None:
                    continue
                if ref.key not in seen:
                    on_post(ref)
                    seen.add(ref.key)
                    result.found += 1
                break

    for round_number in range(limits.max_scrolls + 1):
        data = await _scan(session, mode="discover", target=None, sourceId=None, includeAuthors=False)
        saw_articles = saw_articles or bool(data["articles"])
        before = len(seen)
        capture(data)
        budget_exhausted = False
        for article in data["articles"]:
            if any(normalize_post_url(link["url"], page_url) for link in article["links"]):
                continue
            for link in article["links"]:
                if not link["lazy"] or link["token"] in hovered:
                    continue
                if len(hovered) >= limits.max_actions:
                    budget_exhausted = True
                    break
                hovered.add(link["token"])
                href = await _hover_permalink(session, link["token"])
                ref = normalize_post_url(href, page_url) if href else None
                if ref is not None and ref.key not in seen:
                    on_post(ref)
                    seen.add(ref.key)
                    result.found += 1
                # React can replace the hovered anchor or virtualize another
                # post. Read and checkpoint the current DOM before scrolling.
                data = await _scan(session, mode="discover", target=None, sourceId=None, includeAuthors=False)
                saw_articles = saw_articles or bool(data["articles"])
                capture(data)
                if ref is not None:
                    break
            if budget_exhausted:
                break
        if budget_exhausted:
            result.stop_reason = "max_permalink_hovers"
            break
        progressed = len(seen) > before or previous_height is None or data["height"] > previous_height
        previous_height = data["height"]
        idle = 0 if progressed else idle + 1
        if idle >= limits.idle_rounds:
            result.stop_reason = "no_progress"
            break
        if round_number == limits.max_scrolls:
            break
        await _scroll(session, discovery=True)
        result.scrolls += 1
    if not seen:
        detail = "no recognizable post permalink" if saw_articles else "no recognizable post article"
        raise LayoutChanged(f"Page discovery found {detail}; no empty successful crawl was recorded.")
    await session.check()
    return result


def _merge_comment(old: CommentRecord | None, new: CommentRecord) -> CommentRecord:
    if old is None:
        return new
    # Virtualization and collapsed re-renders must not erase previously expanded
    # text, dates, or an evidenced parent in this observation pass.
    use_new_text = new.text_expanded or (not old.text_expanded and len(new.text) >= len(old.text))
    return replace(
        new,
        text=new.text if use_new_text else old.text,
        text_expanded=new.text_expanded if use_new_text else old.text_expanded,
        content_kind=new.content_kind if use_new_text else old.content_kind,
        parent_source_id=new.parent_source_id or old.parent_source_id,
        permalink=new.permalink or old.permalink,
        published_text=new.published_text or old.published_text,
        published_at=new.published_at or old.published_at,
        author_name=new.author_name or old.author_name,
        observation_id=old.observation_id,
    )


class _Accumulator:
    def __init__(self, ref: PostRef, include_authors: bool):
        self.snapshot = PostSnapshot(ref=ref, text="")
        self.include_authors = include_authors
        self.prefix = uuid4().hex
        self.comments: dict[str, CommentRecord] = {}
        self.node_keys: dict[str, str] = {}
        self.last_signature: tuple[Any, ...] | None = None

    def collect(self, data: dict[str, Any]) -> bool:
        snapshot = self.snapshot
        if len(data["text"]) >= len(snapshot.text):
            snapshot.text = data["text"]
        snapshot.published_text = data.get("published_text") or snapshot.published_text
        snapshot.published_at = data.get("published_at") or snapshot.published_at
        snapshot.ui_comment_count = data.get("ui_comment_count") or snapshot.ui_comment_count
        identities: dict[str, tuple[str | None, str | None, str | None]] = {}
        for node in data["comments"]:
            identity: tuple[str | None, str | None, str | None] = (None, None, None)
            for link in node["links"]:
                candidate = comment_identity(link, snapshot.ref.url)
                if candidate[0]:
                    identity = candidate
                    break
            identities[node["node_id"]] = identity
        for node in data["comments"]:
            source_id, parent_id, permalink = identities[node["node_id"]]
            if parent_id is None and node.get("enclosing_node_id"):
                parent = identities.get(node["enclosing_node_id"])
                if parent:
                    parent_id = parent[0]
                else:
                    prior = self.comments.get(self.node_keys.get(node["enclosing_node_id"], ""))
                    parent_id = prior.source_id if prior else None
            observation_id = f"{self.prefix}:{node['node_id']}"
            key = f"id:{source_id}" if source_id else f"observation:{observation_id}"
            previous_key = self.node_keys.get(node["node_id"])
            old = self.comments.get(key)
            if source_id and previous_key and previous_key.startswith("observation:"):
                anonymous = self.comments.pop(previous_key, None)
                old = old or anonymous
            new = CommentRecord(
                text=node["text"], source_id=source_id, permalink=permalink,
                parent_source_id=parent_id if parent_id != source_id else None,
                published_text=node.get("published_text"), published_at=node.get("published_at"),
                author_name=node.get("author_name") if self.include_authors else None,
                observation_id=observation_id,
                text_expanded=node["text_expanded"],
                content_kind=node["content_kind"],
            )
            self.comments[key] = _merge_comment(old, new)
            self.node_keys[node["node_id"]] = key
        snapshot.comments = list(self.comments.values())
        signature = (
            snapshot.text, snapshot.published_text, snapshot.published_at,
            snapshot.ui_comment_count, snapshot.sort_order,
            tuple((key, value.text, value.content_kind, value.text_expanded, value.parent_source_id, value.published_text, value.published_at, value.author_name)
                  for key, value in self.comments.items()),
        )
        changed = signature != self.last_signature
        self.last_signature = signature
        return changed

    def checkpoint(self, callback: Callable[[PostSnapshot], None]) -> PostSnapshot:
        self.snapshot.observed_at = utc_now()
        # A callback can retain a snapshot without subsequent scans mutating it.
        result = replace(self.snapshot, comments=list(self.snapshot.comments), attachments=list(self.snapshot.attachments))
        callback(result)
        return result


def _positive_comment_count(value: str | None) -> bool:
    return bool(value and any(char in "123456789" for char in value))


async def extract_post(
    session: BrowserSession,
    ref: PostRef,
    limits: CrawlLimits,
    on_snapshot: Callable[[PostSnapshot], None],
    include_authors: bool = False,
) -> PostSnapshot:
    """Observe the targeted thread; 'observed' never asserts full coverage."""
    await session.goto(ref.url)
    await _wait_for_post(session, ref, limits)
    accumulator = _Accumulator(ref, include_authors)
    snapshot = accumulator.snapshot
    actions = 0
    idle = 0
    sort_attempted = False
    ever_had_body = False
    ever_had_media = False
    pending_action = False
    while True:
        data = await _scan(session, mode="extract", target=ref.url, sourceId=ref.source_id, includeAuthors=include_authors)
        if data.get("missing"):
            if _UNAVAILABLE.search(data.get("visibleText", "")):
                snapshot.status = "unavailable"
                snapshot.stop_reason = "content_unavailable_in_ui"
                return accumulator.checkpoint(on_snapshot)
            raise LayoutChanged("The target post/article could not be identified unambiguously in the visible UI.")
        ever_had_body = ever_had_body or data["has_body"]
        ever_had_media = ever_had_media or data["has_media"]
        controls = data["controls"]
        if any(_ALL_COMMENTS.fullmatch(control["label"]) for control in controls):
            snapshot.sort_order = "all_comments"
        expansions = [control for control in controls if
                      (_SEE_MORE.fullmatch(control["label"]) and control["text_expander"])
                      or _EXPAND.fullmatch(control["label"])]
        snapshot.post_expanded = not any(_SEE_MORE.fullmatch(control["label"]) and control["text_expander"] and not control["in_comment"] for control in controls)
        sort_control = next((control for control in controls if not control["text_expander"] and _SORT.fullmatch(control["label"])), None)
        unsupported = any(_POSSIBLE_EXPANSION.search(control["label"]) and not _EXPAND.fullmatch(control["label"]) for control in controls)
        changed = accumulator.collect(data)
        if changed:
            accumulator.checkpoint(on_snapshot)
        idle = 0 if changed else idle + 1
        if not expansions and (sort_control is None or sort_attempted or snapshot.sort_order == "all_comments"):
            if data["orphan_comments"] or (data["comments"] and all(not node["has_body"] for node in data["comments"])):
                snapshot.stop_reason = "unrecognized_comment_layout"
                accumulator.checkpoint(on_snapshot)
                raise LayoutChanged("Comment UI is present but comment bodies/containers could not be interpreted.")
            if not ever_had_body and not ever_had_media:
                snapshot.stop_reason = "unrecognized_post_body"
                accumulator.checkpoint(on_snapshot)
                raise LayoutChanged("Target article exists but no supported post body or attachment was found.")
            if not snapshot.comments and _positive_comment_count(snapshot.ui_comment_count):
                snapshot.stop_reason = "comments_indicated_but_not_visible"
                accumulator.checkpoint(on_snapshot)
                raise LayoutChanged("The UI indicates comments, but no supported comment article or expansion control is visible.")
        if unsupported:
            snapshot.stop_reason = "unrecognized_expansion_control"
            break
        if actions >= limits.max_actions:
            snapshot.stop_reason = "max_actions"
            break
        if idle >= limits.idle_rounds:
            snapshot.stop_reason = "no_progress_with_visible_controls" if expansions or pending_action else "visible_controls_exhausted"
            if not expansions and not pending_action:
                snapshot.status = "observed"
                snapshot.comments_expanded = True
            break
        if sort_control and not sort_attempted and snapshot.sort_order != "all_comments":
            snapshot.sort_order = sort_control["label"]
            sort_attempted = True
            if actions + 2 > limits.max_actions:
                snapshot.stop_reason = "max_actions_before_sort_selection"
                break
            actions += 1
            if not await _click(session, sort_control["token"]):
                snapshot.stop_reason = "sort_control_failed"
                break
            actions += 1
            if not await _select_all_comments(session):
                snapshot.stop_reason = "all_comments_option_unavailable"
                break
            snapshot.sort_order = "all_comments"
            pending_action = True
            continue
        if expansions:
            # Expand post text first, then each comment/reply loader in DOM order.
            enabled = [control for control in expansions if not control["disabled"]]
            if not enabled:
                actions += 1
                await session.settle()
                pending_action = True
                continue
            control = next((c for c in enabled if _SEE_MORE.fullmatch(c["label"]) and not c["in_comment"]), enabled[0])
            actions += 1
            if not await _click(session, control["token"]):
                snapshot.stop_reason = "expansion_failed"
                break
            pending_action = True
            continue
        pending_action = False
        actions += 1
        moved = await _scroll(session, discovery=False)
        pending_action = moved
    await session.check()
    return accumulator.checkpoint(on_snapshot)
