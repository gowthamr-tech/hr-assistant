PYTHON ?= python3

.PHONY: install test run dev docker-up

# Local runs load .env like docker compose does (app/config.py reads only os.environ).
ENV_FILE := $(if $(wildcard .env),--env-file .env)

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 $(ENV_FILE)

dev:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 $(ENV_FILE) --reload

docker-up:
	docker compose up --build
