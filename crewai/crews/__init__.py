from typing import Callable

from crewai import Crew, LLM

from .research_write import build_research_write_crew
from .summarize import build_summarize_crew

CREW_BUILDERS: dict[str, Callable[[LLM, int], Crew]] = {
    "research_write": build_research_write_crew,
    "summarize": build_summarize_crew,
}

__all__ = ["CREW_BUILDERS", "build_research_write_crew", "build_summarize_crew"]
