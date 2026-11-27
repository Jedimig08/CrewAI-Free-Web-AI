# Dev Plan: Repo Explainer (line-by-line + linked wiki)

Status: implemented. This file remains the design/dev note for the implementation.

## 1. Goal and user requirements

Add the ability to point the CrewAI service at a code repository and get a
generated explanation written to files.

- The LLM cannot see the whole repo at once. Feed it numbered chunks, preserving
  exact line numbers, then stitch the results back together.
- Preserve cross-file context: know where a variable/function came from so a
  later chunk that uses it can be explained correctly.
- Output must be Markdown files explaining everything, not just a chat reply.
- Start the explanation where it makes the most sense (entry point first),
  inspired by "explain the repo" websites (guided start-here tour, file tree,
  dependency map, click-through navigation).
- Two modes:
  1. `line_by_line` — walk the code with line references.
  2. `wiki` — an overview file plus linked files, so an unfamiliar function or
     file takes you to another file that explains it.
- Granularity is a separate option: `literal` (a note per physical line) or
  `blocks` (a note per logical block with `Lines X–Y` ranges).

## 2. Decisions locked in

| Topic | Decision |
|---|---|
| Repo input | Public git URL, cloned at runtime (`--depth 1`) |
| Output | API returns text AND writes files to a bind-mounted host dir |
| Wiki mode | Multiple linked Markdown files (overview + per-file + per-symbol) |
| Integration | New `/api/explain` endpoints + dedicated crew + deterministic Python tools |
| Execution | Async job with `job_id` and polling (avoids crew/proxy timeouts) |
| Granularity | Support both `literal` and `blocks` as an option |
| Refinement | Two passes: bottom-up chunk extraction, then global correlation + targeted revision (flag-controlled) |
| Concurrency | Parallel workers for chunk explanations and revisions, bounded by `max_workers` |
| Web UI | Not in scope (API-only unless explicitly requested) |

## 3. Architecture

New package under `crewai/`; the existing `crews/` auto-discovery is left alone.
The explain crew lives OUTSIDE `crews/` so it is not registered as a generic
crew in `/api/crews` and does not accept a plain prompt.

```
crewai/
├── Dockerfile               # + git
├── app.py                   # + /api/explain endpoints
├── requirements.txt         # + pytest (dev)
├── explainer/
│   ├── __init__.py
│   ├── models.py            # enums, pydantic request/status models
│   ├── repo.py              # URL validation, clone, file discovery + prioritization
│   ├── symbols.py           # symbol/variable index and provenance lookup
│   ├── chunker.py           # numbered-line chunks with exact ranges + overlap
│   ├── links.py             # shared deterministic slug/anchor generator
│   ├── refine.py            # correlation map + revision directives
│   ├── crew.py              # dedicated explain crew builders (chunk + integrator)
│   └── pipeline.py          # orchestration, progress, assembly, persistence
├── tests/
│   └── test_explainer.py
docker-compose.yml           # + output bind mount + env vars
.env.example · README.md · .gitignore
```

Key principle: Python does everything deterministic (walking, line numbers,
symbol index, link targets, file assembly); worker agents explain chunks in
parallel, each seeing only one chunk, and the integrator writes the small
overview. No single LLM call ever receives the repo.

Process bottom-up, present top-down: understanding is built from small units
upward (pass 1 → pass 2), while the final document is ordered entry-point first
so a reader starts in the right place.

## 4. Pipeline (one job)

1. `POST /api/explain` validates the body and enqueues via FastAPI
   `BackgroundTasks`; returns `{job_id, poll_url}` immediately.
2. Clone into a per-job temp dir:
   `git clone --depth 1 --single-branch --no-recurse-submodules`,
   `GIT_TERMINAL_PROMPT=0`, `subprocess.run(timeout=...)`.
   SSRF guard: http(s) only, reject embedded credentials, optional host
   allowlist, resolve DNS and reject private/loopback/link-local IPs. Tests can
   opt into `file://` with `EXPLAIN_ALLOW_LOCAL=true`.
3. Discover files with caps: skip binaries, `.git`, `node_modules`, `vendor`,
   lockfiles, images; cap file count/size.
4. Order files entry-point first (`main.py`, `app.py`, `__main__.py`, `index.ts`,
   `if __name__ == "__main__"`, framework handlers), then BFS over the import
   graph so files that are referenced early are explained early.
5. Build a compact symbol index (Python `ast` + regex for JS/TS):
   `name -> file:line`, imports, top-level assignments. Capped to fit context.
   This is how "where did this variable come from" is answered: for each chunk,
   inject the index entries for names the chunk references; unresolved names
   are labelled `[unresolved]` rather than guessed.
6. Chunk each file into ~120 numbered lines with ~10-line overlap. Each chunk
   is a record: `{file, start_line, end_line, text}` where text lines are
   prefixed `L123:`. Chunk char cap keeps prompt + chunk + symbol map under the
   8192-token LLM context (`context_window_size=8192`, `temperature=0.2`).
### Pass 1 — inside-out extraction (parallel fan-out)

7. Fan the chunks out across a bounded pool of `max_workers` workers (default 4).
   Each worker creates its own fresh crew and LLM instance — never share the
   cached `build_llm` singleton or mutable crew state across threads. A worker
   gets `numbered_code`, `file`, `line_start`, `line_end`, `symbol_context`,
   `granularity`, `mode` and returns a strict Markdown template with a
   deterministic anchor (`file:start_line`) PLUS a compact structured summary:
   purpose, symbols it defines/uses, dependencies, and anything marked
   `[unresolved]`. Results go into a dict keyed by anchor, so the final order is
   fixed regardless of completion order. A failed chunk becomes a placeholder
   and the job completes as `partial`; one worker's failure never cancels the
   others. Progress updates as each future completes.

### Pass 2 — correlation and revision (enable with `refine: true`)

8. Barrier: wait for all pass-1 futures (or the per-job timeout) before
   correlating. Then build a global correlation map from the compact summaries
   (not raw code): symbol index, file/import graph, per-chunk purposes,
   cross-references, and the list of unresolved/vague items. This is small
   enough to fit in one context window even when the repo does not.
9. One "integrator" LLM call reads that map and emits (a) the relationships
   between components and (b) a list of revision directives, e.g.
   "`app.py:40-160`: clarify that `TIMEOUT` is loaded from `config.py:12` at
   import time and overridden by env var X". It is told to be conservative and
   only revise chunks where the new context actually changes the explanation.
10. Re-run ONLY the affected chunks through the same worker pool in parallel,
    injecting the correlation map and their revision directives; replace those
    sections by anchor. At most `EXPLAIN_MAX_REFINE_ROUNDS` rounds, each with
    its own barrier. This is where earlier blind spots get fixed once the whole
    picture is known. With `refine: false`, skip 8–10 and use the pass-1 output
    directly.

### Assembly

11. Assemble deterministically in file/line order (never completion order; no
    giant final LLM call):
    - `line_by_line` → `line-by-line.md` with a start-here TOC and `Lines X–Y`
      sections. `literal` = one note per physical line; `blocks` = one note per
      logical block.
    - `wiki` → `overview.md` (purpose, start-here path, Mermaid dependency graph,
      TOC, and the correlation narrative from step 9) + `files/<path>.md` +
      `symbols/<name>.md`. All links use the shared slug function so every link
      target is guaranteed to exist.
12. Write `job.json` (status/progress/files/error/partial) plus outputs into
    `/app/explanations/<job_id>/`; return Markdown text + relative file paths;
    delete the cloned repo.

## 5. How integration works (the "big picture" problem)

No single LLM call sees the whole repo. The big picture is maintained outside
the LLM, as structured data, and delivered to it in small pieces.

- **Structured, not textual.** Python's symbol index plus the import graph *are*
  the whole-repo view. The LLM never needs to hold them in context.
- **Provenance packets.** For each chunk, Python finds the names the code
  references and injects only their index entries, e.g.
  `connect → db.py:8`, `TIMEOUT → config.py:12`. The agent can then explain a
  caller without seeing the callee. Names not in the index are marked
  `[unresolved]` instead of invented.
- **Continuity.** ~10-line overlap between chunks; each chunk carries its own
  `file` and `start_line`/`end_line` so line numbers never drift.
- **Shared anchors.** Both the LLM (in its output template) and Python (when
  assembling) derive links from `file:start_line`, so cross-links resolve even
  though no agent sees all outputs at once.
- **One compact global step.** Pass 2 feeds the integrator a map of *summaries*,
  not code, so it can see relationships and issue revision directives within the
  token budget.

Example: `config.py:12` defines `TIMEOUT`, `db.py:8` defines `connect()`, and
`app.py:45` uses both. The index records all three; the `app.py` chunk receives
the two provenance entries; the final wiki links that mention to
`symbols/connect.md` → `files/db.md` using the same index.

**Limitation:** this captures definitions and references, not runtime data flow
(e.g. a value mutated across three files). The revision pass improves coherence
but cannot fully reconstruct behaviour the source never states.

## 6. Concurrency model

- Fan-out/fan-in: pass 1 and each revision round are a map over chunks with a
  barrier between stages; the integrator runs once at the barrier.
- Bounded pool: `max_workers` per job (default `EXPLAIN_MAX_WORKERS=4`), with a
  global cap so concurrent explain jobs cannot overwhelm the backend.
- Isolation: every worker gets a fresh LLM and crew instance; no shared mutable
  conversation/session state. The cached `build_llm` singleton in `app.py` is
  for the normal crew endpoints only.
- Determinism: results are keyed by `file:start_line` anchor and assembled by
  file order + line number, so parallel completion never changes the output.
- Thread safety: progress counters and result writes are guarded by a lock;
  `job.json` is written atomically.
- Failure isolation: a failed or timed-out chunk becomes a placeholder; the
  rest of the batch still completes and the job is marked `partial`.
- Graceful degradation: if the configured LLM backend serializes requests,
  workers simply queue and correctness is unchanged — only the speedup is lost.

## 7. API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/explain` | `{repo_url, mode, granularity, refine?, max_workers?, max_files?, focus_paths?}` → `job_id`. Uses existing `require_key`. |
| `GET` | `/api/explain/{job_id}` | `{status, progress, partial, files, error, text}`; reads `job.json` from disk so polling survives restarts. |
| `GET` | `/api/explain/{job_id}/files/{path}` | `FileResponse`; path resolved and confined to the job dir. |
| `GET` | `/api/explain` | List known jobs (optional). |

Status lifecycle: `queued → cloning → indexing → explaining (n/total across w workers) → correlating → revising (m/k) → assembling → completed | failed | partial` (`correlating`/`revising` only when `refine: true`).

## 8. Output formats

`line-by-line.md`
```
# <repo> — line-by-line explanation
## Start here
1. [app.py — entry point](#app-py-1) ...
## app.py
### Lines 1–40
...
```

`overview.md` (wiki mode) — purpose, architecture summary, entry-point reading
order, Mermaid graph of file relations, and links to every detail page.

`files/<path>.md` — one page per source file: purpose, key symbols, annotated
blocks, links to symbol pages and related files.

`symbols/<name>.md` — one page per function/class/important variable: where it
is defined (`file:line`), what it does, who uses it, links back to file pages.

## 9. Security and limits

- http(s) only, no URL credentials, host allowlist, DNS/private-IP rejection.
- `GIT_TERMINAL_PROMPT=0`, no submodules, clone timeout.
- Caps: `EXPLAIN_MAX_FILES` 150, `EXPLAIN_MAX_FILE_BYTES` 200k,
  `EXPLAIN_CHUNK_LINES` 120, `EXPLAIN_CHUNK_OVERLAP` 10,
  `EXPLAIN_MAX_CHUNKS` 200, `EXPLAIN_MAX_WORKERS` 4,
  `EXPLAIN_MAX_TOTAL_WORKERS` 8, clone timeout 60s, job timeout 1800s.
- Output file serving confined to the job directory (no path traversal).
- Never log or persist tokens; jobs are public-repo only in this phase.
- Cleanup temp clones; keep only generated `.md` and metadata.

## 10. Docker / config changes

- `crewai/Dockerfile`: `apt-get install -y --no-install-recommends git`.
- `docker-compose.yml` crewai: bind mount `./explained-outputs:/app/explanations`;
  env `EXPLAIN_OUTPUT_DIR`, `EXPLAIN_MAX_FILES`, `EXPLAIN_MAX_FILE_BYTES`,
  `EXPLAIN_CHUNK_LINES`, `EXPLAIN_CHUNK_OVERLAP`, `EXPLAIN_MAX_CHUNKS`,
  `EXPLAIN_CLONE_TIMEOUT_SEC`, `EXPLAIN_JOB_TIMEOUT_SEC`,
  `EXPLAIN_ALLOWED_HOSTS`, `EXPLAIN_ALLOW_LOCAL`, `EXPLAIN_REFINE`,
  `EXPLAIN_MAX_REFINE_ROUNDS`, `EXPLAIN_MAX_WORKERS`,
  `EXPLAIN_MAX_TOTAL_WORKERS`.
- `.gitignore`: `explained-outputs/`. `README.md`: endpoints, modes, output
  layout, limits, and example curl requests.

## 11. Verification

- Unit tests (pytest, no network): URL/SSRF validation, chunk ranges + overlap,
  symbol provenance, entry-point prioritization, slug/link round-trip,
  correlation-map size cap, that revision directives only target known chunk
  anchors, deterministic assembly from out-of-order worker results, per-worker
  LLM/crew isolation, and thread-safe progress updates.
- Integration: `docker compose up -d --build crewai`; POST a small public repo;
  poll to `completed`; confirm `explained-outputs/<id>/` files exist and every
  wiki link target resolves; repeat for both modes and both granularities.
- `python -m compileall crewai` + pytest. No existing lint/typecheck config in
  this Python project, so nothing else to run unless one is added.

## 12. Future ideas / intentionally out of scope

- Web UI controls: repo URL field, mode/granularity selector, progress bar,
  download buttons (`web/index.html` + `web/app.js`).
- Private repos with token auth; per-user job isolation.
- SQLite job store instead of `job.json` for durable history/querying.
- Incremental re-explain on repo updates; caching symbol index across jobs.
- Additional deliverables inspired by explain-the-repo sites: Mermaid sequence
  diagrams per entry point, a "data flow" page, search across generated docs.
- Cost reporting per job (LLM calls/tokens), budget caps, model selection.
- `--focus-paths` to explain only a subtree of a large repo.

## 13. Open questions

- Default mode/granularity for the API when omitted (proposed: `wiki` + `blocks`).
- Default `max_workers` (proposed: 4, with a global cap of 8 across all jobs).
- Default for `refine` (proposed: `true`, with `EXPLAIN_MAX_REFINE_ROUNDS=1`, since it is the step that fixes earlier blind spots).
- Should `job.json` history be pruned automatically (retention policy)?
- Confirm whether the web UI should be included in the first implementation.
