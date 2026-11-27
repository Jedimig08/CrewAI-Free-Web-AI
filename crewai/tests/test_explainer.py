from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from explainer.chunker import chunk_text
from explainer.links import SlugRegistry, anchor_for, rel_link, slugify
from explainer.models import (
    ExplainMode,
    ExplainRequest,
    Granularity,
    JobState,
    JobStatus,
    RevisionRecord,
)
from explainer.pipeline import (
    ExplainerPipeline,
    PipelineConfig,
    assemble,
    assemble_live,
    elapsed_seconds,
    eta_seconds,
    list_jobs,
    load_job,
    render_changes,
    save_job,
)
from explainer.refine import (
    build_correlation_map,
    failed_chunk,
    parse_chunk_output,
    parse_integrator_output,
)
from explainer.repo import FileInfo, discover_files, prioritize_files, validate_repo_url
from explainer.symbols import SymbolIndex

LINK_PATTERN = re.compile(r"\]\(([^)#]+)(?:#[^)]*)?\)")


def test_chunk_ranges_and_line_numbers():
    text = "\n".join(f"line {number}" for number in range(1, 26))
    chunks = chunk_text("src/app.py", text, chunk_lines=10, overlap=2, max_chars=1000)

    assert [(chunk.start_line, chunk.end_line) for chunk in chunks] == [
        (1, 10), (9, 18), (17, 25)
    ]
    assert chunks[0].text.startswith("L1: line 1\n")
    assert chunks[0].key == "src-app-py-1"


def test_chunks_shrink_to_fit_char_budget():
    text = "\n".join("x" * 200 for _ in range(20))
    chunks = chunk_text("big.py", text, chunk_lines=20, overlap=0, max_chars=500)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 500 for chunk in chunks)


def test_slugs_and_relative_links():
    assert slugify("src\\App.PY") == "src-app-py"
    assert anchor_for("src/app.py", 45) == "src-app-py-45"
    assert rel_link("files/a.md", "symbols/b.md") == "../symbols/b.md"
    assert rel_link("overview.md", "files/a.md", "app-py-1") == "files/a.md#app-py-1"

    registry = SlugRegistry()
    assert registry.for_path("src/app.py") == "src-app-py"
    assert registry.for_path("SRC/app.py") == "src-app-py-1"


def test_url_validation(monkeypatch):
    monkeypatch.setattr(
        "explainer.repo.socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("93.184.215.14", 443))],
    )
    assert validate_repo_url("https://example.com/repo.git") == "https://example.com/repo.git"

    for bad in ("ftp://example.com/x", "https://user:pw@example.com/x", "https://x@"):
        try:
            validate_repo_url(bad)
        except Exception:
            pass
        else:
            raise AssertionError(f"expected rejection for {bad}")

    monkeypatch.setattr(
        "explainer.repo.socket.getaddrinfo",
        lambda host, port: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    try:
        validate_repo_url("http://localhost/repo.git")
    except Exception:
        pass
    else:
        raise AssertionError("private address should be rejected")

    try:
        validate_repo_url("file:///tmp/repo")
    except Exception:
        pass
    else:
        raise AssertionError("file URL should be disabled by default")

    assert validate_repo_url("file:///tmp/repo", allow_local=True) == "file:///tmp/repo"


def test_discovery_and_entry_point_order(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(
        "import util\n\nif __name__ == '__main__':\n    util.helper()\n", encoding="utf-8"
    )
    (tmp_path / "src" / "util.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "image.png").write_bytes(b"\x89PNG\x00")

    files = discover_files(tmp_path, max_files=10, max_bytes=10000)
    assert [item.path for item in files] == ["src/app.py", "src/util.py"]

    ordered = prioritize_files(files)
    assert [item.path for item in ordered] == ["src/app.py", "src/util.py"]


def test_symbol_index_provenance():
    files = [
        FileInfo("app.py", "from util import helper\n\nvalue = helper()\n"),
        FileInfo("util.py", "def helper():\n    return 1\n"),
    ]
    index = SymbolIndex.build(files)
    context = index.context_for("value = helper()")

    assert "helper -> util.py:1 (function)" in context
    assert "value -> app.py:3 (variable)" in context


def test_chunk_output_parsing_and_failure():
    result = parse_chunk_output(
        "<!-- anchor: app-py-45 -->\n## app.py lines 45-60\nPurpose: main entry.\n"
        "SUMMARY: purpose=main entry; defines=main; uses=helper; unresolved=none",
        file="app.py",
        start_line=45,
        end_line=60,
        expected_anchor="app-py-45",
    )
    assert result.anchor == "app-py-45"
    assert result.purpose == "main entry"
    assert result.defines == ["main"]
    assert "SUMMARY" not in result.markdown

    fallback = parse_chunk_output(
        "no anchor here",
        file="app.py",
        start_line=1,
        end_line=5,
        expected_anchor="app-py-1",
    )
    assert fallback.anchor == "app-py-1"
    assert fallback.markdown.startswith("ANCHOR: app-py-1")

    broken = failed_chunk(
        file="app.py", start_line=1, end_line=5,
        expected_anchor="app-py-1", error="boom"
    )
    assert broken.error == "boom"


def test_correlation_map_and_revision_filtering():
    index = SymbolIndex.build([FileInfo("util.py", "def helper():\n    return 1\n")])
    result = parse_chunk_output(
        "<!-- anchor: app-py-1 -->\nSUMMARY: purpose=entry; uses=helper",
        file="app.py", start_line=1, end_line=10, expected_anchor="app-py-1",
    )
    mapping = build_correlation_map(index, [result], ["app.py", "util.py"])
    assert "app-py-1" in mapping
    assert "helper (function) - util.py:1" in mapping

    relationships, revisions = parse_integrator_output(
        "RELATIONSHIPS:\n- app uses util\nREVISIONS:\n"
        "- app-py-1 | mention helper source\n- bogus-anchor | ignore me\n",
        {"app-py-1"},
    )
    assert relationships == ["app uses util"]
    assert [(item.anchor, item.instruction) for item in revisions] == [
        ("app-py-1", "mention helper source")
    ]


def test_wiki_links_resolve():
    files = [
        FileInfo("app.py", "from util import helper\nvalue = helper()\n"),
        FileInfo("util.py", "def helper():\n    return 1\n"),
    ]
    index = SymbolIndex.build(files)
    for item in files:
        index.record_usage(item.path, item.text)

    results = [
        parse_chunk_output(
            "<!-- anchor: app-py-1 -->\n## app.py lines 1-2\nPurpose: entry.",
            file="app.py", start_line=1, end_line=2, expected_anchor="app-py-1",
        ),
        parse_chunk_output(
            "<!-- anchor: util-py-1 -->\n## util.py lines 1-2\nPurpose: helper.",
            file="util.py", start_line=1, end_line=2, expected_anchor="util-py-1",
        ),
    ]
    outputs = assemble(
        results=results, files=files, index=index, mode=ExplainMode.WIKI,
        relationships=["app uses util"], repo_name="demo",
    )

    assert "overview.md" in outputs and "symbols/helper.md" in outputs
    for source, body in outputs.items():
        for target in LINK_PATTERN.findall(body):
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
            assert resolved in outputs, f"{source} -> {target} missing"


def test_job_persistence(tmp_path):
    request = ExplainRequest(
        repo_url="https://example.com/repo.git",
        mode=ExplainMode.LINE_BY_LINE,
        granularity=Granularity.LITERAL,
    )
    job = JobState(
        job_id="abc123", repo_url=request.repo_url, mode=request.mode,
        granularity=request.granularity,
    )
    save_job(tmp_path, job)

    loaded = load_job(tmp_path, "abc123")
    assert loaded is not None and loaded.job_id == "abc123"
    assert list_jobs(tmp_path) == ["abc123"]


def test_live_assembly_has_ordered_pending_placeholders():
    from explainer.chunker import chunk_text

    files = [FileInfo("app.py", "line one\nline two\nline three\n")]
    chunks = chunk_text("app.py", files[0].text, chunk_lines=2, overlap=0)
    done = parse_chunk_output(
        "ANCHOR: app-py-1\n## app.py lines 1-2\nPurpose: first block.",
        file="app.py", start_line=1, end_line=2, expected_anchor="app-py-1",
    )

    live = assemble_live(chunks, [done], files, "demo")
    first = live.index("## app.py lines 1-2")
    second = live.index("## app.py lines 3-3")

    assert first < second
    assert "Purpose: first block." in live
    assert "Explaining this section..." in live


def test_elapsed_and_eta():
    from datetime import datetime, timedelta, timezone

    job = JobState(
        job_id="abc", repo_url="x", mode=ExplainMode.WIKI,
        granularity=Granularity.BLOCKS,
        started_at=(datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat(),
    )
    job.progress.done = 2
    job.progress.total = 4

    assert elapsed_seconds(job) >= 39
    assert eta_seconds(job) is not None and 39 <= eta_seconds(job) <= 41
    job.progress.done = 0
    assert eta_seconds(job) is None


def test_render_changes():
    record = RevisionRecord(
        anchor="app-py-1", file="app.py", start_line=1, end_line=2,
        instruction="mention helper source", before="old text", after="new text",
    )
    rendered = render_changes([record])

    assert "## app.py lines 1-2" in rendered
    assert "Instruction: mention helper source" in rendered
    assert "### Before" in rendered and "old text" in rendered
    assert "### After" in rendered and "new text" in rendered


def _make_git_repo(path, cancel_job=None):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    (path / "app.py").write_text(
        "def one():\n    return 1\n\n\ndef two():\n    return 2\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=a@b", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )

    class FakeCrew:
        def __init__(self, kind=None):
            self.kind = kind

        def kickoff(self, inputs):
            if cancel_job is not None:
                cancel_job.cancel_requested = True
            return (
                f"ANCHOR: {inputs['anchor']}\n"
                f"## {inputs['file']} lines {inputs['line_start']}-{inputs['line_end']}\n"
                "Purpose: fake."
            )

    return FakeCrew


def test_pipeline_writes_live_file(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    output = tmp_path / "out"
    _make_git_repo(repo)

    class FakeCrew:
        def kickoff(self, inputs):
            return (
                f"ANCHOR: {inputs['anchor']}\n"
                f"## {inputs['file']} lines {inputs['line_start']}-{inputs['line_end']}\n"
                "Purpose: fake."
            )

    monkeypatch.setattr("explainer.pipeline.build_chunk_crew", lambda llm, sec: FakeCrew())
    monkeypatch.setattr("explainer.pipeline.build_integrator_crew", lambda llm, sec: FakeCrew())

    request = ExplainRequest(
        repo_url=repo.as_uri(), mode=ExplainMode.WIKI, live=True,
        refine=False, max_workers=2,
    )
    job = JobState(
        job_id="a" * 32, repo_url=request.repo_url, mode=request.mode,
        granularity=request.granularity,
    )
    config = PipelineConfig(
        output_dir=output, model="fake", allow_local=True, max_workers=2,
        llm_factory=lambda model: object(),
    )
    ExplainerPipeline(config).run(job, request)

    live = (output / job.job_id / "live.md").read_text(encoding="utf-8")
    assert job.status == JobStatus.COMPLETED
    assert job.live_available is True
    assert "live.md" in job.files
    assert "## app.py lines 1-6" in live
    assert "Explaining this section..." not in live


def test_pipeline_cancel(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    output = tmp_path / "out"
    job = JobState(
        job_id="b" * 32, repo_url=repo.as_uri(), mode=ExplainMode.WIKI,
        granularity=Granularity.BLOCKS,
    )
    FakeCrew = _make_git_repo(repo, cancel_job=job)

    monkeypatch.setattr("explainer.pipeline.build_chunk_crew", lambda llm, sec: FakeCrew())
    monkeypatch.setattr("explainer.pipeline.build_integrator_crew", lambda llm, sec: FakeCrew())

    request = ExplainRequest(
        repo_url=repo.as_uri(), mode=ExplainMode.WIKI, live=True,
        refine=False, max_workers=2,
    )
    config = PipelineConfig(
        output_dir=output, model="fake", allow_local=True, max_workers=2,
        chunk_lines=2, llm_factory=lambda model: object(),
    )
    ExplainerPipeline(config).run(job, request)

    assert job.status == JobStatus.CANCELLED
    assert job.partial is True
    live = (output / job.job_id / "live.md").read_text(encoding="utf-8")
    assert "Cancelled by user" in live


def test_pipeline_revision_diff(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    output = tmp_path / "out"
    _make_git_repo(repo)

    class ChunkCrew:
        def kickoff(self, inputs):
            revised = "mention helper source" in inputs["revision_context"]
            suffix = "\nRevised text." if revised else ""
            return (
                f"ANCHOR: {inputs['anchor']}\n"
                f"## {inputs['file']} lines {inputs['line_start']}-{inputs['line_end']}\n"
                f"Purpose: fake.{suffix}"
            )

    class IntegratorCrew:
        def kickoff(self, inputs):
            return (
                "RELATIONSHIPS:\n- app relates to itself\n"
                "REVISIONS:\n- app-py-1 | mention helper source\n"
            )

    monkeypatch.setattr("explainer.pipeline.build_chunk_crew", lambda llm, sec: ChunkCrew())
    monkeypatch.setattr("explainer.pipeline.build_integrator_crew", lambda llm, sec: IntegratorCrew())

    request = ExplainRequest(
        repo_url=repo.as_uri(), mode=ExplainMode.WIKI, refine=True,
        live=False, max_workers=1,
    )
    job = JobState(
        job_id="c" * 32, repo_url=request.repo_url, mode=request.mode,
        granularity=request.granularity,
    )
    config = PipelineConfig(
        output_dir=output, model="fake", allow_local=True, max_workers=1,
        chunk_lines=2, llm_factory=lambda model: object(),
    )
    ExplainerPipeline(config).run(job, request)

    assert job.status == JobStatus.COMPLETED
    assert len(job.revisions) >= 1
    changes = (output / job.job_id / "changes.md").read_text(encoding="utf-8")
    assert "Revised text." in changes
    assert "mention helper source" in changes
