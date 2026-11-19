# One image, several roles (api, worker, ingest, chroma server, llm-stub, locust). The role is the container command.
# Build args:  WITH_ST=1 installs sentence-transformers and bakes EMBEDDING_MODEL into the image (optional).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONPATH=/app HF_HOME=/models
WORKDIR /app

COPY requirements.txt requirements-dev.txt requirements-embeddings.txt ./
RUN pip install -r requirements-dev.txt

ARG WITH_ST=0
ARG EMBEDDING_MODEL=
ENV EMBEDDING_MODEL=${EMBEDDING_MODEL}
COPY scripts/prefetch_model.py scripts/prefetch_model.py
RUN if [ "$WITH_ST" = "1" ]; then pip install -r requirements-embeddings.txt && python scripts/prefetch_model.py; fi

COPY app app
COPY stub stub
COPY frontend frontend
COPY corpus corpus
COPY loadtest loadtest
COPY scripts scripts
COPY monitoring monitoring
# tests ship in the image so they can run on hosts without redis-server (e.g. Windows): README section 5
COPY tests tests
COPY pytest.ini .env.example ./

RUN useradd --create-home --uid 10001 appuser && mkdir -p /data /models && chown -R appuser /app /data /models
USER appuser
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.api:get_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
