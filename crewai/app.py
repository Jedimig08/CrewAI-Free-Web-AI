import json
import logging
import os
import re
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from crewai import LLM
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from crews import CREW_BUILDERS
from explainer.models import ExplainRequest
from explainer.pipeline import (
    ExplainerPipeline,
    PipelineConfig,
    elapsed_seconds,
    eta_seconds,
    job_directory,
    list_jobs,
    load_job,
    new_job,
    save_job,
)
from explainer.repo import RepoError, validate_repo_url

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("crewai-api")

TFG_BASE_URL = os.getenv("TFG_BASE_URL", "http://browser:3456/v1").rstrip("/")
TFG_API_KEY = os.getenv("TFG_API_KEY", "")
CREWAI_API_KEY = os.getenv("CREWAI_API_KEY", "")
CREWAI_MODEL = os.getenv("CREWAI_MODEL", "chatgpt-auto")
CREWAI_MAX_EXECUTION_SEC = int(os.getenv("CREWAI_MAX_EXECUTION_SEC", "300"))

EXPLAIN_OUTPUT_DIR = Path(os.getenv("EXPLAIN_OUTPUT_DIR", "/app/explanations"))
EXPLAIN_MAX_FILES = int(os.getenv("EXPLAIN_MAX_FILES", "150"))
EXPLAIN_MAX_FILE_BYTES = int(os.getenv("EXPLAIN_MAX_FILE_BYTES", "200000"))
EXPLAIN_CHUNK_LINES = int(os.getenv("EXPLAIN_CHUNK_LINES", "120"))
EXPLAIN_CHUNK_OVERLAP = int(os.getenv("EXPLAIN_CHUNK_OVERLAP", "10"))
EXPLAIN_MAX_CHUNKS = int(os.getenv("EXPLAIN_MAX_CHUNKS", "200"))
EXPLAIN_MAX_WORKERS = int(os.getenv("EXPLAIN_MAX_WORKERS", "4"))
EXPLAIN_JOB_TIMEOUT_SEC = int(os.getenv("EXPLAIN_JOB_TIMEOUT_SEC", "1800"))
EXPLAIN_CLONE_TIMEOUT_SEC = int(os.getenv("EXPLAIN_CLONE_TIMEOUT_SEC", "60"))
EXPLAIN_REFINE = os.getenv("EXPLAIN_REFINE", "true").lower() in {"1", "true", "yes"}
EXPLAIN_MAX_REFINE_ROUNDS = int(os.getenv("EXPLAIN_MAX_REFINE_ROUNDS", "1"))
EXPLAIN_LIVE_EVERY = max(1, int(os.getenv("EXPLAIN_LIVE_EVERY", "1")))
EXPLAIN_ALLOW_LOCAL = os.getenv("EXPLAIN_ALLOW_LOCAL", "false").lower() in {
    "1", "true", "yes"
}
EXPLAIN_ALLOWED_HOSTS = {
    host.strip()
    for host in os.getenv("EXPLAIN_ALLOWED_HOSTS", "").split(",")
    if host.strip()
}

if not TFG_API_KEY:
    raise RuntimeError("TFG_API_KEY is required")
if not CREWAI_API_KEY:
    raise RuntimeError("CREWAI_API_KEY is required")

app = FastAPI(title="CrewAI + Free Web AI", version="0.1.0")
bearer = HTTPBearer(auto_error=False)


def require_key(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> None:
    if creds is None or creds.credentials != CREWAI_API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


@lru_cache(maxsize=8)
def build_llm(model: str) -> LLM:
    logger.info("Creating LLM model=%s base_url=%s", model, TFG_BASE_URL)
    return LLM(
        model=model,
        custom_openai=True,
        base_url=TFG_BASE_URL,
        api_key=TFG_API_KEY,
        temperature=0.2,
        context_window_size=8192,
    )


def run_crew(
    prompt: str,
    model: str,
    crew_name: str = "research_write",
    llm_factory: Callable[[str], LLM] = build_llm,
) -> str:
    builder = CREW_BUILDERS.get(crew_name)
    if builder is None:
        raise ValueError(f"Unknown crew: {crew_name}")
    crew = builder(llm_factory(model), CREWAI_MAX_EXECUTION_SEC)
    return str(crew.kickoff(inputs={"prompt": prompt}))


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    model: str | None = None
    crew: str = "research_write"


def _fetch_gateway_models() -> dict[str, Any]:
    request = urllib.request.Request(
        f"{TFG_BASE_URL}/models",
        headers={"Authorization": f"Bearer {TFG_API_KEY}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "crewai",
        "model": CREWAI_MODEL,
        "gateway": TFG_BASE_URL,
    }


@app.get("/api/models", dependencies=[Depends(require_key)])
async def models() -> dict[str, Any]:
    try:
        return await run_in_threadpool(_fetch_gateway_models)
    except Exception as exc:
        logger.warning("Could not list gateway models: %s", exc)
        return {
            "object": "list",
            "data": [{"id": CREWAI_MODEL, "object": "model", "owned_by": "free-web-ai"}],
        }


@app.get("/api/crews", dependencies=[Depends(require_key)])
async def crews() -> dict[str, Any]:
    return {"crews": list(CREW_BUILDERS)}


@app.post("/api/crew/run", dependencies=[Depends(require_key)])
async def crew_run(req: RunRequest) -> dict[str, Any]:
    if req.crew not in CREW_BUILDERS:
        raise HTTPException(status_code=400, detail=f"Unknown crew: {req.crew}")
    model = req.model or CREWAI_MODEL
    logger.info("Running crew=%s model=%s prompt_chars=%d", req.crew, model, len(req.prompt))
    try:
        result = await run_in_threadpool(run_crew, req.prompt, model, req.crew)
    except Exception as exc:
        logger.exception("Crew run failed")
        raise HTTPException(status_code=502, detail=f"Crew run failed: {exc}") from exc
    return {"model": model, "result": result}


def _explain_config(req: ExplainRequest) -> PipelineConfig:
    return PipelineConfig(
        output_dir=EXPLAIN_OUTPUT_DIR,
        model=req.model or CREWAI_MODEL,
        max_file_bytes=EXPLAIN_MAX_FILE_BYTES,
        chunk_lines=EXPLAIN_CHUNK_LINES,
        chunk_overlap=EXPLAIN_CHUNK_OVERLAP,
        max_chunks=EXPLAIN_MAX_CHUNKS,
        max_workers=min(req.max_workers, EXPLAIN_MAX_WORKERS),
        agent_timeout_sec=CREWAI_MAX_EXECUTION_SEC,
        job_timeout_sec=EXPLAIN_JOB_TIMEOUT_SEC,
        clone_timeout_sec=EXPLAIN_CLONE_TIMEOUT_SEC,
        allowed_hosts=EXPLAIN_ALLOWED_HOSTS,
        allow_local=EXPLAIN_ALLOW_LOCAL,
        refine=EXPLAIN_REFINE,
        max_refine_rounds=EXPLAIN_MAX_REFINE_ROUNDS,
        live_every=EXPLAIN_LIVE_EVERY,
    )


def run_explain_job(job_id: str, req: ExplainRequest) -> None:
    job = load_job(EXPLAIN_OUTPUT_DIR, job_id)
    if job is None:
        logger.error("Explain job %s not found when starting", job_id)
        return
    ExplainerPipeline(_explain_config(req)).run(job, req)


@app.post("/api/explain", dependencies=[Depends(require_key)])
async def explain(req: ExplainRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    try:
        validate_repo_url(req.repo_url, EXPLAIN_ALLOWED_HOSTS, EXPLAIN_ALLOW_LOCAL)
    except RepoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job = new_job(req)
    save_job(EXPLAIN_OUTPUT_DIR, job)
    background_tasks.add_task(run_explain_job, job.job_id, req)
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "poll_url": f"/api/explain/{job.job_id}",
    }


@app.get("/api/explain", dependencies=[Depends(require_key)])
async def explain_jobs() -> dict[str, Any]:
    return {"jobs": list_jobs(EXPLAIN_OUTPUT_DIR)}


def _valid_job_id(job_id: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{32}", job_id))


@app.get("/api/explain/{job_id}", dependencies=[Depends(require_key)])
async def explain_status(job_id: str) -> dict[str, Any]:
    if not _valid_job_id(job_id):
        raise HTTPException(status_code=404, detail=f"Unknown explain job: {job_id}")
    job = load_job(EXPLAIN_OUTPUT_DIR, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown explain job: {job_id}")
    data = job.model_dump(mode="json")
    data["elapsed_seconds"] = elapsed_seconds(job)
    data["eta_seconds"] = eta_seconds(job)
    return data


@app.post("/api/explain/{job_id}/cancel", dependencies=[Depends(require_key)])
async def cancel_explain(job_id: str) -> dict[str, Any]:
    if not _valid_job_id(job_id):
        raise HTTPException(status_code=404, detail=f"Unknown explain job: {job_id}")
    job = load_job(EXPLAIN_OUTPUT_DIR, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown explain job: {job_id}")
    if job.status.value in {"completed", "failed", "partial", "cancelled"}:
        return {"job_id": job_id, "status": job.status.value}
    job.cancel_requested = True
    save_job(EXPLAIN_OUTPUT_DIR, job)
    return {"job_id": job_id, "status": "cancelling"}


@app.get(
    "/api/explain/{job_id}/files/{file_path:path}",
    dependencies=[Depends(require_key)],
)
async def explain_file(job_id: str, file_path: str) -> FileResponse:
    if not _valid_job_id(job_id):
        raise HTTPException(status_code=404, detail="File not found in this job")
    base = job_directory(EXPLAIN_OUTPUT_DIR, job_id).resolve()
    target = (base / file_path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found in this job")
    return FileResponse(target, media_type="text/markdown")
