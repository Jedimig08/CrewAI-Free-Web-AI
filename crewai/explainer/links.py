from __future__ import annotations

import os
import re
from pathlib import PurePosixPath


def slugify(value: str) -> str:
    value = value.lower().replace("\\", "/")
    slug = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return slug or "item"


class SlugRegistry:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def for_path(self, path: str) -> str:
        base = slugify(path)
        seen = self._seen.get(base, 0)
        self._seen[base] = seen + 1
        return base if seen == 0 else f"{base}-{seen}"


def anchor_for(file: str, start_line: int) -> str:
    return f"{slugify(file)}-{start_line}"


def rel_link(source: str, target: str, fragment: str | None = None) -> str:
    source_dir = PurePosixPath(source.replace("\\", "/")).parent
    base = "." if str(source_dir) == "." else str(source_dir)
    result = PurePosixPath(os.path.relpath(target.replace("\\", "/"), base)).as_posix()
    if fragment:
        return f"{result}#{fragment}"
    return result
