"""Records shared by the browser, durable queue, and exporters."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class PostRef:
    url: str
    source_id: str | None = None

    @property
    def key(self) -> str:
        return self.source_id or self.url


@dataclass(slots=True)
class CommentRecord:
    text: str
    source_id: str | None = None
    permalink: str | None = None
    parent_source_id: str | None = None
    published_text: str | None = None
    published_at: str | None = None
    author_name: str | None = None
    # Local observation identity, never presented as a Facebook comment ID.
    observation_id: str = field(default_factory=lambda: uuid4().hex)
    text_expanded: bool = False
    content_kind: str = "text"


@dataclass(slots=True)
class PostSnapshot:
    ref: PostRef
    text: str
    comments: list[CommentRecord] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    published_text: str | None = None
    published_at: str | None = None
    observed_at: str = field(default_factory=utc_now)
    status: str = "partial"
    stop_reason: str = "in_progress"
    sort_order: str | None = None
    post_expanded: bool = False
    comments_expanded: bool = False
    ui_comment_count: str | None = None


@dataclass(slots=True)
class DiscoveryResult:
    found: int = 0
    scrolls: int = 0
    stop_reason: str = "not_started"


@dataclass(frozen=True, slots=True)
class CrawlLimits:
    max_scrolls: int = 40
    max_actions: int = 200
    idle_rounds: int = 3
    action_delay: float = 2.0
    settle_timeout_ms: int = 15000


class StopCrawl(RuntimeError):
    """An authentication/security signal that must stop the whole run."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class LayoutChanged(RuntimeError):
    """The visible page could not be interpreted without guessing."""
