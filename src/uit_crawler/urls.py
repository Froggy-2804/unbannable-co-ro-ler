"""Normalize visible Facebook links without using private APIs."""

from __future__ import annotations

import base64
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from .models import PostRef

DEFAULT_PAGE = "https://www.facebook.com/UITconfess/"
DEFAULT_PAGE_ID = "100088809545601"
_FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
_RESERVED_PAGES = {"login", "checkpoint", "groups", "watch", "reel", "reels", "photo", "photos", "share", "marketplace", "settings"}


def is_facebook_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname in _FACEBOOK_HOSTS and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except ValueError:
        return False


def normalize_page_url(value: str) -> str:
    value = value.strip()
    if not is_facebook_url(value):
        raise ValueError("Page URL must use https://www.facebook.com/ (no credentials or custom port).")
    parsed = urlsplit(value)
    parts = [part for part in parsed.path.split("/") if part]
    if parts == ["profile.php"]:
        page_id = parse_qs(parsed.query).get("id", [""])[0]
        if page_id.isdigit():
            return f"https://www.facebook.com/profile.php?id={page_id}"
    if len(parts) != 1 or parts[0].casefold() in _RESERVED_PAGES or not re.fullmatch(r"[A-Za-z0-9.]+", parts[0]):
        raise ValueError("Use the Page home URL, for example https://www.facebook.com/UITconfess/.")
    return f"https://www.facebook.com/{parts[0]}/"


def page_identity(page_url: str) -> str:
    parsed = urlsplit(page_url)
    if parsed.path == "/profile.php":
        return parse_qs(parsed.query)["id"][0]
    return parsed.path.strip("/").casefold()


def normalize_post_url(value: str, page_url: str | None = None) -> PostRef | None:
    absolute = urljoin(page_url or DEFAULT_PAGE, value)
    if not is_facebook_url(absolute):
        return None
    parsed = urlsplit(absolute)
    parts = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if "comment_id" in query or "reply_comment_id" in query:
        # A comment permalink is not a new post discovered in the feed.
        return None
    if len(parts) == 3 and parts[1] in {"posts", "videos"}:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", parts[2]):
            return None
        if page_url and parts[0].casefold() != page_identity(page_url):
            return None
        return PostRef(f"https://www.facebook.com/{parts[0]}/{parts[1]}/{parts[2]}/", parts[2])
    if len(parts) == 1 and parts[0] in {"story.php", "permalink.php"}:
        story_id = query.get("story_fbid", [""])[0]
        owner = query.get("id", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", story_id) or not owner.isdigit():
            return None
        if page_url:
            identity = page_identity(page_url)
            expected = DEFAULT_PAGE_ID if identity == "uitconfess" else identity
            if expected.isdigit() and owner != expected:
                return None
        return PostRef("https://www.facebook.com/permalink.php?" + urlencode({"story_fbid": story_id, "id": owner}), story_id)
    # Reels can be discovered inside a Page article, but have no owner in the URL.
    if len(parts) == 2 and parts[0] == "reel" and parts[1].isdigit():
        return PostRef(f"https://www.facebook.com/reel/{parts[1]}/", parts[1])
    return None


def decode_comment_id(value: str) -> str:
    if value.isdigit():
        return value
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), validate=True).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return value
    matched = re.fullmatch(r"comment:\d+_(\d+)", raw)
    return matched.group(1) if matched else value


def comment_identity(value: str, post_url: str) -> tuple[str | None, str | None, str | None]:
    absolute = urljoin(post_url, value)
    if not is_facebook_url(absolute):
        return None, None, None
    query = parse_qs(urlsplit(absolute).query)
    root = query.get("comment_id", [None])[0]
    reply = query.get("reply_comment_id", [None])[0]
    if not root:
        return None, None, None
    root = decode_comment_id(root)
    reply = decode_comment_id(reply) if reply else None
    parsed = urlsplit(post_url)
    clean_query = {key: values[0] for key, values in parse_qs(parsed.query).items() if key in {"id", "story_fbid"}}
    clean_query["comment_id"] = root
    if reply:
        clean_query["reply_comment_id"] = reply
    permalink = urlunsplit(("https", "www.facebook.com", parsed.path, urlencode(clean_query), ""))
    return reply or root, root if reply else None, permalink
