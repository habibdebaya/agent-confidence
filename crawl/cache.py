from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable


def cache_key(*parts: object) -> str:
    raw = "\0".join(str(part) for part in parts).encode()
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True))
    temporary.replace(path)


def cached_json(path: Path, fetch: Callable[[], Any]) -> Any:
    cached = read_json(path)
    if cached is not None:
        return cached
    value = fetch()
    write_json(path, value)
    return value
