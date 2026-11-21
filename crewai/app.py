import json
import logging
import os
import urllib.request
from functools import lru_cache
from typing import Any

from crewai import Agent, Crew, LLM, Process, Task
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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


def _run_crew(prompt: str, model: str) -> str:
    llm = build_llm(model)

    researcher = Agent(
        role="Researcher",
        goal="Produce concise, accurate notes that answer the user's request.",
        backstory=(
            "You are a meticulous researcher. You only state facts you can support "
            "and keep notes short."
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=5,
        max_execution_time=CREWAI_MAX_EXECUTION_SEC,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Turn research notes into a clear, well-structured final answer.",
        backstory="You are a precise technical writer who values clarity and brevity.",
        llm=llm,
        allow_delegation=False,
        max_iter=5,
        max_execution_time=CREWAI_MAX_EXECUTION_SEC,
        verbose=False,
    )

    research_task = Task(
        description=(
            "Research the following request and produce concise notes. "
            "Do not write the final answer yet.\n\nRequest:\n{prompt}"
        ),
        expected_output="A short bullet list of accurate notes.",
        agent=researcher,
    )
    write_task = Task(
        description="Using the research notes, write the final answer to the original request.",
        expected_output="A clear, complete answer in Markdown.",
        agent=writer,
        context=[research_task],
    )

    crew = Crew(
        agents=[researcher, writer],
        tasks=[research_task, write_task],
        process=Process.sequential,
        verbose=False,
    )
    result = crew.kickoff(inputs={"prompt": prompt})
    return str(result)


class RunRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=20000)
    model: str | None = None


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


@app.post("/api/crew/run", dependencies=[Depends(require_key)])
async def crew_run(req: RunRequest) -> dict[str, Any]:
    model = req.model or CREWAI_MODEL
    logger.info("Running crew model=%s prompt_chars=%d", model, len(req.prompt))
    try:
        result = await run_in_threadpool(_run_crew, req.prompt, model)
    except Exception as exc:
        logger.exception("Crew run failed")
        raise HTTPException(status_code=502, detail=f"Crew run failed: {exc}") from exc
    return {"model": model, "result": result}
