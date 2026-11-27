from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "dist", "build",
    "target", "__pycache__", ".venv", "venv", ".idea", ".vscode", "coverage",
}

SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".woff", ".woff2",
    ".ttf", ".eot", ".mp3", ".mp4", ".wav", ".avi", ".mov", ".pyc",
    ".so", ".dll", ".dylib", ".o", ".a", ".class", ".jar", ".db",
    ".sqlite", ".sqlite3", ".lock", ".sum", ".min.js", ".min.css",
}

ENTRY_NAMES = {
    "main.py", "app.py", "__main__.py", "index.py", "server.py",
    "cli.py", "manage.py", "index.ts", "index.tsx", "index.js",
    "main.ts", "main.js", "app.ts", "app.js", "server.ts", "server.js",
}

PY_IMPORT = re.compile(
    r"^\s*(?:from\s+([A-Za-z0-9_.]+)\s+import|import\s+([A-Za-z0-9_.]+))",
    re.MULTILINE,
)
JS_IMPORT = re.compile(r"""(?:import[^'"\n]*from\s*|require\(\s*)['"]([^'"]+)['"]""")


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class FileInfo:
    path: str
    text: str


def validate_repo_url(
    url: str,
    allowed_hosts: set[str] | None = None,
    allow_local: bool = False,
) -> str:
    cleaned = url.strip()
    if not cleaned:
        raise RepoError("Repository URL is empty")

    parsed = urlparse(cleaned)
    if parsed.scheme == "file":
        if not allow_local:
            raise RepoError("file:// URLs are disabled")
        local = unquote(parsed.path)
        if not local or not Path(local).is_absolute():
            raise RepoError("file:// URL must contain an absolute path")
        return cleaned

    if parsed.scheme not in ("http", "https"):
        raise RepoError("Only http(s) repository URLs are allowed")
    if parsed.username or parsed.password:
        raise RepoError("Credentials in the repository URL are not allowed")

    host = parsed.hostname
    if not host:
        raise RepoError("Repository URL has no host")
    if allowed_hosts and host.lower() not in {h.lower() for h in allowed_hosts}:
        raise RepoError(f"Host {host!r} is not in the allowlist")

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RepoError(f"Could not resolve host {host!r}: {exc}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise RepoError("Repository URL resolves to a disallowed address")
    return cleaned


def clone_repo(url: str, dest: Path, timeout: int) -> Path:
    if dest.exists():
        raise RepoError(f"Clone destination already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    source = unquote(urlparse(url).path) if url.startswith("file://") else url
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
    command = [
        "git", "clone", "--depth", "1", "--single-branch",
        "--no-recurse-submodules", "--quiet", source, str(dest),
    ]
    try:
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"Clone timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise RepoError("git is not installed") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RepoError(f"Clone failed: {detail[:500]}")
    if not dest.is_dir():
        raise RepoError("Clone did not produce a directory")
    return dest


def discover_files(
    root: Path,
    *,
    max_files: int,
    max_bytes: int,
    focus_paths: list[str] | None = None,
) -> list[FileInfo]:
    prefixes = [p.replace("\\", "/").strip("/") for p in (focus_paths or []) if p.strip()]
    found: list[FileInfo] = []

    for current, dirs, names in os.walk(root):
        dirs[:] = sorted(
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(".") and not (Path(current) / d).is_symlink()
        )
        for name in sorted(names):
            full = Path(current) / name
            if full.is_symlink() or not full.is_file():
                continue
            if name.startswith(".") or name.lower().endswith(tuple(SKIP_SUFFIXES)):
                continue
            size = full.stat().st_size
            if size == 0 or size > max_bytes:
                continue

            relative = full.relative_to(root).as_posix()
            if prefixes and not any(
                relative == p or relative.startswith(p.rstrip("/") + "/") for p in prefixes
            ):
                continue

            raw = full.read_bytes()[:max_bytes]
            if b"\x00" in raw:
                continue
            found.append(FileInfo(path=relative, text=raw.decode("utf-8", errors="replace")))

    found.sort(key=lambda item: item.path)
    return found[:max_files]


def _module_names(file: str) -> set[str]:
    stem = file.rsplit(".", 1)[0].replace("\\", "/")
    names = {stem, stem.replace("/", "."), Path(stem).name}
    if Path(file).name == "__init__.py":
        package = str(Path(stem).parent).replace("\\", "/").replace("/", ".")
        if package and package != ".":
            names.add(package)
    return names


def _imported_modules(text: str) -> list[str]:
    modules: list[str] = []
    for match in PY_IMPORT.finditer(text):
        module = match.group(1) or match.group(2) or ""
        modules.append(module.lstrip("."))
    modules.extend(JS_IMPORT.findall(text))
    return [m for m in modules if m]


def _entry_score(file: FileInfo) -> int:
    score = 0
    if Path(file.path).name in ENTRY_NAMES:
        score += 10
    if '__name__ == "__main__"' in file.text or "__name__ == '__main__'" in file.text:
        score += 5
    if re.search(r"^\s*(?:async\s+)?def\s+main\s*\(", file.text, re.MULTILINE):
        score += 3
    if "export default" in file.text:
        score += 2
    return score


def prioritize_files(files: list[FileInfo]) -> list[FileInfo]:
    by_path = {item.path: item for item in files}
    module_map: dict[str, str] = {}
    for item in files:
        for name in _module_names(item.path):
            module_map.setdefault(name, item.path)
            module_map.setdefault(name.replace("/", "."), item.path)

    def resolve(importer: str, module: str) -> str | None:
        candidates = {module, module.replace("/", ".")}
        directory = str(Path(importer).parent).replace("\\", "/")
        if directory and directory != ".":
            base = directory.replace("/", ".")
            candidates.add(f"{base}.{module}")
            candidates.add(f"{directory}/{module}")
        for candidate in candidates:
            if candidate in module_map:
                return module_map[candidate]
        for candidate in candidates:
            for known, path in module_map.items():
                if known.endswith(candidate) or known.endswith("." + candidate):
                    return path
        return None

    ordered: list[FileInfo] = []
    seen: set[str] = set()

    def visit(path: str) -> None:
        if path in seen or path not in by_path:
            return
        seen.add(path)
        ordered.append(by_path[path])

    entries = sorted(files, key=lambda item: (-_entry_score(item), item.path))
    queue: deque[str] = deque(item.path for item in entries if _entry_score(item) > 0)
    if not queue:
        queue = deque(item.path for item in entries)

    while queue:
        path = queue.popleft()
        visit(path)
        for module in _imported_modules(by_path[path].text):
            target = resolve(path, module)
            if target and target not in seen:
                queue.append(target)

    for item in files:
        visit(item.path)
    return ordered
