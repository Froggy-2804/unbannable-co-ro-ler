"""Exclusive browser ownership and a durable human-review pause latch."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

from .models import utc_now


@contextmanager
def exclusive_run(data_dir: Path):
    """Lock a byte, rather than deleting lockfiles (which can race on restart)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        data_dir.chmod(0o700)
    handle = (data_dir / ".crawler.lock").open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("Another login/crawl process owns this data directory. Close it first.") from exc
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def pause_path(data_dir: Path) -> Path:
    return data_dir / "paused.json"


def read_pause(data_dir: Path) -> dict | None:
    path = pause_path(data_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Cannot read pause state. Inspect the data directory before resuming.") from exc


def save_pause(data_dir: Path, reason: str) -> None:
    path = pause_path(data_dir)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"reason": reason, "paused_at": utc_now()}, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def clear_pause(data_dir: Path) -> None:
    pause_path(data_dir).unlink(missing_ok=True)


def load_env_file(path: Path | str | None = None) -> None:
    """Load key-value pairs from .env into os.environ if not already present."""
    targets: list[Path] = []
    if path:
        targets.append(Path(path))
    else:
        targets.extend([Path(".env"), Path("data/.env")])

    for target in targets:
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
