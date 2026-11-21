from crewai import Agent, Crew, LLM, Process, Task


def build_research_write_crew(llm: LLM, max_sec: int) -> Crew:
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
        max_execution_time=max_sec,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Turn research notes into a clear, well-structured final answer.",
        backstory="You are a precise technical writer who values clarity and brevity.",
        llm=llm,
        allow_delegation=False,
        max_iter=5,
        max_execution_time=max_sec,
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

    return Crew(
        agents=[researcher, writer],
        tasks=[research_task, write_task],
        process=Process.sequential,
        verbose=False,
    )
