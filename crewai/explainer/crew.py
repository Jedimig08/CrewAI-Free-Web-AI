from __future__ import annotations

import os

LITERAL_RULES = (
    "Emit one bullet for EVERY physical line, exactly in the form: - L45: explanation. "
    "Include blank lines as: - L45: blank line."
)

BLOCKS_RULES = (
    "Group consecutive lines into logical blocks. For each block emit a heading exactly "
    "as: ### Lines 45-60, followed by a short paragraph explaining that block."
)

CHUNK_TASK_TEMPLATE = """\
Explain one code chunk for a developer who cannot see the rest of the repository.

FILE: {file}
LINES: {line_start}-{line_end}
MODE: {mode}

NUMBERED CODE:
{numbered_code}

{symbol_context}

REVISION CONTEXT:
{revision_context}

FORMATTING RULES:
{granularity_rules}
- Keep every line number exactly as shown. Never invent line numbers.
- If a name is not covered by the symbol context and you cannot infer it from the code, list it as unresolved.
- Do not claim behaviour the code does not show.
- Output ONLY the template below, with no preamble or closing remarks.

ANCHOR: {anchor}
## {file} lines {line_start}-{line_end}
Purpose: one sentence.
Notes:
SUMMARY: purpose=...; defines=...; uses=...; unresolved=...
"""

INTEGRATOR_TASK_TEMPLATE = """\
You are integrating explanations of many code chunks into one coherent view.
You see compact summaries, not source code. Use the map to understand how parts relate.

MODE: {mode}

MAP:
{correlation_map}

Output ONLY the template below, with no preamble or closing remarks.
RELATIONSHIPS:
- one relationship per line, plain text
REVISIONS:
- anchor | one concrete revision instruction
Use an anchor exactly as written in the map. Write none if there are no revisions. Only revise where the map reveals something the original chunk could not know.
"""


def _crewai():
    from crewai import Agent, Crew, LLM, Process, Task

    return Agent, Crew, LLM, Process, Task


def create_llm(model: str):
    LLM = _crewai()[2]
    return LLM(
        model=model,
        custom_openai=True,
        base_url=os.getenv("TFG_BASE_URL", "http://browser:3456/v1").rstrip("/"),
        api_key=os.getenv("TFG_API_KEY", ""),
        temperature=0.2,
        context_window_size=8192,
    )


def build_chunk_crew(llm, max_sec: int):
    Agent, Crew, _LLM, Process, Task = _crewai()
    analyst = Agent(
        role="Code Analyst",
        goal="Explain one numbered code chunk precisely, using only what the chunk and symbol context show.",
        backstory=(
            "You are a patient code reader. You never lose line numbers, never guess "
            "about unseen code, and always follow the requested output template."
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3,
        max_execution_time=max_sec,
        verbose=False,
    )
    task = Task(
        description=CHUNK_TASK_TEMPLATE,
        expected_output="The exact Markdown template with an anchor line and a SUMMARY line.",
        agent=analyst,
    )
    return Crew(
        agents=[analyst],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )


def build_integrator_crew(llm, max_sec: int):
    Agent, Crew, _LLM, Process, Task = _crewai()
    integrator = Agent(
        role="Integration Analyst",
        goal="Find cross-file relationships and identify explanations that need revision.",
        backstory=(
            "You see the whole repository only as compact summaries. You are conservative "
            "and only request revisions where the global view changes an earlier explanation."
        ),
        llm=llm,
        allow_delegation=False,
        max_iter=3,
        max_execution_time=max_sec,
        verbose=False,
    )
    task = Task(
        description=INTEGRATOR_TASK_TEMPLATE,
        expected_output="The exact RELATIONSHIPS and REVISIONS template.",
        agent=integrator,
    )
    return Crew(
        agents=[integrator],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
