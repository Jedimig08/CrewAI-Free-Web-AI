from __future__ import annotations

import re
from dataclasses import dataclass, field

from explainer.symbols import SymbolIndex

ANCHOR_PATTERN = re.compile(
    r"(?:<!--\s*anchor:\s*([^\s]+)\s*-->|^\s*ANCHOR:\s*([^\s]+))",
    re.MULTILINE,
)
SUMMARY_PATTERN = re.compile(r"^SUMMARY:\s*(.*)$", re.MULTILINE)


@dataclass
class ChunkResult:
    anchor: str
    markdown: str
    file: str
    start_line: int
    end_line: int
    purpose: str = ""
    defines: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    error: str | None = None
    pending: bool = False


@dataclass(frozen=True)
class RevisionDirective:
    anchor: str
    instruction: str


def _parse_fields(raw: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        fields[key.strip().lower()] = value.strip()
    return fields


def _split_names(raw: str) -> list[str]:
    return [name for name in re.split(r"[,|\s]+", raw) if name and name.lower() != "none"]


def parse_chunk_output(
    text: str,
    *,
    file: str,
    start_line: int,
    end_line: int,
    expected_anchor: str,
) -> ChunkResult:
    body = (text or "").strip()
    anchor_match = ANCHOR_PATTERN.search(body)
    anchor = (
        (anchor_match.group(1) or anchor_match.group(2))
        if anchor_match
        else expected_anchor
    )

    summary_match = SUMMARY_PATTERN.search(body)
    fields: dict[str, str] = {}
    if summary_match:
        fields = _parse_fields(summary_match.group(1))
        body = body[: summary_match.start()].strip()

    if not anchor_match:
        body = f"ANCHOR: {anchor}\n{body}"

    return ChunkResult(
        anchor=anchor,
        markdown=body,
        file=file,
        start_line=start_line,
        end_line=end_line,
        purpose=fields.get("purpose", ""),
        defines=_split_names(fields.get("defines", "")),
        uses=_split_names(fields.get("uses", "")),
        unresolved=_split_names(fields.get("unresolved", "")),
    )


def failed_chunk(
    *, file: str, start_line: int, end_line: int, expected_anchor: str, error: str
) -> ChunkResult:
    detail = error.strip().replace("\n", " ")[:300]
    return ChunkResult(
        anchor=expected_anchor,
        markdown=(
            f"ANCHOR: {expected_anchor}\n"
            f"## {file} lines {start_line}-{end_line}\n"
            f"Explanation unavailable: {detail}"
        ),
        file=file,
        start_line=start_line,
        end_line=end_line,
        error=detail,
    )


def build_correlation_map(
    index: SymbolIndex,
    results: list[ChunkResult],
    files: list[str],
    *,
    max_chars: int = 6000,
) -> str:
    lines = ["Files in reading order:"]
    lines.extend(f"- {path}" for path in files)
    lines.append("")
    lines.append(index.summary(max_chars=2500))
    lines.append("")
    lines.append("Chunk summaries (anchor | file:lines | fields):")
    for result in results:
        lines.append(
            f"{result.anchor} | {result.file}:{result.start_line}-{result.end_line}"
            f" | purpose={result.purpose or 'none'}"
            f" | defines={','.join(result.defines) or 'none'}"
            f" | uses={','.join(result.uses) or 'none'}"
            f" | unresolved={','.join(result.unresolved) or 'none'}"
        )

    rendered = "\n".join(lines)
    return rendered[:max_chars]


def parse_integrator_output(
    text: str, valid_anchors: set[str]
) -> tuple[list[str], list[RevisionDirective]]:
    relationships: list[str] = []
    revisions: list[RevisionDirective] = []
    section: str | None = None

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("RELATIONSHIPS:"):
            section = "relationships"
            continue
        if stripped.upper().startswith("REVISIONS:"):
            section = "revisions"
            continue
        if not stripped.startswith("-"):
            continue
        content = stripped[1:].strip()
        if not content or content.lower() == "none":
            continue
        if section == "relationships":
            relationships.append(content)
        elif section == "revisions" and "|" in content:
            anchor, instruction = content.split("|", 1)
            anchor = anchor.strip()
            instruction = instruction.strip()
            if anchor in valid_anchors and instruction:
                revisions.append(RevisionDirective(anchor=anchor, instruction=instruction))

    return relationships or ["No cross-file relationships identified."], revisions
