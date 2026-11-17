# syntax=docker/dockerfile:1
#
# Stages:
#   base    - OS packages, runtime Python dependencies, baked tokenizer + embedding model
#   test    - base + dev dependencies + tests:   docker build --target test -t hr-assistant-test .
#   runtime - base + application code only (default target, what you deploy)

ARG PYTHON_IMAGE=python:3.11-slim-bookworm

FROM ${PYTHON_IMAGE} AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TIKTOKEN_CACHE_DIR=/opt/cache/tiktoken \
    EMBEDDING_CACHE_DIR=/opt/cache/fastembed \
    HF_HUB_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng util-linux \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged user for platforms that start the container as non-root (Kubernetes
# runAsUser: 10001). With plain `docker run`, the entrypoint picks the data owner instead.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /tmp --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Bake the tokenizer and the CPU embedding model into the image: start-up needs no network
# except the LLM API.
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" "$EMBEDDING_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && HF_HUB_OFFLINE=0 python -c "import os; from fastembed import TextEmbedding; \
TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=os.environ['EMBEDDING_CACHE_DIR'])" \
    && chmod -R a+rX /opt/cache


FROM base AS test
COPY requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY app ./app
COPY tests ./tests
COPY pytest.ini pyproject.toml ./
CMD ["python", "-m", "pytest", "-q"]


FROM base AS runtime
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="hr-assistant" \
      org.opencontainers.image.version="${APP_VERSION}"
ENV LOG_FORMAT=json

COPY app ./app
COPY docker-entrypoint.sh ./
RUN chmod 0755 docker-entrypoint.sh && chmod -R a+rX /app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=60 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status==200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
# One worker: the knowledge base lives in process memory and SQLite takes one writer.
# Scale with replicas (see docs/DEPLOYMENT.md). Our middleware writes the access log.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-access-log", "--no-server-header", "--timeout-graceful-shutdown", "30"]
