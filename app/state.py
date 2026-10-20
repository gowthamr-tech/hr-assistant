"""Process-wide service state, filled in once ingestion completes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from app.config import Settings

if TYPE_CHECKING:  # type hints only; importing these at runtime would pull in the whole app
    from app.ingestion.pipeline import KnowledgeBase
    from app.orchestration.orchestrator import Orchestrator


@dataclass
class ServiceState:
    settings: Optional[Settings] = None
    kb: Optional[KnowledgeBase] = None
    orchestrator: Optional[Orchestrator] = None
    ready: bool = False
    failed: bool = False


STATE = ServiceState()
