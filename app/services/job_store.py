import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app.models import RecipeAdaptation, RecipeData


@dataclass
class ImportJob:
    id: str
    recipe_url: str
    desired_servings: Optional[int] = None
    status: str = "queued"
    message: str = "In coda per l'estrazione"
    selected_review_mode: str = "adapted"
    confirmed_at: Optional[datetime] = None
    source_servings: Optional[int] = None
    scaling_factor: Optional[float] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    recipe_data: Optional[RecipeData] = None
    adaptation: Optional[RecipeAdaptation] = None
    error: Optional[str] = None
    debug_files: list[str] = field(default_factory=list)
    debug_traceback: Optional[str] = None
    recipe_json: Optional[str] = None
    adaptation_json: Optional[str] = None
    manual_export_payload: Optional[dict[str, object]] = None
    manual_export_payload_json: Optional[str] = None
    ingredients_text: Optional[str] = None
    ingredients_guide_text: Optional[str] = None
    scaled_ingredients_text: Optional[str] = None
    scaled_ingredients_guide_text: Optional[str] = None
    steps_text: Optional[str] = None
    mc_steps_text: Optional[str] = None
    adapted_steps_text: Optional[str] = None


class InMemoryJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}
        self._lock = asyncio.Lock()

    async def create(self, recipe_url: str, desired_servings: Optional[int] = None) -> ImportJob:
        async with self._lock:
            job = ImportJob(id=str(uuid.uuid4()), recipe_url=recipe_url, desired_servings=desired_servings)
            self._jobs[job.id] = job
            return job

    async def get(self, job_id: str) -> Optional[ImportJob]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def list_recent(self, limit: int = 10) -> list[ImportJob]:
        async with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return jobs[:limit]

    async def latest_confirmed(self) -> Optional[ImportJob]:
        async with self._lock:
            confirmed = [job for job in self._jobs.values() if job.confirmed_at is not None]
            confirmed.sort(key=lambda item: item.confirmed_at or item.updated_at, reverse=True)
            return confirmed[0] if confirmed else None

    async def update(
        self,
        job_id: str,
        *,
        desired_servings: Optional[int] = None,
        status: Optional[str] = None,
        message: Optional[str] = None,
        selected_review_mode: Optional[str] = None,
        confirmed_at: Optional[datetime] = None,
        source_servings: Optional[int] = None,
        scaling_factor: Optional[float] = None,
        recipe_data: Optional[RecipeData] = None,
        adaptation: Optional[RecipeAdaptation] = None,
        error: Optional[str] = None,
        debug_files: Optional[list[str]] = None,
        debug_traceback: Optional[str] = None,
        recipe_json: Optional[str] = None,
        adaptation_json: Optional[str] = None,
        manual_export_payload: Optional[dict[str, object]] = None,
        manual_export_payload_json: Optional[str] = None,
        ingredients_text: Optional[str] = None,
        ingredients_guide_text: Optional[str] = None,
        scaled_ingredients_text: Optional[str] = None,
        scaled_ingredients_guide_text: Optional[str] = None,
        steps_text: Optional[str] = None,
        mc_steps_text: Optional[str] = None,
        adapted_steps_text: Optional[str] = None,
    ) -> Optional[ImportJob]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if desired_servings is not None:
                job.desired_servings = desired_servings
            if status is not None:
                job.status = status
            if message is not None:
                job.message = message
            if selected_review_mode is not None:
                job.selected_review_mode = selected_review_mode
            if confirmed_at is not None:
                job.confirmed_at = confirmed_at
            if source_servings is not None:
                job.source_servings = source_servings
            if scaling_factor is not None:
                job.scaling_factor = scaling_factor
            if recipe_data is not None:
                job.recipe_data = recipe_data
            if adaptation is not None:
                job.adaptation = adaptation
            if error is not None:
                job.error = error
            if debug_files is not None:
                job.debug_files = debug_files
            if debug_traceback is not None:
                job.debug_traceback = debug_traceback
            if recipe_json is not None:
                job.recipe_json = recipe_json
            if adaptation_json is not None:
                job.adaptation_json = adaptation_json
            if manual_export_payload is not None:
                job.manual_export_payload = manual_export_payload
            if manual_export_payload_json is not None:
                job.manual_export_payload_json = manual_export_payload_json
            if ingredients_text is not None:
                job.ingredients_text = ingredients_text
            if ingredients_guide_text is not None:
                job.ingredients_guide_text = ingredients_guide_text
            if scaled_ingredients_text is not None:
                job.scaled_ingredients_text = scaled_ingredients_text
            if scaled_ingredients_guide_text is not None:
                job.scaled_ingredients_guide_text = scaled_ingredients_guide_text
            if steps_text is not None:
                job.steps_text = steps_text
            if mc_steps_text is not None:
                job.mc_steps_text = mc_steps_text
            if adapted_steps_text is not None:
                job.adapted_steps_text = adapted_steps_text
            job.updated_at = datetime.now(timezone.utc)
            return job
