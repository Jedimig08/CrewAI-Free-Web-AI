from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from explainer.chunker import Chunk, chunk_text
from explainer.crew import (
    BLOCKS_RULES,
    LITERAL_RULES,
    build_chunk_crew,
    build_integrator_crew,
    create_llm,
)
from explainer.links import SlugRegistry, rel_link, slugify
from explainer.models import (
    ExplainMode,
    ExplainRequest,
    Granularity,
    JobState,
    JobStatus,
    RevisionRecord,
)
from explainer.refine import (
    ChunkResult,
    RevisionDirective,
    build_correlation_map,
    failed_chunk,
    parse_chunk_output,
    parse_integrator_output,
)
from explainer.repo import (
    FileInfo,
    RepoError,
    clone_repo,
    discover_files,
    prioritize_files,
    validate_repo_url,
)
from explainer.symbols import SymbolIndex

logger = logging.getLogger("crewai-explainer")

_TOTAL_WORKERS = threading.BoundedSemaphore(
    max(1, int(os.getenv("EXPLAIN_MAX_TOTAL_WORKERS", "8")))
)


@dataclass
class PipelineConfig:
    output_dir: Path
    model: str
    max_file_bytes: int = 200_000
    chunk_lines: int = 120
    chunk_overlap: int = 10
    chunk_max_chars: int = 6500
    max_chunks: int = 200
    max_workers: int = 4
    agent_timeout_sec: int = 300
    job_timeout_sec: int = 1800
    clone_timeout_sec: int = 60
    allowed_hosts: set[str] = field(default_factory=set)
    allow_local: bool = False
    refine: bool = True
    max_refine_rounds: int = 1
    live_every: int = 1
    llm_factory: Callable[[str], object] | None = None


def job_directory(output_dir: Path, job_id: str) -> Path:
    return output_dir / job_id


def save_job(output_dir: Path, job: JobState) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    directory = job_directory(output_dir, job.job_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "job.json"
    if target.is_file():
        try:
            stored = JobState.model_validate_json(target.read_text(encoding="utf-8"))
            if stored.cancel_requested:
                job.cancel_requested = True
        except (ValueError, OSError):
            pass
    temporary = directory / "job.json.tmp"
    temporary.write_text(job.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(target)


def load_job(output_dir: Path, job_id: str) -> JobState | None:
    path = job_directory(output_dir, job_id) / "job.json"
    if not path.is_file():
        return None
    try:
        return JobState.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("Could not read job state at %s", path)
        return None


def list_jobs(output_dir: Path) -> list[str]:
    if not output_dir.is_dir():
        return []
    return sorted(
        path.name
        for path in output_dir.iterdir()
        if path.is_dir() and (path / "job.json").is_file()
    )


def new_job(request: ExplainRequest) -> JobState:
    return JobState(
        job_id=uuid.uuid4().hex,
        repo_url=request.repo_url,
        model=request.model,
        mode=request.mode,
        granularity=request.granularity,
    )


def elapsed_seconds(job: JobState) -> float | None:
    if not job.started_at:
        return None
    try:
        start = datetime.fromisoformat(job.started_at)
        end = (
            datetime.fromisoformat(job.finished_at)
            if job.finished_at
            else datetime.now(timezone.utc)
        )
    except ValueError:
        return None
    return max(0.0, (end - start).total_seconds())


def eta_seconds(job: JobState) -> float | None:
    progress = job.progress
    if not job.started_at or progress.done <= 0 or progress.total <= progress.done:
        return None
    elapsed = elapsed_seconds(job)
    if elapsed is None:
        return None
    return max(0.0, elapsed / progress.done * (progress.total - progress.done))


def pending_result(chunk: Chunk) -> ChunkResult:
    return ChunkResult(
        anchor=chunk.key,
        markdown=(
            f"ANCHOR: {chunk.key}\n"
            f"## {chunk.file} lines {chunk.start_line}-{chunk.end_line}\n\n"
            "Explaining this section..."
        ),
        file=chunk.file,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        pending=True,
    )


def assemble_live(
    chunks: list[Chunk],
    results: list[ChunkResult],
    files: list[FileInfo],
    repo_name: str,
) -> str:
    by_anchor = {result.anchor: result for result in results}
    parts = [
        f"# {repo_name} — live explanation",
        "",
        "> This file updates while the job runs. Unfinished sections are marked as pending.",
        "",
        "## Start here",
        "",
    ]
    parts.extend(f"- `{item.path}`" for item in files)
    parts.append("")
    for chunk in chunks:
        parts.append(by_anchor.get(chunk.key, pending_result(chunk)).markdown)
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def render_changes(revisions: list[RevisionRecord]) -> str:
    parts = ["# Pass 2 changes", ""]
    for item in revisions:
        parts.extend(
            [
                f"## {item.file} lines {item.start_line}-{item.end_line}",
                "",
                f"Instruction: {item.instruction}",
                "",
                "### Before",
                "",
                item.before,
                "",
                "### After",
                "",
                item.after,
                "",
            ]
        )
    return "\n".join(parts).rstrip() + "\n"


class ExplainerPipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self._live_chunks: list[Chunk] = []
        self._live_files: list[FileInfo] = []
        self._live_repo_name = "repository"
        self._live_results: dict[str, ChunkResult] = {}
        self._live_done = 0
        self._live_request: ExplainRequest | None = None

    def run(self, job: JobState, request: ExplainRequest) -> None:
        acquired = _TOTAL_WORKERS.acquire(timeout=self.config.job_timeout_sec)
        if not acquired:
            self._fail(job, "No explainer worker slot became available")
            return

        repo_dir = job_directory(self.config.output_dir, job.job_id) / "repo"
        job.started_at = datetime.now(timezone.utc).isoformat()
        try:
            self._status(job, JobStatus.CLONING)
            url = validate_repo_url(
                request.repo_url, self.config.allowed_hosts, self.config.allow_local
            )
            clone_repo(url, repo_dir, self.config.clone_timeout_sec)

            self._status(job, JobStatus.INDEXING)
            files = discover_files(
                repo_dir,
                max_files=request.max_files,
                max_bytes=self.config.max_file_bytes,
                focus_paths=request.focus_paths,
            )
            if not files:
                raise RepoError("No explainable source files were found")
            files = prioritize_files(files)

            chunks: list[Chunk] = []
            for item in files:
                chunks.extend(
                    chunk_text(
                        item.path,
                        item.text,
                        chunk_lines=self.config.chunk_lines,
                        overlap=self.config.chunk_overlap,
                        max_chars=self.config.chunk_max_chars,
                    )
                )
            if len(chunks) > self.config.max_chunks:
                logger.warning("Truncating %d chunks to %d", len(chunks), self.config.max_chunks)
                chunks = chunks[: self.config.max_chunks]
                job.partial = True

            index = SymbolIndex.build(files)
            for chunk in chunks:
                index.record_usage(chunk.file, chunk.text)

            self._live_chunks = chunks
            self._live_files = files
            self._live_repo_name = _repo_name(request.repo_url)
            self._live_results = {}
            self._live_done = 0
            self._live_request = request
            if request.live:
                self._write_live(job)

            results = self._explain_pass(job, chunks, index, request)

            relationships: list[str] = []
            if self.config.refine and request.refine and results and not job.cancel_requested:
                for _ in range(self.config.max_refine_rounds):
                    relationships, directives = self._correlate(
                        job, index, results, files, request.mode
                    )
                    if not directives:
                        break
                    before = {result.anchor: result for result in results}
                    results = self._apply_revisions(job, chunks, index, results, directives, request)
                    self._record_revisions(job, directives, before, results)

            if request.live:
                self._write_live(job)

            self._status(job, JobStatus.ASSEMBLING)
            outputs = assemble(
                results=results,
                files=files,
                index=index,
                mode=request.mode,
                relationships=relationships,
                repo_name=_repo_name(request.repo_url),
            )
            if job.revisions:
                outputs["changes.md"] = render_changes(job.revisions)

            for relative, content in outputs.items():
                path = job_directory(self.config.output_dir, job.job_id) / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            primary = "line-by-line.md" if request.mode == ExplainMode.LINE_BY_LINE else "overview.md"
            extra_files = ["job.json"] + (["live.md"] if job.live_available else [])
            job.files = sorted([*outputs, *extra_files])
            job.text = outputs.get(primary, "")
            job.finished_at = datetime.now(timezone.utc).isoformat()
            if job.cancel_requested:
                job.status = JobStatus.CANCELLED
            elif job.partial:
                job.status = JobStatus.PARTIAL
            else:
                job.status = JobStatus.COMPLETED
            save_job(self.config.output_dir, job)
        except Exception as exc:
            logger.exception("Explainer job %s failed", job.job_id)
            self._fail(job, str(exc))
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)
            _TOTAL_WORKERS.release()

    def _status(self, job: JobState, status: JobStatus, **changes: object) -> None:
        job.status = status
        for key, value in changes.items():
            setattr(job, key, value)
        save_job(self.config.output_dir, job)

    def _fail(self, job: JobState, error: str) -> None:
        job.status = JobStatus.FAILED
        job.error = error.strip()[:1000]
        job.finished_at = datetime.now(timezone.utc).isoformat()
        save_job(self.config.output_dir, job)

    def _write_live(self, job: JobState) -> None:
        request = getattr(self, "_live_request", None)
        if request is None or not request.live:
            return
        directory = job_directory(self.config.output_dir, job.job_id)
        results = [
            self._live_results.get(chunk.key, pending_result(chunk))
            for chunk in self._live_chunks
        ]
        content = assemble_live(
            self._live_chunks, results, self._live_files, self._live_repo_name
        )
        temporary = directory / "live.md.tmp"
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(directory / "live.md")
        job.live_available = True
        save_job(self.config.output_dir, job)

    def _record_revisions(
        self,
        job: JobState,
        directives: list[RevisionDirective],
        before: dict[str, ChunkResult],
        after: list[ChunkResult],
    ) -> None:
        after_by_anchor = {result.anchor: result for result in after}
        records: list[RevisionRecord] = []
        for directive in directives:
            old = before.get(directive.anchor)
            new = after_by_anchor.get(directive.anchor)
            if old is None or new is None or old.markdown == new.markdown:
                continue
            records.append(
                RevisionRecord(
                    anchor=directive.anchor,
                    file=new.file,
                    start_line=new.start_line,
                    end_line=new.end_line,
                    instruction=directive.instruction,
                    before=old.markdown[:3000],
                    after=new.markdown[:3000],
                )
            )
        job.revisions = records
        save_job(self.config.output_dir, job)

    def _llm(self):
        factory = self.config.llm_factory or create_llm
        return factory(self.config.model)

    def _explain_pass(
        self,
        job: JobState,
        chunks: list[Chunk],
        index: SymbolIndex,
        request: ExplainRequest,
    ) -> list[ChunkResult]:
        self._status(job, JobStatus.EXPLAINING)
        return self._run_workers(
            job=job,
            chunks=chunks,
            workers=min(self.config.max_workers, max(1, len(chunks))),
            task=lambda chunk, revision: self._explain_chunk(chunk, index, request, revision),
            revisions={},
        )

    def _explain_chunk(
        self,
        chunk: Chunk,
        index: SymbolIndex,
        request: ExplainRequest,
        revision: str,
    ) -> ChunkResult:
        inputs = {
            "file": chunk.file,
            "line_start": chunk.start_line,
            "line_end": chunk.end_line,
            "numbered_code": chunk.text,
            "symbol_context": index.context_for(chunk.text),
            "revision_context": revision or "No revision instructions.",
            "granularity_rules": (
                LITERAL_RULES if request.granularity == Granularity.LITERAL else BLOCKS_RULES
            ),
            "mode": request.mode.value,
            "anchor": chunk.key,
        }
        crew = build_chunk_crew(self._llm(), self.config.agent_timeout_sec)
        output = str(crew.kickoff(inputs=inputs))
        return parse_chunk_output(
            output,
            file=chunk.file,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            expected_anchor=chunk.key,
        )

    def _run_workers(
        self,
        *,
        job: JobState,
        chunks: list[Chunk],
        workers: int,
        task: Callable[[Chunk, str], ChunkResult],
        revisions: dict[str, str],
    ) -> list[ChunkResult]:
        results: dict[str, ChunkResult] = {}
        total = len(chunks)
        done = 0
        job.progress.total = total
        job.progress.workers = workers
        save_job(self.config.output_dir, job)

        if job.cancel_requested:
            job.partial = True
            for chunk in chunks:
                results[chunk.key] = failed_chunk(
                    file=chunk.file,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    expected_anchor=chunk.key,
                    error="Cancelled by user",
                )
                self._live_results[chunk.key] = results[chunk.key]
            self._write_live(job)
            return [results[chunk.key] for chunk in chunks]

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_safe_task, task, chunk, revisions.get(chunk.key, "")): chunk
                for chunk in chunks
            }
            try:
                for future in as_completed(futures, timeout=self.config.job_timeout_sec):
                    chunk = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        logger.exception("Worker failed for chunk %s", chunk.key)
                        result = failed_chunk(
                            file=chunk.file,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            expected_anchor=chunk.key,
                            error=str(exc),
                        )
                    results[chunk.key] = result
                    self._live_results[chunk.key] = result
                    done += 1
                    self._live_done += 1
                    job.progress.done = done
                    save_job(self.config.output_dir, job)
                    if (
                        self.config.live_every <= 1
                        or self._live_done % self.config.live_every == 0
                        or done == total
                    ):
                        self._write_live(job)

                    if job.cancel_requested:
                        job.partial = True
                        for pending in futures:
                            pending.cancel()
                        break
            except TimeoutError:
                logger.warning("Explain batch timed out for job %s", job.job_id)
                job.partial = True
                for future in futures:
                    future.cancel()
                wait(futures, timeout=5, return_when=ALL_COMPLETED)

        for chunk in chunks:
            if chunk.key not in results:
                results[chunk.key] = failed_chunk(
                    file=chunk.file,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                    expected_anchor=chunk.key,
                    error="Cancelled by user"
                    if job.cancel_requested
                    else "Worker did not finish before the job timeout",
                )
                self._live_results[chunk.key] = results[chunk.key]
        self._write_live(job)
        return [results[chunk.key] for chunk in chunks]

    def _correlate(
        self,
        job: JobState,
        index: SymbolIndex,
        results: list[ChunkResult],
        files: list[FileInfo],
        mode: ExplainMode,
    ) -> tuple[list[str], list[RevisionDirective]]:
        self._status(job, JobStatus.CORRELATING)
        correlation_map = build_correlation_map(
            index, results, [item.path for item in files]
        )
        crew = build_integrator_crew(self._llm(), self.config.agent_timeout_sec)
        try:
            output = str(crew.kickoff(inputs={"mode": mode.value, "correlation_map": correlation_map}))
        except Exception as exc:
            logger.exception("Integrator failed for job %s", job.job_id)
            job.partial = True
            return ["Integration unavailable: " + str(exc)[:200]], []
        return parse_integrator_output(output, {result.anchor for result in results})

    def _apply_revisions(
        self,
        job: JobState,
        chunks: list[Chunk],
        index: SymbolIndex,
        results: list[ChunkResult],
        directives: list[RevisionDirective],
        request: ExplainRequest,
    ) -> list[ChunkResult]:
        self._status(job, JobStatus.REVISING)
        by_anchor = {result.anchor: result for result in results}
        selected = [chunk for chunk in chunks if chunk.key in {d.anchor for d in directives}]
        if not selected:
            return results

        instructions: dict[str, str] = {}
        for directive in directives:
            existing = instructions.get(directive.anchor, "")
            instructions[directive.anchor] = (
                f"{existing}\n- {directive.instruction}" if existing else f"- {directive.instruction}"
            )

        revised = self._run_workers(
            job=job,
            chunks=selected,
            workers=min(self.config.max_workers, len(selected)),
            task=lambda chunk, revision: self._explain_chunk(chunk, index, request, revision),
            revisions=instructions,
        )
        for result in revised:
            by_anchor[result.anchor] = result
        return [by_anchor[chunk.key] for chunk in chunks]


def _safe_task(
    task: Callable[[Chunk, str], ChunkResult], chunk: Chunk, revision: str
) -> ChunkResult:
    try:
        return task(chunk, revision)
    except Exception as exc:
        logger.exception("Chunk worker raised for %s", chunk.key)
        return failed_chunk(
            file=chunk.file,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            expected_anchor=chunk.key,
            error=str(exc),
        )


def _repo_name(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    name = Path(path).name
    return name[:-4] if name.endswith(".git") else (name or "repository")


def assemble(
    *,
    results: list[ChunkResult],
    files: list[FileInfo],
    index: SymbolIndex,
    mode: ExplainMode,
    relationships: list[str],
    repo_name: str,
) -> dict[str, str]:
    if mode == ExplainMode.LINE_BY_LINE:
        return _assemble_line_by_line(results, files, relationships, repo_name)
    return _assemble_wiki(results, files, index, relationships, repo_name)


def _relation_block(relationships: list[str]) -> list[str]:
    if not relationships:
        return []
    return ["## How the parts fit together", ""] + [f"- {item}" for item in relationships] + [""]


def _assemble_line_by_line(
    results: list[ChunkResult],
    files: list[FileInfo],
    relationships: list[str],
    repo_name: str,
) -> dict[str, str]:
    parts = [f"# {repo_name} — line-by-line explanation", "", "## Start here", ""]
    parts.extend(f"- `{item.path}`" for item in files)
    parts.append("")
    for item in files:
        for result in results:
            if result.file == item.path:
                parts.extend([result.markdown, ""])
    parts.extend(_relation_block(relationships))
    return {"line-by-line.md": "\n".join(parts).rstrip() + "\n"}


def _assemble_wiki(
    results: list[ChunkResult],
    files: list[FileInfo],
    index: SymbolIndex,
    relationships: list[str],
    repo_name: str,
) -> dict[str, str]:
    file_slugs = SlugRegistry()
    symbol_slugs = SlugRegistry()
    file_links = {item.path: file_slugs.for_path(item.path) for item in files}
    file_index = {item.path: position for position, item in enumerate(files, start=1)}

    sorted_symbols = sorted(
        index.definitions.values(), key=lambda item: (item.file, item.line, item.name)
    )
    symbol_page = {
        symbol.name: symbol_slugs.for_path(symbol.name) for symbol in sorted_symbols
    }

    file_bodies: dict[str, list[str]] = {item.path: [] for item in files}
    for item in files:
        defined = sorted(
            (symbol for symbol in index.definitions.values() if symbol.file == item.path),
            key=lambda symbol: symbol.line,
        )
        header = [f"# {item.path}", ""]
        if defined:
            links = [
                f"[{symbol.name}](../symbols/{symbol_page[symbol.name]}.md)"
                for symbol in defined
            ]
            header.extend(["Key symbols: " + ", ".join(links), ""])
        file_bodies[item.path].extend(header)

    for result in results:
        file_bodies.setdefault(result.file, [f"# {result.file}", ""])
        file_bodies[result.file].extend([result.markdown, ""])

    outputs: dict[str, str] = {}
    for path, body in file_bodies.items():
        outputs[f"files/{file_links[path]}.md"] = "\n".join(body).rstrip() + "\n"

    for symbol in sorted_symbols:
        slug = symbol_page[symbol.name]
        users = sorted(index.uses.get(symbol.name, set()))
        lines = [
            f"# {symbol.name}",
            "",
            f"Defined at `{symbol.file}:{symbol.line}` ({symbol.kind}).",
            "",
        ]
        if symbol.file in file_links:
            lines.append(
                f"See the containing file: [{symbol.file}]({rel_link(f'symbols/{slug}.md', f'files/{file_links[symbol.file]}.md')})."
            )
            lines.append("")
        if users:
            links = [
                f"[{user}]({rel_link(f'symbols/{slug}.md', f'files/{file_links[user]}.md')})"
                for user in users
                if user in file_links
            ]
            if links:
                lines.append("Used in: " + ", ".join(links))
                lines.append("")
        outputs[f"symbols/{slug}.md"] = "\n".join(lines).rstrip() + "\n"

    overview = [f"# {repo_name} — overview", "", "## Start here", ""]
    for position, item in enumerate(files, start=1):
        overview.append(f"{position}. [{item.path}]({rel_link('overview.md', f'files/{file_links[item.path]}.md')})")
    overview.append("")
    overview.extend(_relation_block(relationships))
    overview.extend(["## File map", ""])
    for item in files:
        overview.append(f"- [{item.path}]({rel_link('overview.md', f'files/{file_links[item.path]}.md')})")
    overview.append("")
    overview.extend(["## File relationships", "", "```mermaid", "graph LR"])
    for position, item in enumerate(files, start=1):
        overview.append(f'  n{position}["{item.path}"]')
    known_paths = {item.path for item in files}
    for item in files:
        source = file_index[item.path]
        for imported in _approximate_imports(item.text, known_paths):
            if imported != item.path:
                overview.append(f"  n{source} --> n{file_index[imported]}")
    overview.extend(["```", ""])
    outputs["overview.md"] = "\n".join(overview).rstrip() + "\n"
    return outputs


def _approximate_imports(text: str, paths: set[str]) -> list[str]:
    from explainer.repo import _imported_modules

    matches: list[str] = []
    for module in _imported_modules(text):
        normalized = module.replace("/", ".").lstrip(".")
        for path in sorted(paths):
            stem = path.rsplit(".", 1)[0].replace("/", ".")
            if stem == normalized or stem.endswith("." + normalized):
                matches.append(path)
    return sorted(set(matches))
