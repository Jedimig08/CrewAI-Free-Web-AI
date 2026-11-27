from crewai import Agent, Crew, LLM, Process, Task


def build_summarize_crew(llm: LLM, max_sec: int) -> Crew:
    summarizer = Agent(
        role="Summarizer",
        goal="Condense the user's request into a short, clear summary.",
        backstory="You are concise and precise, and you never add unsupported details.",
        llm=llm,
        allow_delegation=False,
        max_iter=3,
        max_execution_time=max_sec,
        verbose=False,
    )
    task = Task(
        description=(
            "Summarize the following request in as many sentences as needed without losing any information. "
            "Do not add anything that is not in the request.\n\nRequest:\n{prompt}"
        ),
        expected_output="A short summary in Markdown.",
        agent=summarizer,
    )
    return Crew(
        agents=[summarizer],
        tasks=[task],
        process=Process.sequential,
        verbose=False,
    )
