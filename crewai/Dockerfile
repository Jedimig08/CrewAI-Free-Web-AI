FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OTEL_SDK_DISABLED=true

WORKDIR /app

RUN useradd --create-home --uid 1000 crewai

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=crewai:crewai app.py .
COPY --chown=crewai:crewai crews ./crews

USER crewai

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
