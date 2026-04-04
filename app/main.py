import logging
import math
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from app.services.job_store import InMemoryJobStore
from app.services.adaptation_prompt_store import (
    get_default_adaptation_prompt_template,
    load_adaptation_prompt_template,
    save_adaptation_prompt_template,
)
from app.services.recipe_adaptation import build_recipe_adaptation
from app.services.recipe_service import RecipeExtractionError, extract_recipe


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

load_dotenv(BASE_DIR / ".env.local")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("monsieur_app.web")

app = FastAPI(title="MonsieurAPP")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
job_store = InMemoryJobStore()

STEP_ENVIRONMENT_LABELS = {
    "mc": "Monsieur Cuisine",
    "external": "Manuale / Esterno",
    "transition": "Transizione boccale",
}


def serialize_adapted_step(step) -> dict[str, object]:
    return {
        "environment": getattr(step, "environment", "mc"),
        "description": step.description,
        "program": step.program,
        "parametersSummary": step.parameters_summary,
        "accessory": step.accessory,
        "detailedInstructions": step.detailed_instructions,
        "durationSeconds": step.duration_seconds,
        "temperatureC": step.temperature_c,
        "speed": step.speed,
        "reverse": step.reverse,
        "confidence": step.confidence,
        "rationale": step.rationale,
        "sourceRefs": step.source_refs,
    }


def build_adaptation_step_groups(adaptation) -> dict[str, list[dict[str, object]]]:
    grouped = {"mc": [], "external": [], "transition": []}
    if not adaptation:
        return grouped

    for order, step in enumerate(adaptation.adapted_steps, start=1):
        environment = getattr(step, "environment", "mc") or "mc"
        grouped.setdefault(environment, []).append(
            {
                "order": order,
                "environment_label": STEP_ENVIRONMENT_LABELS.get(environment, environment),
                "step": step,
            }
        )
    return grouped


def build_adaptation_timeline_steps(adaptation) -> list[dict[str, object]]:
    if not adaptation:
        return []

    timeline = []
    for order, step in enumerate(adaptation.adapted_steps, start=1):
        environment = getattr(step, "environment", "mc") or "mc"
        badge_label = step.program if step.program else "ESTERNO"
        is_machine_step = bool(step.program)
        timeline.append(
            {
                "order": order,
                "step": step,
                "environment": environment,
                "environment_label": STEP_ENVIRONMENT_LABELS.get(environment, environment),
                "badge_label": badge_label,
                "badge_status": "completed" if is_machine_step else "queued",
                "kind_label": "Fase" if is_machine_step else "Azione",
                "secondary_label": "Programma Monsieur Cuisine" if is_machine_step else "Tipo",
                "secondary_value": step.program or "-" if is_machine_step else (step.parameters_summary or STEP_ENVIRONMENT_LABELS.get(environment, environment)),
                "is_machine_step": is_machine_step,
            }
        )
    return timeline


def build_job_template_context(job, *, prompt_saved: bool = False, prompt_error: str | None = None) -> dict[str, object]:
    grouped_steps = build_adaptation_step_groups(job.adaptation if job else None)
    return {
        "job": job,
        "prompt_template": load_adaptation_prompt_template(),
        "default_prompt_template": get_default_adaptation_prompt_template(),
        "prompt_saved": prompt_saved,
        "prompt_error": prompt_error,
        "adapted_mc_steps": grouped_steps["mc"],
        "adapted_external_steps": grouped_steps["external"],
        "adapted_transition_steps": grouped_steps["transition"],
        "adapted_timeline_steps": build_adaptation_timeline_steps(job.adaptation if job else None),
    }


def format_step_minutes(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "-"
    return str(max(1, round(duration_seconds / 60)))


def build_ingredients_text(recipe) -> str:
    return "\n".join(recipe.ingredients)


def build_ingredients_guide_text(recipe) -> str:
    lines = []
    for index, ingredient in enumerate(recipe.structured_ingredients, start=1):
        lines.extend(
            [
                f"Ingrediente {index}",
                f"Quantita: {ingredient.quantity or '-'}",
                f"Unita: {ingredient.unit or '-'}",
                f"Nome: {ingredient.name}",
                f"Note: {ingredient.notes or '-'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def parse_servings_count(value: str | None) -> int | None:
    if not value:
        return None
    text_match = re.search(r"(\d+)", value)
    if not text_match:
        return None
    return int(text_match.group(1))


def parse_fractional_quantity(value: str | None) -> float | None:
    if not value:
        return None
    normalized = value.strip().lower().replace(",", ".")
    word_map = {
        "un": 1.0,
        "una": 1.0,
        "uno": 1.0,
        "mezzo": 0.5,
        "mezza": 0.5,
    }
    if normalized in word_map:
        return word_map[normalized]
    if " " in normalized and "/" in normalized:
        integer_part, fraction_part = normalized.split(" ", 1)
        try:
            numerator, denominator = fraction_part.split("/", 1)
            return float(integer_part) + (float(numerator) / float(denominator))
        except (ValueError, ZeroDivisionError):
            return None
    if "/" in normalized:
        try:
            numerator, denominator = normalized.split("/", 1)
            return float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(normalized)
    except ValueError:
        return None


def format_scaled_quantity(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return str(int(round(value)))
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def build_scaled_ingredients(recipe, desired_servings: int | None) -> tuple[str | None, str | None, int | None, float | None]:
    source_servings = parse_servings_count(recipe.yield_text)
    if not desired_servings or not source_servings or desired_servings <= 0 or source_servings <= 0:
        return None, None, source_servings, None

    scaling_factor = desired_servings / source_servings
    scaled_lines = []
    guide_lines = []
    for index, ingredient in enumerate(recipe.structured_ingredients, start=1):
        scaled_quantity = None
        parsed_quantity = parse_fractional_quantity(ingredient.quantity)
        if parsed_quantity is not None:
            scaled_quantity = format_scaled_quantity(parsed_quantity * scaling_factor)

        scaled_line = " ".join(
            part for part in [scaled_quantity or ingredient.quantity, ingredient.unit, ingredient.name] if part
        ).strip()
        if ingredient.notes:
            scaled_line = f"{scaled_line} ({ingredient.notes})" if scaled_line else ingredient.notes
        scaled_lines.append(scaled_line or ingredient.original_text)

        guide_lines.extend(
            [
                f"Ingrediente {index}",
                f"Quantita originale: {ingredient.quantity or '-'}",
                f"Quantita adattata: {scaled_quantity or '-'}",
                f"Unita: {ingredient.unit or '-'}",
                f"Nome: {ingredient.name}",
                f"Note: {ingredient.notes or '-'}",
                "",
            ]
        )

    return "\n".join(scaled_lines).strip(), "\n".join(guide_lines).strip(), source_servings, scaling_factor


def build_scaled_ingredient_entry(ingredient, scaling_factor: float | None) -> dict[str, str | None]:
    scaled_quantity = None
    parsed_quantity = parse_fractional_quantity(ingredient.quantity)
    if scaling_factor and parsed_quantity is not None:
        scaled_quantity = format_scaled_quantity(parsed_quantity * scaling_factor)

    final_quantity = scaled_quantity or ingredient.quantity
    final_text = " ".join(part for part in [final_quantity, ingredient.unit, ingredient.name] if part).strip()
    if ingredient.notes:
        final_text = f"{final_text} ({ingredient.notes})" if final_text else ingredient.notes

    return {
        "originalText": ingredient.original_text,
        "name": ingredient.name,
        "quantity": ingredient.quantity,
        "unit": ingredient.unit,
        "notes": ingredient.notes,
        "scaledQuantity": scaled_quantity,
        "finalQuantity": final_quantity,
        "finalText": final_text or ingredient.original_text,
    }


def build_export_payload(job, mode: str = "selected") -> dict[str, object]:
    if not job.recipe_data:
        raise HTTPException(status_code=409, detail="Ricetta non ancora disponibile")
    if job.confirmed_at is None:
        raise HTTPException(status_code=409, detail="Ricetta non ancora confermata")

    export_mode = "adapted" if mode == "selected" else mode
    if export_mode not in {"original", "adapted"}:
        raise HTTPException(status_code=400, detail="Modalita' export non valida")
    if export_mode == "adapted" and not job.adaptation:
        raise HTTPException(status_code=409, detail="Adattamento non disponibile")

    structured_ingredients = [
        build_scaled_ingredient_entry(ingredient, job.scaling_factor)
        for ingredient in job.recipe_data.structured_ingredients
    ]
    ingredient_lines = [item["finalText"] for item in structured_ingredients] or list(job.recipe_data.ingredients)

    if export_mode == "adapted" and job.adaptation:
        timeline_steps = [serialize_adapted_step(step) for step in job.adaptation.adapted_steps]
        steps = [step for step in timeline_steps if step["environment"] == "mc"]
        manual_steps = [step for step in timeline_steps if step["environment"] == "external"]
        transition_steps = [step for step in timeline_steps if step["environment"] == "transition"]
    else:
        steps = [
            {
                "environment": "mc",
                "description": step.description,
                "program": None,
                "parametersSummary": None,
                "accessory": None,
                "detailedInstructions": step.description,
                "durationSeconds": step.duration_seconds,
                "temperatureC": step.temperature_c,
                "speed": step.speed,
                "reverse": step.reverse,
                "confidence": None,
                "rationale": None,
                "sourceRefs": None,
            }
            for step in job.recipe_data.steps
        ]
        manual_steps = []
        transition_steps = []
        timeline_steps = list(steps)

    return {
        "jobId": job.id,
        "sourceUrl": job.recipe_data.source_url,
        "sourceSite": job.recipe_data.source_site,
        "title": job.recipe_data.title,
        "ingredients": ingredient_lines,
        "structuredIngredients": structured_ingredients,
        "steps": steps,
        "manualSteps": manual_steps,
        "transitionSteps": transition_steps,
        "operationalTimeline": timeline_steps,
        "yieldText": str(job.desired_servings) if job.desired_servings else job.recipe_data.yield_text,
        "sourceYieldText": job.recipe_data.yield_text,
        "desiredServings": job.desired_servings,
        "sourceServings": job.source_servings,
        "totalTimeMinutes": job.recipe_data.total_time_minutes,
        "selectedReviewMode": job.selected_review_mode,
        "confirmedAt": job.confirmed_at.isoformat() if job.confirmed_at else None,
        "exportMode": export_mode,
        "adaptationMode": job.adaptation.mode if job.adaptation else None,
        "finalNote": job.adaptation.final_note if job.adaptation else None,
        "warnings": job.adaptation.warnings if job.adaptation else [],
    }


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


def build_adapted_steps_text(adaptation) -> str:
    lines = []
    for index, step in enumerate(adaptation.adapted_steps, start=1):
        environment = getattr(step, "environment", "mc")
        lines.extend(
            [
                f"STEP {index}",
                f"Ambiente: {STEP_ENVIRONMENT_LABELS.get(environment, environment)}",
                f"Fase: {step.description}",
                f"Programma: {step.program or '-'}",
                f"Programma/Parametri: {step.parameters_summary or '-'}",
                f"Accessorio: {step.accessory or '-'}",
                f"Istruzioni dettagliate: {step.detailed_instructions or step.description}",
                f"Tempo (min): {format_step_minutes(step.duration_seconds)}",
                f"Temperatura (C): {step.temperature_c if step.temperature_c is not None else '-'}",
                f"Velocita: {step.speed if step.speed else '-'}",
                f"Reverse: {'Si' if step.reverse else 'No'}",
                f"Confidenza: {step.confidence}",
                f"Motivazione: {step.rationale or '-'}",
                "",
            ]
        )
    if getattr(adaptation, "final_note", None):
        lines.extend(["NOTA FINALE", adaptation.final_note, ""])
    return "\n".join(lines).strip()


def preferred_review_mode(adaptation) -> str:
    return "adapted"


def has_live_ai_adaptation(job) -> bool:
    return bool(job and job.adaptation and job.adaptation.mode == "ai-live")


async def run_extract_job(job_id: str, recipe_url: str, desired_servings: int | None = None) -> None:
    await job_store.update(job_id, status="extracting", message="Estrazione ricetta in corso")
    try:
        recipe = await extract_recipe(recipe_url)
        scaled_ingredients_text, scaled_ingredients_guide_text, source_servings, scaling_factor = build_scaled_ingredients(
            recipe,
            desired_servings,
        )
        await job_store.update(job_id, status="adapting", message="Adattamento ricetta tramite AI in corso")
        adaptation = await build_recipe_adaptation(
            recipe,
            desired_servings=desired_servings,
            source_servings=source_servings,
        )
        if adaptation.mode != "ai-live":
            await job_store.update(
                job_id,
                status="failed",
                message="Adattamento AI non disponibile: impossibile proseguire",
                selected_review_mode=preferred_review_mode(adaptation),
                source_servings=source_servings,
                scaling_factor=scaling_factor,
                recipe_data=recipe,
                adaptation=adaptation,
                recipe_json=recipe.to_json(),
                adaptation_json=adaptation.to_json(),
                ingredients_text=build_ingredients_text(recipe),
                ingredients_guide_text=build_ingredients_guide_text(recipe),
                scaled_ingredients_text=scaled_ingredients_text,
                scaled_ingredients_guide_text=scaled_ingredients_guide_text,
                steps_text=build_steps_text(recipe),
                mc_steps_text=build_mc_steps_text(recipe),
                adapted_steps_text=build_adapted_steps_text(adaptation),
                error="L'adattamento AI non e' disponibile. Configura il provider AI o risolvi l'errore della chiamata prima di continuare.",
            )
            return
        final_status = "awaiting_review"
        if adaptation.mode == "ai-live":
            final_message = "Ricetta estratta. Proposta AI pronta per la revisione"
        else:
            final_message = "Ricetta estratta. Bozza di adattamento pronta per la revisione"
        await job_store.update(
            job_id,
            status=final_status,
            message=final_message,
            selected_review_mode=preferred_review_mode(adaptation),
            source_servings=source_servings,
            scaling_factor=scaling_factor,
            recipe_data=recipe,
            adaptation=adaptation,
            recipe_json=recipe.to_json(),
            adaptation_json=adaptation.to_json(),
            ingredients_text=build_ingredients_text(recipe),
            ingredients_guide_text=build_ingredients_guide_text(recipe),
            scaled_ingredients_text=scaled_ingredients_text,
            scaled_ingredients_guide_text=scaled_ingredients_guide_text,
            steps_text=build_steps_text(recipe),
            mc_steps_text=build_mc_steps_text(recipe),
            adapted_steps_text=build_adapted_steps_text(adaptation),
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
            "default_desired_servings": request.query_params.get("desired_servings", ""),
        },
    )


@app.post("/imports", response_class=HTMLResponse)
async def create_import(
    recipe_url: str = Form(...),
    desired_servings: int | None = Form(default=None),
) -> HTMLResponse:
    job = await job_store.create(recipe_url, desired_servings=desired_servings)
    import asyncio
    asyncio.create_task(run_extract_job(job.id, recipe_url, desired_servings))
    return RedirectResponse(url=f"/imports/{job.id}", status_code=303)


@app.post("/api/imports")
async def create_import_api(
    request: Request,
    recipe_url: str = Form(...),
    desired_servings: int | None = Form(default=None),
) -> dict[str, object]:
    job = await job_store.create(recipe_url, desired_servings=desired_servings)
    import asyncio
    asyncio.create_task(run_extract_job(job.id, recipe_url, desired_servings))
    return {
        "jobId": job.id,
        "detailUrl": str(request.url_for("import_detail", job_id=job.id)),
        "statusUrl": str(request.url_for("get_import_api_status", job_id=job.id)),
        "exportUrl": str(request.url_for("export_import_api", job_id=job.id)),
        "desiredServings": desired_servings,
    }


@app.get("/imports/{job_id}", response_class=HTMLResponse)
async def import_detail(request: Request, job_id: str) -> HTMLResponse:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return templates.TemplateResponse(request, "import_detail.html", build_job_template_context(job))


@app.get("/api/imports/latest-confirmed")
async def get_latest_confirmed_import_api(request: Request) -> dict[str, object]:
    job = await job_store.latest_confirmed()
    if not job:
        raise HTTPException(status_code=404, detail="Nessuna ricetta confermata disponibile")

    return {
        "jobId": job.id,
        "status": job.status,
        "confirmed": True,
        "selectedReviewMode": job.selected_review_mode,
        "detailUrl": str(request.url_for("import_detail", job_id=job.id)),
        "exportUrl": str(request.url_for("export_import_api", job_id=job.id)),
    }


@app.get("/api/imports/{job_id}")
async def get_import_api_status(request: Request, job_id: str) -> dict[str, object]:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")

    return {
        "jobId": job.id,
        "status": job.status,
        "message": job.message,
        "ready": job.status in {"completed", "awaiting_review"},
        "confirmed": job.confirmed_at is not None,
        "selectedReviewMode": job.selected_review_mode,
        "detailUrl": str(request.url_for("import_detail", job_id=job.id)),
        "exportUrl": str(request.url_for("export_import_api", job_id=job.id)),
    }


@app.get("/api/imports/{job_id}/export")
async def export_import_api(job_id: str, mode: str = "selected") -> dict[str, object]:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    if job.status not in {"completed", "awaiting_review"}:
        raise HTTPException(status_code=409, detail="Job non ancora pronto per l'export")
    if not has_live_ai_adaptation(job):
        raise HTTPException(status_code=409, detail="Adattamento AI non disponibile: export bloccato")
    return build_export_payload(job, mode)


@app.post("/imports/{job_id}/confirm")
async def confirm_import(request: Request, job_id: str):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    if job.status not in {"completed", "awaiting_review"}:
        raise HTTPException(status_code=409, detail="Job non ancora pronto per la conferma")
    if not has_live_ai_adaptation(job):
        raise HTTPException(status_code=409, detail="Adattamento AI non disponibile: conferma bloccata")

    await job_store.update(
        job_id,
        confirmed_at=datetime.now(timezone.utc),
        message="Ricetta confermata e pronta per Tampermonkey su Monsieur Cuisine",
    )
    updated_job = await job_store.get(job_id)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "partials/job_status.html", build_job_template_context(updated_job))
    return RedirectResponse(url=f"/imports/{job_id}", status_code=303)


@app.post("/imports/{job_id}/selection")
async def update_import_selection(request: Request, job_id: str, review_mode: str = Form(...)):
    if review_mode != "adapted":
        raise HTTPException(status_code=400, detail="Modalita' non valida")

    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")

    if not job.adaptation:
        raise HTTPException(status_code=400, detail="Adattamento non disponibile")

    await job_store.update(job_id, selected_review_mode=review_mode)
    updated_job = await job_store.get(job_id)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(request, "partials/job_status.html", build_job_template_context(updated_job))
    return RedirectResponse(url=f"/imports/{job_id}", status_code=303)


@app.post("/imports/{job_id}/prompt-template")
async def update_prompt_template(request: Request, job_id: str, prompt_template: str = Form(...)):
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")

    try:
        save_adaptation_prompt_template(prompt_template)
    except ValueError as exc:
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(
                request,
                "partials/job_status.html",
                build_job_template_context(job, prompt_error=str(exc)),
                status_code=400,
            )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated_job = await job_store.get(job_id)
    if request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request,
            "partials/job_status.html",
            build_job_template_context(updated_job, prompt_saved=True),
        )
    return RedirectResponse(url=f"/imports/{job_id}", status_code=303)


@app.get("/partials/jobs/{job_id}", response_class=HTMLResponse)
async def job_status_partial(request: Request, job_id: str) -> HTMLResponse:
    job = await job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    return templates.TemplateResponse(request, "partials/job_status.html", build_job_template_context(job))


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}
