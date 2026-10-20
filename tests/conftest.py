from __future__ import annotations

import shutil
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.db import repositories as repo
from app.db.database import connect, verify_and_migrate
from app.ingestion.pipeline import run_ingestion
from tests.fixtures import build_corpus, build_database

TODAY = date(2026, 10, 16)  # a Friday


def make_settings(root: Path, db_path: Path, **overrides) -> Settings:
    base = Settings(
        corpus_dir=str(root / "corpus"), manifest_path=str(root / "manifest.json"), db_path=str(db_path),
        app_today=TODAY, llm_provider="fake", llm_model="fake-model", llm_router_model="fake-router",
        llm_timeout_s=10, llm_max_retries=0, anthropic_effort="low", anthropic_refusal_fallback=False,
        embeddings_enabled=False, embedding_model="", log_level="INFO", log_file="", request_deadline_s=25,
    )
    return replace(base, **overrides)


@pytest.fixture(scope="session")
def corpus_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("data")
    build_corpus(root)
    build_database(root / "veltrix.db")
    verify_and_migrate(str(root / "veltrix.db"))
    return root


@pytest.fixture(scope="session")
def kb(corpus_root):
    return run_ingestion(str(corpus_root / "corpus"), str(corpus_root / "manifest.json"),
                         str(corpus_root / "veltrix.db"), embedder_factory=None)


@pytest.fixture
def db_path(corpus_root, tmp_path) -> Path:
    """A fresh copy of the fixture database for every test (mutations never leak)."""
    p = tmp_path / "veltrix.db"
    shutil.copy(corpus_root / "veltrix.db", p)
    return p


@pytest.fixture
def conn(db_path):
    c = connect(str(db_path))
    yield c
    c.close()


@pytest.fixture
def settings(corpus_root, db_path) -> Settings:
    return make_settings(corpus_root, db_path)


def employee(conn: sqlite3.Connection, emp_id: str):
    e = repo.get_employee(conn, emp_id)
    assert e is not None
    return e
