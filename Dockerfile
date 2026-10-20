FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TIKTOKEN_CACHE_DIR=/opt/cache/tiktoken \
    EMBEDDING_CACHE_DIR=/opt/cache/fastembed \
    HF_HUB_OFFLINE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements-dev.txt

# Bake the tokenizer and the CPU embedding model into the image: start-up needs no network
# except the LLM API.
RUN mkdir -p "$TIKTOKEN_CACHE_DIR" "$EMBEDDING_CACHE_DIR" \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && HF_HUB_OFFLINE=0 python -c "import os; from fastembed import TextEmbedding; \
TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir=os.environ['EMBEDDING_CACHE_DIR'])" \
    && chmod -R a+rX /opt/cache

COPY app ./app
COPY tests ./tests
COPY pytest.ini Makefile docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=60 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
