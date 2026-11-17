PYTHON ?= python3
IMAGE ?= hr-assistant

.PHONY: install test lint check run dev docker-up docker-prod docker-test

# Local runs load .env like docker compose does (app/config.py reads only os.environ).
ENV_FILE := $(if $(wildcard .env),--env-file .env)

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check .

check: lint test

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 $(ENV_FILE)

dev:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 $(ENV_FILE) --reload

docker-up:
	docker compose up --build

docker-prod:
	docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build

docker-test:
	docker build --target test -t $(IMAGE)-test . && docker run --rm $(IMAGE)-test
