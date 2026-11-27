from __future__ import annotations

from dataclasses import dataclass

from explainer.links import anchor_for


@dataclass(frozen=True)
class Chunk:
    file: str
    start_line: int
    end_line: int
    text: str
    index: int

    @property
    def key(self) -> str:
        return anchor_for(self.file, self.start_line)


def _numbered(lines: list[str], start: int, end: int) -> str:
    return "\n".join(f"L{i}: {lines[i - 1]}" for i in range(start, end + 1))


def chunk_text(
    file: str,
    text: str,
    *,
    chunk_lines: int = 120,
    overlap: int = 10,
    max_chars: int = 6500,
) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []

    avg = max(1, sum(len(line) for line in lines) // len(lines))
    per_chunk = max(1, min(chunk_lines, max_chars // (avg + 8)))
    effective_overlap = min(max(overlap, 0), max(0, per_chunk - 1))

    chunks: list[Chunk] = []
    start = 1
    total = len(lines)
    while start <= total:
        end = min(total, start + per_chunk - 1)
        body = _numbered(lines, start, end)
        while len(body) > max_chars and end > start:
            end -= max(1, (end - start + 1) // 2)
            body = _numbered(lines, start, end)

        chunks.append(
            Chunk(
                file=file,
                start_line=start,
                end_line=end,
                text=body,
                index=len(chunks),
            )
        )
        if end >= total:
            break

        next_start = end + 1 - effective_overlap
        start = next_start if next_start > start else start + 1

    return chunks
