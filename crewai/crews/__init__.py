import logging
from importlib import import_module
from pathlib import Path
from typing import Callable

from crewai import Crew, LLM

logger = logging.getLogger(__name__)

CREW_BUILDERS: dict[str, Callable[[LLM, int], Crew]] = {}

for path in sorted(Path(__file__).parent.glob("*.py")):
    if path.name.startswith("_"):
        continue

    module_name = path.stem
    try:
        module = import_module(f"{__name__}.{module_name}")
    except Exception:
        logger.exception("Failed to import crew module %s", module_name)
        continue

    crew_name = getattr(module, "CREW_NAME", module_name)
    builder = getattr(module, "CREW_BUILDER", None)
    if builder is None:
        candidates = [
            value
            for key, value in vars(module).items()
            if key.startswith("build_") and callable(value)
        ]
        if len(candidates) == 1:
            builder = candidates[0]

    if not callable(builder):
        logger.warning("Skipping crew module %s: no unique build_* builder found", module_name)
        continue

    if crew_name in CREW_BUILDERS:
        logger.warning(
            "Skipping crew module %s: duplicate crew name %r", module_name, crew_name
        )
        continue

    CREW_BUILDERS[crew_name] = builder
    logger.info("Registered crew %r from %s", crew_name, path.name)

__all__ = ["CREW_BUILDERS"]
