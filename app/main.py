import logging
import traceback
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.job_store import InMemoryJobStore
from app.services.recipe_service import RecipeExtractionError, extract_recipe


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("monsieur_app.web")

app = FastAPI(title="MonsieurAPP")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_store = InMemoryJobStore()


def format_step_minutes(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "-"
    return str(max(1, round(duration_seconds / 60)))


def build_ingredients_text(recipe) -> str:
    return "\n".join(recipe.ingredients)


def build_steps_text(recipe) -> str:
    blocks = []
    for index, step in enumerate(recipe.steps, start=1):
        blocks.append(
            "\n".join(
                [
                    f"Step {index}",
                    f"Descrizione: {step.description}",
                    f"Tempo: {format_step_minutes(step.duration_seconds)} min",
                    f"Temperatura: {step.temperature_c if step.temperature_c is not None else '-'} C",
                    f"Velocita: {step.speed if step.speed else '-'}",
                    f"Reverse: {'Si' if step.reverse else 'No'}",
                ]
            )
        )
    return "\n\n".join(blocks)


def build_mc_steps_text(recipe) -> str:
    lines = []
    for index, step in enumerate(recipe.steps, start=1):
        lines.extend(
            [
                f"STEP {index}",
                f"Titolo/Descrizione: {step.description}",
                f"Tempo (min): {format_step_minutes(step.duration_seconds)}",
                f"Temperatura (C): {step.temperature_c if step.temperature_c is not None else '-'}",
                f"Velocita: {step.speed if step.speed else '-'}",
                f"Reverse: {'Si' if step.reverse else 'No'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


async def run_extract_job(job_id: str, recipe_url: str) -> None:
    await job_store.update(job_id, status="extracting", message="Estrazione ricetta in corso")
    try:
        recipe = await extract_recipe(recipe_url)
        await job_store.update(
            job_id,
            status="completed",
            message="Ricetta estratta e pronta da copiare su Monsieur Cuisine",
            recipe_data=recipe,
            recipe_json=recipe.to_json(),
            ingredients_text=build_ingredients_text(recipe),
            steps_text=build_steps_text(recipe),
            mc_steps_text=build_mc_steps_text(recipe),
        )
    except RecipeExtractionError as exc:
        LOGGER.exception("Errore di dominio nel job %s", job_id)
        await job_store.update(
            job_id,
            status="failed",
            message="Estrazione fallita",
            error=str(exc) or repr(exc),
            debug_traceback=traceback.format_exc(),
        )
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Errore inatteso nel job %s", job_id)
        await job_store.update(
            job_id,
            status="failed",
            message="Errore inatteso",
            error=str(exc) or repr(exc),
            debug_traceback=traceback.format_exc(),
        )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    jobs = await job_store.list_recent()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
        },
    )


@app.post("/imports", response_class=HTMLResponse)
async def create_import(
    recipe_url: str = Form(...),
) -> HTMLResponse:
    job = await job_store.create(recipe_url)
    import asyncio
    asyncio.create_task(run_extract_job(job.id, recipe_url))
    return RedirectResponse(url=f"/imports/{job.id}", status_code=303)


@app.get("/imports/{job_id}", response_class=HTMLResponse)
async def import_detail(request: Request, job_id: str) -> HTMLResponse:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return templates.TemplateResponse(request, "import_detail.html", {"job": job})


@app.get("/partials/jobs/{job_id}", response_class=HTMLResponse)
async def job_status_partial(request: Request, job_id: str) -> HTMLResponse:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return templates.TemplateResponse(request, "partials/job_status.html", {"job": job})


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
