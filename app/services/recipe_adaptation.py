import json
import logging
import os
import re
import time

import httpx

from app.models import (
    IngredientAdaptationNote,
    RecipeAdaptation,
    RecipeAdaptedStep,
    RecipeData,
    RecipeStep,
)


LOGGER = logging.getLogger("monsieur_app.adaptation")
PROMPT_VERSION = "mc-adaptation-v1"


AMBIGUITY_TOKENS = (
    "quanto basta",
    "q.b",
    "a piacere",
    "finche",
    "fino a",
    "regolatevi",
    "all'occorrenza",
)

STEP_CONNECTOR_PATTERN = re.compile(
    r"^(nel frattempo|intanto|ora|quindi|poi|a questo punto|infine|dopodiche|dopodiché)\s+",
    flags=re.IGNORECASE,
)


class AdaptationError(Exception):
    """Raised when the live AI adaptation cannot be parsed or validated."""


def get_ai_settings() -> dict[str, object]:
    base_url = str(os.getenv("AI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
    path = str(os.getenv("AI_CHAT_COMPLETIONS_PATH", "/chat/completions"))
    if not path.startswith("/"):
        path = f"/{path}"

    return {
        "enabled": os.getenv("AI_ENABLED", "1") != "0",
        "api_key": os.getenv("AI_API_KEY"),
        "base_url": base_url,
        "path": path,
        "model": os.getenv("AI_MODEL"),
        "timeout": float(os.getenv("AI_TIMEOUT_SECONDS", "45")),
    }


def is_live_ai_configured() -> bool:
    settings = get_ai_settings()
    return bool(settings["enabled"] and settings["api_key"] and settings["model"])


def step_has_structured_parameters(step: RecipeStep) -> bool:
    return any(
        [
            step.duration_seconds is not None,
            step.temperature_c is not None,
            bool(step.speed),
            step.reverse,
        ]
    )


def count_ambiguous_signals(recipe: RecipeData) -> int:
    haystacks = [item.lower() for item in recipe.ingredients]
    haystacks.extend((step.source_text or step.description).lower() for step in recipe.steps)
    return sum(any(token in haystack for token in AMBIGUITY_TOKENS) for haystack in haystacks)


def infer_step_confidence(recipe: RecipeData, step: RecipeStep) -> str:
    if step_has_structured_parameters(step):
        return "high"
    if recipe.source_site == "cookidoo":
        return "medium"
    return "low"


def build_step_headline(step: RecipeStep) -> str:
    source_text = (step.source_text or step.description).strip()
    normalized = re.sub(r"^\d+[).:-]?\s*", "", source_text)
    normalized = STEP_CONNECTOR_PATTERN.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .;:-")

    sentences = [part.strip(" .;:-") for part in re.split(r"[.;:]", normalized) if part.strip()]
    headline = sentences[0] if sentences else normalized
    if len(headline) > 110:
        headline = headline[:107].rsplit(" ", 1)[0].strip() + "..."

    return headline or step.description


def build_step_rationale(recipe: RecipeData, step: RecipeStep, confidence: str) -> str:
    has_technical_data = step_has_structured_parameters(step)
    if confidence == "high":
        return "Bozza operativa ottenuta dal parsing deterministico; i parametri tecnici presenti sono stati mantenuti nella proposta."
    if confidence == "medium":
        return "Bozza sintetizzata per la revisione: la descrizione e' stata resa piu' diretta, ma i parametri restano da confermare."
    if recipe.source_site == "giallozafferano" and not has_technical_data:
        return "Sintesi operativa di uno step narrativo: il testo e' stato accorciato per simulare una proposta AI, senza introdurre parametri non presenti."
    return "Bozza conservativa mantenuta vicina al testo originale per evitare inferenze non supportate."


def build_adapted_step(recipe: RecipeData, step: RecipeStep, index: int) -> RecipeAdaptedStep:
    confidence = infer_step_confidence(recipe, step)
    rationale = build_step_rationale(recipe, step, confidence)
    adapted_description = build_step_headline(step)

    return RecipeAdaptedStep(
        description=adapted_description,
        duration_seconds=step.duration_seconds,
        temperature_c=step.temperature_c,
        speed=step.speed,
        reverse=step.reverse,
        confidence=confidence,
        rationale=rationale,
        source_refs=[index],
    )


def build_ingredient_notes(recipe: RecipeData) -> list[IngredientAdaptationNote]:
    notes: list[IngredientAdaptationNote] = []
    for index, ingredient in enumerate(recipe.structured_ingredients, start=1):
        if ingredient.notes and "quanto basta" in ingredient.notes.lower():
            notes.append(
                IngredientAdaptationNote(
                    ingredient_index=index,
                    issue="Quantita' non deterministica",
                    suggestion="Lasciare la riga originale e confermare manualmente il dosaggio nel form Monsieur Cuisine.",
                )
            )
        if not ingredient.quantity and not ingredient.unit:
            notes.append(
                IngredientAdaptationNote(
                    ingredient_index=index,
                    issue="Ingrediente poco strutturato",
                    suggestion="Usare nome e riga originale come riferimento finche' non viene introdotto l'adattamento AI completo.",
                )
            )
    return notes


def build_adaptation_draft(
    recipe: RecipeData,
    *,
    desired_servings: int | None = None,
    source_servings: int | None = None,
) -> RecipeAdaptation:
    structured_steps = sum(1 for step in recipe.steps if step_has_structured_parameters(step))
    total_steps = len(recipe.steps) or 1
    structured_ratio = structured_steps / total_steps
    ambiguity_count = count_ambiguous_signals(recipe)

    recommended = any(
        [
            recipe.source_site == "giallozafferano",
            structured_ratio < 0.3,
            ambiguity_count > 0,
        ]
    )

    warnings: list[str] = []
    if recipe.source_site == "giallozafferano":
        warnings.append("La sorgente GialloZafferano produce spesso step narrativi: l'adattamento AI e' consigliato.")
    if structured_ratio < 0.3:
        warnings.append("Meno del 30% degli step contiene parametri tecnici gia' strutturati.")
    if ambiguity_count:
        warnings.append(f"Rilevati {ambiguity_count} segnali di ambiguita' testuale da revisionare.")
    low_confidence_steps = sum(1 for step in recipe.steps if infer_step_confidence(recipe, step) == "low")
    if low_confidence_steps:
        warnings.append(f"{low_confidence_steps} step su {len(recipe.steps)} restano a bassa confidenza nella bozza adattata.")
    if desired_servings and source_servings and desired_servings != source_servings:
        warnings.append(
            f"Adattamento richiesto da {source_servings} a {desired_servings} persone: le quantita' dovrebbero essere riviste con attenzione."
        )

    if recommended:
        summary = "Bozza di adattamento preparata per la revisione: gli step sono stati sintetizzati in forma piu' operativa per simulare una proposta AI controllata."
        reason = "La ricetta presenta abbastanza elementi narrativi o ambigui da giustificare un passaggio intermedio di adattamento."
        status = "ready_for_review"
    else:
        summary = "Nessun adattamento aggiuntivo consigliato al momento: il parsing deterministico e' gia' sufficiente per una revisione manuale."
        reason = "La ricetta contiene gia' un livello minimo accettabile di struttura per il flusso attuale."
        status = "not_needed"

    return RecipeAdaptation(
        status=status,
        recommended=recommended,
        summary=summary,
        reason=reason,
        warnings=warnings,
        adapted_steps=[build_adapted_step(recipe, step, index) for index, step in enumerate(recipe.steps, start=1)],
        ingredient_notes=build_ingredient_notes(recipe),
    )


def append_warning(adaptation: RecipeAdaptation, warning: str) -> None:
    if warning not in adaptation.warnings:
        adaptation.warnings.append(warning)


def build_live_prompt(
    recipe: RecipeData,
    draft: RecipeAdaptation,
    *,
    desired_servings: int | None = None,
    source_servings: int | None = None,
) -> list[dict[str, str]]:
    system_message = (
        "Sei un assistente che adatta ricette italiane per il form di Monsieur Cuisine. "
        "Non inventare ingredienti, quantita', temperature, velocita' o tempi non supportati dal testo. "
        "Se un dato manca, restituisci null o un warning. Rispondi solo con JSON valido."
    )
    user_payload = {
        "task": "Adatta la ricetta a Monsieur Cuisine mantenendo una proposta prudente e strutturata.",
        "prompt_version": PROMPT_VERSION,
        "output_schema": {
            "summary": "string",
            "reason": "string",
            "recommended": "boolean",
            "warnings": ["string"],
            "adapted_steps": [
                {
                    "description": "string",
                    "duration_seconds": "integer|null",
                    "temperature_c": "integer|null",
                    "speed": "string|null",
                    "reverse": "boolean",
                    "confidence": "low|medium|high",
                    "rationale": "string",
                    "source_refs": [1],
                }
            ],
            "ingredient_notes": [
                {
                    "ingredient_index": 1,
                    "issue": "string",
                    "suggestion": "string",
                }
            ],
        },
        "rules": [
            "Usa solo informazioni presenti nel testo o inferenze conservative.",
            "Non cambiare ingredienti.",
            "Se e' presente un target di porzioni, puoi proporre un adattamento ragionato delle quantita' solo quando la proporzione e' sensata e dichiarandolo nei warning o nelle note.",
            "Mantieni gli step brevi e operativi.",
            "Se manca evidenza per tempo, temperatura o velocita', lascia null.",
            "Aggiungi warning espliciti per ogni ambiguita' rilevante.",
        ],
        "servings_context": {
            "source_servings": source_servings,
            "desired_servings": desired_servings,
        },
        "recipe": json.loads(recipe.to_json()),
        "deterministic_draft": json.loads(draft.to_json()),
    }

    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]


def extract_json_payload(content: str) -> dict[str, object]:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start_index = raw.find("{")
    end_index = raw.rfind("}")
    if start_index == -1 or end_index == -1 or end_index <= start_index:
        raise AdaptationError("Risposta AI senza JSON valido.")

    try:
        parsed = json.loads(raw[start_index : end_index + 1])
    except json.JSONDecodeError as exc:
        raise AdaptationError("JSON AI non parsabile.") from exc

    if not isinstance(parsed, dict):
        raise AdaptationError("La risposta AI non contiene un oggetto JSON.")
    return parsed


def parse_int_or_none(value: object) -> int | None:
    if value in (None, "", "null"):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return round(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if re.fullmatch(r"-?\d+", normalized):
            return int(normalized)
    return None


def parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "si", "sì"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def parse_confidence(value: object, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"low", "medium", "high"}:
            return normalized
    return fallback


def parse_source_refs(value: object, max_steps: int, fallback_index: int) -> list[int]:
    if not isinstance(value, list):
        return [fallback_index]

    refs: list[int] = []
    for item in value:
        parsed = parse_int_or_none(item)
        if parsed is None:
            continue
        if 1 <= parsed <= max_steps and parsed not in refs:
            refs.append(parsed)
    return refs or [fallback_index]


def build_adaptation_from_payload(
    recipe: RecipeData,
    draft: RecipeAdaptation,
    payload: dict[str, object],
    *,
    model: str,
) -> RecipeAdaptation:
    raw_steps = payload.get("adapted_steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AdaptationError("La risposta AI non contiene adapted_steps validi.")

    adapted_steps: list[RecipeAdaptedStep] = []
    max_steps = len(recipe.steps)
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            raise AdaptationError(f"Lo step AI {index} non e' un oggetto valido.")

        base_step = draft.adapted_steps[min(index - 1, len(draft.adapted_steps) - 1)]
        description = str(item.get("description") or "").strip()
        if not description:
            raise AdaptationError(f"Lo step AI {index} non contiene description.")

        adapted_steps.append(
            RecipeAdaptedStep(
                description=description,
                duration_seconds=parse_int_or_none(item.get("duration_seconds")),
                temperature_c=parse_int_or_none(item.get("temperature_c")),
                speed=str(item.get("speed")).strip() if item.get("speed") not in (None, "") else None,
                reverse=parse_bool(item.get("reverse")),
                confidence=parse_confidence(item.get("confidence"), base_step.confidence),
                rationale=str(item.get("rationale") or base_step.rationale or "").strip() or None,
                source_refs=parse_source_refs(item.get("source_refs"), max_steps, index),
            )
        )

    ingredient_notes: list[IngredientAdaptationNote] = []
    raw_notes = payload.get("ingredient_notes")
    if isinstance(raw_notes, list):
        for item in raw_notes:
            if not isinstance(item, dict):
                continue
            ingredient_index = parse_int_or_none(item.get("ingredient_index"))
            issue = str(item.get("issue") or "").strip()
            suggestion = str(item.get("suggestion") or "").strip()
            if not ingredient_index or not issue or not suggestion:
                continue
            ingredient_notes.append(
                IngredientAdaptationNote(
                    ingredient_index=ingredient_index,
                    issue=issue,
                    suggestion=suggestion,
                )
            )

    warnings = list(draft.warnings)
    raw_warnings = payload.get("warnings")
    if isinstance(raw_warnings, list):
        for item in raw_warnings:
            warning = str(item).strip()
            if warning and warning not in warnings:
                warnings.append(warning)

    recommended = parse_bool(payload.get("recommended")) if "recommended" in payload else draft.recommended
    status = "ready_for_review" if recommended else "not_needed"

    return RecipeAdaptation(
        status=status,
        recommended=recommended,
        mode="ai-live",
        model=model,
        prompt_version=PROMPT_VERSION,
        summary=str(payload.get("summary") or draft.summary or "").strip() or draft.summary,
        reason=str(payload.get("reason") or draft.reason or "").strip() or draft.reason,
        warnings=warnings,
        adapted_steps=adapted_steps,
        ingredient_notes=ingredient_notes or draft.ingredient_notes,
    )


async def request_live_adaptation(
    recipe: RecipeData,
    draft: RecipeAdaptation,
    *,
    desired_servings: int | None = None,
    source_servings: int | None = None,
) -> RecipeAdaptation:
    settings = get_ai_settings()
    if not settings["enabled"]:
        raise AdaptationError("Integrazione AI disabilitata da configurazione.")
    if not settings["api_key"] or not settings["model"]:
        raise AdaptationError("Configurazione AI incompleta: servono AI_API_KEY e AI_MODEL.")

    endpoint = f"{settings['base_url']}{settings['path']}"
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": build_live_prompt(
            recipe,
            draft,
            desired_servings=desired_servings,
            source_servings=source_servings,
        ),
    }

    started_at = time.perf_counter()
    async with httpx.AsyncClient(timeout=float(settings["timeout"])) as client:
        response = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {settings['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        LOGGER.info(
            "Adattamento AI completato | model=%s | source=%s | elapsed_ms=%s",
            settings["model"],
            recipe.source_site,
            elapsed_ms,
        )

    body = response.json()
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AdaptationError("La risposta AI non contiene choices[0].message.content.") from exc

    parsed_payload = extract_json_payload(content)
    return build_adaptation_from_payload(recipe, draft, parsed_payload, model=str(settings["model"]))


async def build_recipe_adaptation(
    recipe: RecipeData,
    *,
    desired_servings: int | None = None,
    source_servings: int | None = None,
) -> RecipeAdaptation:
    draft = build_adaptation_draft(recipe, desired_servings=desired_servings, source_servings=source_servings)
    draft.prompt_version = PROMPT_VERSION

    if not is_live_ai_configured():
        draft.mode = "deterministic-fallback"
        append_warning(draft, "Provider AI non configurato: uso la bozza deterministica finche' non viene impostato il token.")
        return draft

    try:
        adaptation = await request_live_adaptation(
            recipe,
            draft,
            desired_servings=desired_servings,
            source_servings=source_servings,
        )
        if adaptation.mode == "ai-live":
            append_warning(adaptation, "Proposta generata da una chiamata AI reale: verifica manualmente i parametri prima dell'upload.")
        return adaptation
    except Exception as exc:
        LOGGER.exception("Fallback su bozza deterministica per la ricetta %s", recipe.source_url)
        draft.mode = "deterministic-fallback"
        draft.model = str(get_ai_settings().get("model") or "") or None
        append_warning(draft, f"Chiamata AI non riuscita: {exc}. Uso il fallback deterministico.")
        return draft