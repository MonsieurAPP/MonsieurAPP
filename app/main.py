import asyncio
import ast
import html
import logging
import math
import json
import os
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

from app.models import RecipeData, RecipeIngredient, RecipeStep
from app.services.job_store import InMemoryJobStore
from app.services.adaptation_prompt_store import (
    get_default_adaptation_prompt_template,
    load_adaptation_prompt_template,
    save_adaptation_prompt_template,
)
from app.services.payload_generation import PayloadGenerationError, generate_payload_from_description
from app.services.recipe_adaptation import (
    build_recipe_adaptation,
    extract_json_payload,
    get_ai_settings,
    is_live_ai_configured,
)
from app.services.recipe_service import RecipeExtractionError, extract_recipe


BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TAMPERMONKEY_SCRIPT_PATH = BASE_DIR / "scripts" / "tampermonkey" / "monsieur-cuisine-bridge.user.js"
LOCAL_APP_BASE_URL = "http://127.0.0.1:8000"
TAMPERMONKEY_APP_BASE_URL_PLACEHOLDER = "__APP_BASE_URL__"

load_dotenv(BASE_DIR / ".env.local")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
LOGGER = logging.getLogger("monsieur_app.web")
APP_STARTED_AT = datetime.now(timezone.utc)
APP_STARTED_MONOTONIC = time.monotonic()


def first_forwarded_header_value(value: str | None) -> str | None:
    if not value:
        return None
    first_value = value.split(",", 1)[0].strip()
    return first_value or None


def strip_host_port(host: str | None) -> str:
    if not host:
        return ""
    if host.startswith("["):
        closing_bracket_index = host.find("]")
        if closing_bracket_index != -1:
            return host[1:closing_bracket_index]
    hostname, separator, _ = host.partition(":")
    return hostname if separator else host


def is_local_hostname(host: str | None) -> bool:
    normalized_host = strip_host_port(host).strip().lower()
    return normalized_host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


def build_public_base_url(request: Request) -> str:
    configured_base_url = os.getenv("PUBLIC_APP_BASE_URL", "").strip()
    if configured_base_url:
        return configured_base_url.rstrip("/")

    render_external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if render_external_url:
        return render_external_url.rstrip("/")

    forwarded_host = first_forwarded_header_value(request.headers.get("x-forwarded-host"))
    if forwarded_host:
        forwarded_proto = first_forwarded_header_value(request.headers.get("x-forwarded-proto"))
        if not forwarded_proto:
            forwarded_proto = "http" if is_local_hostname(forwarded_host) else "https"
        elif forwarded_proto == "http" and not is_local_hostname(forwarded_host):
            forwarded_proto = "https"
        return f"{forwarded_proto}://{forwarded_host}".rstrip("/")

    fallback_base_url = str(request.base_url).rstrip("/")
    request_hostname = request.url.hostname or ""
    if request.url.scheme == "http" and not is_local_hostname(request_hostname):
        return fallback_base_url.replace("http://", "https://", 1)
    return fallback_base_url


def render_tampermonkey_script(request: Request) -> str:
    base_url = build_public_base_url(request)
    script_content = TAMPERMONKEY_SCRIPT_PATH.read_text(encoding="utf-8")
    return script_content.replace(TAMPERMONKEY_APP_BASE_URL_PLACEHOLDER, base_url, 1)

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
        "targetedWeight": getattr(step, "targeted_weight", None),
        "targetedWeightUnit": getattr(step, "targeted_weight_unit", None),
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


async def render_home(
    request: Request,
    *,
    payload_import_error: str | None = None,
    payload_import_value: str = "",
    payload_generation_error: str | None = None,
    payload_description_value: str = "",
    payload_description_servings_value: str = "",
) -> HTMLResponse:
    requested_flow = str(request.query_params.get("flow") or "").strip().lower()
    selected_home_flow = requested_flow if requested_flow in {"web", "ai"} else ""
    if payload_import_error or payload_generation_error:
        selected_home_flow = "ai"

    jobs = await job_store.list_recent()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "jobs": jobs,
            "default_desired_servings": request.query_params.get("desired_servings", ""),
            "payload_import_error": payload_import_error,
            "payload_import_value": payload_import_value,
            "payload_generation_error": payload_generation_error,
            "payload_description_value": payload_description_value,
            "payload_description_servings_value": payload_description_servings_value,
            "selected_home_flow": selected_home_flow,
        },
        status_code=400 if payload_import_error or payload_generation_error else 200,
    )


def format_step_minutes(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "-"
    return str(max(1, round(duration_seconds / 60)))


def format_targeted_weight_display(targeted_weight: int | None, targeted_weight_unit: str | None) -> str:
    if targeted_weight is None:
        return "-"
    return f"{targeted_weight} {targeted_weight_unit or 'g'}"


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


def parse_optional_int(value: object, *, minimum: int | None = None) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None

    parsed: int | None = None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if math.isfinite(value):
            parsed = int(round(value))
    elif isinstance(value, str):
        normalized = value.strip().replace(",", ".")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
            parsed = int(round(float(normalized)))

    if parsed is None:
        return None
    if minimum is not None and parsed < minimum:
        return None
    return parsed


def parse_optional_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "si", "sì"}
    return False


def coerce_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def normalize_manual_environment(value: object) -> str:
    environment = str(value or "").strip().lower()
    if environment in STEP_ENVIRONMENT_LABELS:
        return environment
    return "mc"


def normalize_manual_structured_ingredients(
    value: object,
) -> tuple[list[dict[str, object]], list[RecipeIngredient]]:
    if not isinstance(value, list):
        return [], []

    exported_items: list[dict[str, object]] = []
    preview_items: list[RecipeIngredient] = []
    for item in value:
        if not isinstance(item, dict):
            continue

        original_text = str(item.get("originalText") or item.get("finalText") or item.get("name") or "").strip()
        final_text = str(item.get("finalText") or original_text).strip()
        name = str(item.get("name") or final_text or original_text).strip()
        quantity = str(item.get("quantity") or "").strip() or None
        unit = str(item.get("unit") or "").strip() or None
        notes = str(item.get("notes") or "").strip() or None
        scaled_quantity = str(item.get("scaledQuantity") or "").strip() or None
        final_quantity = str(item.get("finalQuantity") or item.get("quantity") or "").strip() or None

        if not any([original_text, final_text, name]):
            continue

        exported_items.append(
            {
                "originalText": original_text or final_text or name,
                "name": name or final_text or original_text,
                "quantity": quantity,
                "unit": unit,
                "notes": notes,
                "scaledQuantity": scaled_quantity,
                "finalQuantity": final_quantity,
                "finalText": final_text or original_text or name,
            }
        )
        preview_items.append(
            RecipeIngredient(
                original_text=original_text or final_text or name,
                name=name or final_text or original_text,
                quantity=quantity,
                unit=unit,
                notes=notes,
            )
        )

    return exported_items, preview_items


def build_fallback_structured_ingredients(ingredients: list[str]) -> tuple[list[dict[str, object]], list[RecipeIngredient]]:
    exported_items = []
    preview_items = []
    for ingredient in ingredients:
        exported_items.append(
            {
                "originalText": ingredient,
                "name": ingredient,
                "quantity": None,
                "unit": None,
                "notes": None,
                "scaledQuantity": None,
                "finalQuantity": None,
                "finalText": ingredient,
            }
        )
        preview_items.append(RecipeIngredient(original_text=ingredient, name=ingredient))
    return exported_items, preview_items


def normalize_manual_payload_step(step: dict[str, object], index: int) -> dict[str, object]:
    description = str(step.get("description") or step.get("detailedInstructions") or f"Passaggio {index + 1}").strip()
    detailed_instructions = str(step.get("detailedInstructions") or description).strip() or description
    targeted_weight = parse_optional_int(step.get("targetedWeight"), minimum=1)
    targeted_weight_unit = None
    if targeted_weight is not None:
        targeted_weight_unit = str(step.get("targetedWeightUnit") or "g").strip() or "g"
    source_refs = []
    if isinstance(step.get("sourceRefs"), list):
        source_refs = [ref for ref in (parse_optional_int(item, minimum=1) for item in step["sourceRefs"]) if ref is not None]

    return {
        "environment": normalize_manual_environment(step.get("environment")),
        "description": description,
        "program": str(step.get("program") or "").strip() or None,
        "parametersSummary": str(step.get("parametersSummary") or "").strip() or None,
        "accessory": str(step.get("accessory") or "").strip() or None,
        "detailedInstructions": detailed_instructions,
        "durationSeconds": parse_optional_int(step.get("durationSeconds"), minimum=0),
        "temperatureC": parse_optional_int(step.get("temperatureC")),
        "speed": str(step.get("speed") or "").strip() or None,
        "targetedWeight": targeted_weight,
        "targetedWeightUnit": targeted_weight_unit,
        "reverse": parse_optional_bool(step.get("reverse")),
        "confidence": str(step.get("confidence") or "").strip() or None,
        "rationale": str(step.get("rationale") or "").strip() or None,
        "sourceRefs": source_refs,
    }


def compute_scaling_factor(desired_servings: int | None, source_servings: int | None) -> float | None:
    if not desired_servings or not source_servings or desired_servings <= 0 or source_servings <= 0:
        return None
    return desired_servings / source_servings


def normalize_manual_export_payload(
    raw_payload: str,
) -> tuple[dict[str, object], RecipeData, int | None, int | None, float | None]:
    translation_map = str.maketrans(
        {
            "\ufeff": "",
            "\u200b": "",
            "\u200c": "",
            "\u200d": "",
            "\u2060": "",
            "\xa0": " ",
            "\u201c": '"',
            "\u201d": '"',
            "\u2018": "'",
            "\u2019": "'",
        }
    )
    sanitized_payload = html.unescape(raw_payload).translate(translation_map).strip()
    if not sanitized_payload:
        raise ValueError("Incolla un payload JSON valido prima di inviarlo.")

    def _parse_python_literal_candidate(candidate: str) -> dict[str, object] | None:
        try:
            parsed_literal = ast.literal_eval(candidate)
        except (ValueError, SyntaxError):
            return None
        return parsed_literal if isinstance(parsed_literal, dict) else None

    def _parse_javascript_object_candidate(candidate: str) -> dict[str, object] | None:
        start_index = candidate.find("{")
        end_index = candidate.rfind("}")
        if start_index == -1 or end_index <= start_index:
            return None

        normalized_candidate = candidate[start_index : end_index + 1].strip().removesuffix(";")
        normalized_candidate = re.sub(r",\s*([}\]])", r"\1", normalized_candidate)
        normalized_candidate = re.sub(r"\bundefined\b", "null", normalized_candidate)
        normalized_candidate = re.sub(
            r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:',
            r'\1"\2":',
            normalized_candidate,
        )

        try:
            parsed_literal = json.loads(normalized_candidate)
        except json.JSONDecodeError:
            return None
        return parsed_literal if isinstance(parsed_literal, dict) else None

    try:
        payload = extract_json_payload(sanitized_payload)
    except Exception:
        try:
            payload = json.loads(sanitized_payload)
        except json.JSONDecodeError as exc:
            payload = _parse_python_literal_candidate(sanitized_payload)
            if payload is None:
                start_index = sanitized_payload.find("{")
                end_index = sanitized_payload.rfind("}")
                if start_index != -1 and end_index > start_index:
                    payload = _parse_python_literal_candidate(sanitized_payload[start_index : end_index + 1])
            if payload is None:
                payload = _parse_javascript_object_candidate(sanitized_payload)

            if payload is None:
                LOGGER.warning(
                    "Payload manuale non parsabile. Preview=%r",
                    sanitized_payload[:160],
                )
                raise ValueError(f"Payload JSON non valido: {exc.msg} (riga {exc.lineno}, colonna {exc.colno}).") from exc

    if not isinstance(payload, dict):
        raise ValueError("Il payload deve essere un oggetto JSON.")

    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Il payload deve contenere un titolo valorizzato nel campo 'title'.")

    operational_timeline = payload.get("operationalTimeline") if isinstance(payload.get("operationalTimeline"), list) else []
    raw_steps = operational_timeline or (payload.get("steps") if isinstance(payload.get("steps"), list) else [])
    normalized_timeline = [
        normalize_manual_payload_step(step, index)
        for index, step in enumerate(raw_steps)
        if isinstance(step, dict)
    ]
    if not normalized_timeline:
        raise ValueError("Il payload deve contenere almeno uno step valido in 'operationalTimeline' oppure in 'steps'.")

    ingredient_lines = coerce_string_list(payload.get("ingredients"))
    structured_ingredients, structured_preview = normalize_manual_structured_ingredients(payload.get("structuredIngredients"))
    if not ingredient_lines and structured_ingredients:
        ingredient_lines = [str(item.get("finalText") or item.get("originalText") or item.get("name") or "").strip() for item in structured_ingredients]
        ingredient_lines = [item for item in ingredient_lines if item]
    if not structured_ingredients:
        structured_ingredients, structured_preview = build_fallback_structured_ingredients(ingredient_lines)

    source_url = str(payload.get("sourceUrl") or "payload://manual").strip() or "payload://manual"
    source_site = str(payload.get("sourceSite") or "Payload manuale").strip() or "Payload manuale"
    yield_text = str(payload.get("yieldText") or payload.get("sourceYieldText") or "").strip() or None
    desired_servings = parse_optional_int(payload.get("desiredServings"), minimum=1)
    source_servings = parse_optional_int(payload.get("sourceServings"), minimum=1)
    if source_servings is None:
        source_servings = parse_servings_count(str(payload.get("sourceYieldText") or payload.get("yieldText") or ""))
    scaling_factor = compute_scaling_factor(desired_servings, source_servings)
    total_time_minutes = parse_optional_int(payload.get("totalTimeMinutes"), minimum=1)

    recipe = RecipeData(
        source_url=source_url,
        source_site=source_site,
        title=title,
        ingredients=ingredient_lines,
        structured_ingredients=structured_preview,
        steps=[
            RecipeStep(
                description=str(step["description"]),
                duration_seconds=parse_optional_int(step.get("durationSeconds"), minimum=0),
                temperature_c=parse_optional_int(step.get("temperatureC")),
                speed=str(step.get("speed") or "").strip() or None,
                targeted_weight=parse_optional_int(step.get("targetedWeight"), minimum=1),
                targeted_weight_unit=str(step.get("targetedWeightUnit") or "").strip() or None,
                reverse=parse_optional_bool(step.get("reverse")),
                source_text=str(step.get("detailedInstructions") or step["description"]),
            )
            for step in normalized_timeline
        ],
        yield_text=yield_text,
        total_time_minutes=total_time_minutes,
    )

    normalized_payload = dict(payload)
    normalized_payload["title"] = title
    normalized_payload["sourceUrl"] = source_url
    normalized_payload["sourceSite"] = source_site
    normalized_payload["ingredients"] = ingredient_lines
    normalized_payload["structuredIngredients"] = structured_ingredients
    normalized_payload["operationalTimeline"] = normalized_timeline
    normalized_payload["steps"] = [step for step in normalized_timeline if step["environment"] == "mc"]
    normalized_payload["manualSteps"] = [step for step in normalized_timeline if step["environment"] == "external"]
    normalized_payload["transitionSteps"] = [step for step in normalized_timeline if step["environment"] == "transition"]
    normalized_payload["yieldText"] = yield_text
    normalized_payload["sourceYieldText"] = str(payload.get("sourceYieldText") or yield_text or "").strip() or None
    normalized_payload["desiredServings"] = desired_servings
    normalized_payload["sourceServings"] = source_servings
    normalized_payload["totalTimeMinutes"] = total_time_minutes
    normalized_payload["warnings"] = coerce_string_list(payload.get("warnings"))
    normalized_payload["selectedReviewMode"] = str(payload.get("selectedReviewMode") or "manual").strip() or "manual"
    normalized_payload["exportMode"] = str(payload.get("exportMode") or "manual").strip() or "manual"

    return normalized_payload, recipe, desired_servings, source_servings, scaling_factor


def is_manual_payload_job(job) -> bool:
    return bool(job and getattr(job, "manual_export_payload", None))


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
    if job.confirmed_at is None:
        raise HTTPException(status_code=409, detail="Ricetta non ancora confermata")

    if is_manual_payload_job(job):
        payload = dict(job.manual_export_payload or {})
        payload["jobId"] = job.id
        payload["confirmedAt"] = job.confirmed_at.isoformat() if job.confirmed_at else None
        payload.setdefault("sourceUrl", job.recipe_data.source_url if job.recipe_data else job.recipe_url)
        payload.setdefault("sourceSite", job.recipe_data.source_site if job.recipe_data else "Payload manuale")
        return payload

    if not job.recipe_data:
        raise HTTPException(status_code=409, detail="Ricetta non ancora disponibile")

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
                "targetedWeight": step.targeted_weight,
                "targetedWeightUnit": step.targeted_weight_unit,
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
                    f"Peso target: {format_targeted_weight_display(step.targeted_weight, step.targeted_weight_unit)}",
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
                f"Peso target: {format_targeted_weight_display(step.targeted_weight, step.targeted_weight_unit)}",
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
                f"Peso target: {format_targeted_weight_display(step.targeted_weight, step.targeted_weight_unit)}",
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


def build_ai_unavailable_error(adaptation) -> str:
    if not adaptation:
        return "L'adattamento AI non e' disponibile."

    warnings = getattr(adaptation, "warnings", None) or []
    for warning in warnings:
        normalized_warning = str(warning).strip()
        if normalized_warning.startswith("Chiamata AI non riuscita:"):
            return normalized_warning.replace(" Uso il fallback deterministico.", "")
        if normalized_warning.startswith("Provider AI non configurato:"):
            return normalized_warning

    return "L'adattamento AI non e' disponibile. Configura il provider AI o risolvi l'errore della chiamata prima di continuare."


def summarize_payload_generation_request(recipe_description: str, desired_servings: int | None = None) -> str:
    normalized_description = re.sub(r"\s+", " ", recipe_description).strip()
    if len(normalized_description) > 96:
        normalized_description = normalized_description[:93].rstrip() + "..."
    prefix = f"Descrizione AI ({desired_servings} persone):" if desired_servings else "Descrizione AI:"
    return f"{prefix} {normalized_description}"


def apply_generated_payload_servings(
    normalized_payload: dict[str, object],
    recipe: RecipeData,
    desired_servings: int | None,
) -> tuple[dict[str, object], RecipeData, int | None, float | None]:
    if not desired_servings:
        source_servings = parse_optional_int(normalized_payload.get("sourceServings"), minimum=1)
        scaling_factor = compute_scaling_factor(desired_servings, source_servings)
        return normalized_payload, recipe, source_servings, scaling_factor

    enforced_yield_text = f"{desired_servings} persone"
    normalized_payload["desiredServings"] = desired_servings
    normalized_payload["sourceServings"] = desired_servings
    normalized_payload["yieldText"] = enforced_yield_text
    normalized_payload["sourceYieldText"] = enforced_yield_text
    recipe.yield_text = enforced_yield_text
    return normalized_payload, recipe, desired_servings, None


async def finalize_manual_payload_job(
    job_id: str,
    *,
    normalized_payload: dict[str, object],
    recipe: RecipeData,
    desired_servings: int | None,
    source_servings: int | None,
    scaling_factor: float | None,
    success_message: str,
) -> None:
    confirmed_at = datetime.now(timezone.utc)
    finalized_payload = dict(normalized_payload)
    finalized_payload["jobId"] = job_id
    finalized_payload["confirmedAt"] = confirmed_at.isoformat()
    finalized_payload_json = json.dumps(finalized_payload, ensure_ascii=False, indent=2)

    await job_store.update(
        job_id,
        desired_servings=desired_servings,
        status="completed",
        message=success_message,
        selected_review_mode="manual",
        confirmed_at=confirmed_at,
        source_servings=source_servings,
        scaling_factor=scaling_factor,
        recipe_data=recipe,
        recipe_json=recipe.to_json(),
        manual_export_payload=finalized_payload,
        manual_export_payload_json=finalized_payload_json,
        ingredients_text=build_ingredients_text(recipe),
        ingredients_guide_text=build_ingredients_guide_text(recipe),
        steps_text=build_steps_text(recipe),
        mc_steps_text=build_mc_steps_text(recipe),
    )


async def run_generate_payload_job(job_id: str, recipe_description: str, requested_desired_servings: int | None = None) -> None:
    await job_store.update(job_id, status="adapting", message="Generazione payload AI in corso")
    try:
        generated_payload = await generate_payload_from_description(
            recipe_description,
            desired_servings=requested_desired_servings,
        )
        payload_json = json.dumps(generated_payload, ensure_ascii=False, indent=2)
        normalized_payload, recipe, normalized_desired_servings, source_servings, scaling_factor = normalize_manual_export_payload(payload_json)
        normalized_payload, recipe, source_servings, scaling_factor = apply_generated_payload_servings(
            normalized_payload,
            recipe,
            requested_desired_servings,
        )
        final_desired_servings = requested_desired_servings or normalized_desired_servings
        await finalize_manual_payload_job(
            job_id,
            normalized_payload=normalized_payload,
            recipe=recipe,
            desired_servings=final_desired_servings,
            source_servings=source_servings,
            scaling_factor=scaling_factor,
            success_message="Payload AI confermato e pronto per Tampermonkey su Monsieur Cuisine",
        )
    except PayloadGenerationError as exc:
        LOGGER.warning("Generazione payload AI fallita per il job %s: %s", job_id, exc)
        await job_store.update(
            job_id,
            status="failed",
            message="Generazione payload AI fallita",
            error=str(exc),
        )
    except ValueError as exc:
        LOGGER.exception("Payload AI non valido nel job %s", job_id)
        await job_store.update(
            job_id,
            status="failed",
            message="Generazione payload AI fallita",
            error=f"Il payload generato dalla AI non e' valido: {exc}",
            debug_traceback=traceback.format_exc(),
        )
    except Exception as exc:  # pragma: no cover
        LOGGER.exception("Errore inatteso nel job AI %s", job_id)
        await job_store.update(
            job_id,
            status="failed",
            message="Errore inatteso nella generazione payload AI",
            error=str(exc) or repr(exc),
            debug_traceback=traceback.format_exc(),
        )


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
                error=build_ai_unavailable_error(adaptation),
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
    return await render_home(request)


@app.get("/downloads/tampermonkey-script")
async def download_tampermonkey_script(request: Request) -> Response:
    if not TAMPERMONKEY_SCRIPT_PATH.exists():
        raise HTTPException(status_code=404, detail="Script Tampermonkey non trovato")

    return Response(
        content=render_tampermonkey_script(request),
        media_type="application/javascript",
        headers={
            "Content-Disposition": 'attachment; filename="monsieur-cuisine-bridge.user.js"',
            "Cache-Control": "no-store",
        },
    )


@app.post("/imports", response_class=HTMLResponse)
async def create_import(
    recipe_url: str = Form(...),
    desired_servings: int | None = Form(default=None),
) -> HTMLResponse:
    job = await job_store.create(recipe_url, desired_servings=desired_servings)
    asyncio.create_task(run_extract_job(job.id, recipe_url, desired_servings))
    return RedirectResponse(url=f"/imports/{job.id}", status_code=303)


@app.post("/imports/payload/generate", response_class=HTMLResponse)
async def create_import_from_description(
    request: Request,
    recipe_description: str = Form(...),
    desired_servings: int | None = Form(default=None),
) -> HTMLResponse:
    normalized_description = re.sub(r"\s+", " ", recipe_description).strip()
    if not normalized_description:
        return await render_home(
            request,
            payload_generation_error="Descrivi brevemente la ricetta prima di inviare la richiesta.",
            payload_description_value=recipe_description,
            payload_description_servings_value=str(desired_servings or ""),
        )

    job = await job_store.create(
        summarize_payload_generation_request(normalized_description, desired_servings=desired_servings),
        desired_servings=desired_servings,
    )
    asyncio.create_task(run_generate_payload_job(job.id, normalized_description, desired_servings))
    return RedirectResponse(url=f"/imports/{job.id}", status_code=303)


@app.post("/imports/payload", response_class=HTMLResponse)
async def create_import_from_payload(
    request: Request,
    payload_json: str = Form(...),
) -> HTMLResponse:
    try:
        normalized_payload, recipe, desired_servings, source_servings, scaling_factor = normalize_manual_export_payload(payload_json)
    except ValueError as exc:
        return await render_home(
            request,
            payload_import_error=str(exc),
            payload_import_value=payload_json,
            payload_description_value="",
            payload_description_servings_value="",
        )

    job = await job_store.create("Payload manuale incollato", desired_servings=desired_servings)
    await finalize_manual_payload_job(
        job.id,
        normalized_payload=normalized_payload,
        recipe=recipe,
        desired_servings=desired_servings,
        source_servings=source_servings,
        scaling_factor=scaling_factor,
        success_message="Payload manuale confermato e pronto per Tampermonkey su Monsieur Cuisine",
    )
    return RedirectResponse(url=f"/imports/{job.id}", status_code=303)


@app.post("/api/imports")
async def create_import_api(
    request: Request,
    recipe_url: str = Form(...),
    desired_servings: int | None = Form(default=None),
) -> dict[str, object]:
    job = await job_store.create(recipe_url, desired_servings=desired_servings)
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
    if not is_manual_payload_job(job) and not has_live_ai_adaptation(job):
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


@app.get("/health/uptime")
@app.get("/healthz")
async def uptime_healthcheck() -> dict[str, object]:
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": round(time.monotonic() - APP_STARTED_MONOTONIC, 3),
        "service": "MonsieurAPP API",
    }


@app.get("/health")
async def healthcheck() -> dict[str, object]:
    ai_settings = get_ai_settings()
    default_base_url = "https://api.openai.com/v1"
    configured_base_url = str(ai_settings["base_url"]).rstrip("/")
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime": round(time.monotonic() - APP_STARTED_MONOTONIC, 3),
        "started_at": APP_STARTED_AT.isoformat(),
        "service": "MonsieurAPP API",
        "ai_enabled": bool(ai_settings["enabled"]),
        "ai_configured": is_live_ai_configured(),
        "ai_has_api_key": bool(ai_settings["api_key"]),
        "ai_has_model": bool(ai_settings["model"]),
        "ai_uses_custom_base_url": configured_base_url != default_base_url,
    }
