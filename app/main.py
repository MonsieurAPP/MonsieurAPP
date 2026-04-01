import asyncio
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.services.job_store import InMemoryJobStore
from app.services.monsieur_cuisine import LoginError, MonsieurCuisineUploader, UploadError
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


def masked_username(username: str) -> str:
    if not username or "@" not in username:
        return "Account configurato"
    name, domain = username.split("@", 1)
    visible = name[:2] if len(name) > 2 else name[:1]
    return f"{visible}***@{domain}"


async def run_import_job(job_id: str, recipe_url: str, username: str, password: str) -> None:
    await job_store.update(job_id, status="extracting", message="Estrazione ricetta in corso")
    try:
        recipe = await extract_recipe(recipe_url)
        await job_store.update(
            job_id,
            status="uploading",
            message=f"Ricetta estratta: {recipe.title}. Upload su Monsieur Cuisine in corso",
            recipe_data=recipe,
        )

        async with MonsieurCuisineUploader(headless=os.getenv("HEADLESS", "1") == "1") as uploader:
            await uploader.login_lidl(username=username, password=password)
            await job_store.update(job_id, status="uploading", message="Login completato, compilazione form in corso")
            await uploader.upload_to_mc(recipe)

        await job_store.update(job_id, status="completed", message="Import completato con successo")
    except (RecipeExtractionError, LoginError, UploadError) as exc:
        LOGGER.exception("Errore di dominio nel job %s", job_id)
        await job_store.update(job_id, status="failed", message="Import fallito", error=str(exc))
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Errore inatteso nel job %s", job_id)
        await job_store.update(job_id, status="failed", message="Errore inatteso", error=str(exc))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request) -> HTMLResponse:
    jobs = await job_store.list_recent()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "default_username": os.getenv("LIDL_USERNAME", ""),
            "render_environment": os.getenv("RENDER", ""),
            "masked_username": masked_username(os.getenv("LIDL_USERNAME", "")),
        },
    )


@app.post("/imports", response_class=HTMLResponse)
async def create_import(
    recipe_url: str = Form(...),
    username: str = Form(""),
    password: str = Form(""),
) -> HTMLResponse:
    effective_username = username or os.getenv("LIDL_USERNAME", "")
    effective_password = password or os.getenv("LIDL_PASSWORD", "")

    if not effective_username or not effective_password:
        raise HTTPException(
            status_code=400,
            detail="Credenziali mancanti. Inseriscile nel form o configura LIDL_USERNAME e LIDL_PASSWORD.",
        )

    job = await job_store.create(recipe_url)
    asyncio.create_task(run_import_job(job.id, recipe_url, effective_username, effective_password))
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
