import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.models import RecipeData


@dataclass
class ImportJob:
    id: str
    recipe_url: str
    status: str = "queued"
    message: str = "In coda"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    recipe_data: Optional[RecipeData] = None
    error: Optional[str] = None


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, recipe_url: str) -> ImportJob:
        async with self._lock:
            job = ImportJob(id=str(uuid.uuid4()), recipe_url=recipe_url)
            self._jobs[job.id] = job
            return job

    async def get(self, job_id: str) -> Optional[ImportJob]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def list_recent(self, limit: int = 10) -> list[ImportJob]:
        async with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return jobs[:limit]

    async def update(
        self,
        job_id: str,
        *,
        status: Optional[str] = None,
        message: Optional[str] = None,
        recipe_data: Optional[RecipeData] = None,
        error: Optional[str] = None,
    ) -> Optional[ImportJob]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if status is not None:
                job.status = status
            if message is not None:
                job.message = message
            if recipe_data is not None:
                job.recipe_data = recipe_data
            if error is not None:
                job.error = error
            job.updated_at = datetime.now(timezone.utc)
            return job
