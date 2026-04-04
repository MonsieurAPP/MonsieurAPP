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
from app.services.adaptation_prompt_store import load_adaptation_prompt_template


LOGGER = logging.getLogger("monsieur_app.adaptation")
PROMPT_VERSION = "mc-adaptation-v4"


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

CANONICAL_PROGRAMS = {
    "impasto compatto": "Impasto compatto",
    "impasto morbido": "Impasto morbido",
    "impasto liquido": "Impasto liquido",
    "cottura al vapore": "Cottura al vapore",
    "rosolare": "Rosolare",
    "bilancia": "Bilancia",
    "cottura personalizzata": "Cottura personalizzata",
    "cottura sous-vide": "Cottura sous-vide",
    "cottura lenta": "Cottura lenta",
    "bollire uova": "Bollire uova",
    "prelavaggio": "Prelavaggio",
    "fermentare": "Fermentare",
    "turbo": "Turbo",
    "cuocere il riso": "Cuocere il riso",
    "tagliaverdure": "Tagliaverdure",
    "ridurre in purea": "Ridurre in purea",
    "smoothie": "Smoothie",
}

HIGH_SPEED_PROGRAMS = {"Smoothie", "Ridurre in purea", "Turbo"}
VALID_STEP_ENVIRONMENTS = {"mc", "external", "transition"}

SOLID_FOOD_TOKENS = (
    "carne",
    "pollo",
    "manzo",
    "maiale",
    "pesce",
    "gamber",
    "verdur",
    "patat",
    "zucchin",
    "melanz",
    "pasta",
    "riso",
    "pezzi",
    "dadini",
    "bocconcini",
)

TRANSITION_STEP_TOKENS = (
    "lavare il boccale",
    "lava il boccale",
    "sciacquare il boccale",
    "risciacquare il boccale",
    "svuotare il boccale",
    "svuota il boccale",
    "pulire il boccale",
    "prelavaggio",
    "lavaggio completo",
    "risciacquo rapido",
)

EXTERNAL_STEP_TOKENS = (
    "forno",
    "padella",
    "pentola",
    "tegame",
    "casseruola",
    "teglia",
    "griglia",
    "frigo",
    "frigorifero",
    "abbattitore",
    "impiatta",
    "impiattare",
    "servi",
    "servire",
    "guarnisci",
    "guarnire",
    "decora",
    "decorare",
    "lascia raffreddare",
    "raffreddare",
    "lascia riposare",
    "riposare",
    "stendi",
    "stendere",
    "forma",
    "formare",
)

MC_STEP_HINT_TOKENS = (
    "boccale",
    "monsieur",
    "farfalla",
    "cestello",
    "vaporiera",
    "rosolare",
    "trita",
    "tritare",
    "frulla",
    "frullare",
    "impasta",
    "impastare",
    "mescola",
    "mescolare",
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


def normalize_program(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    normalized = re.sub(r"\s+", " ", value.strip().lower())
    if not normalized:
        return None
    return CANONICAL_PROGRAMS.get(normalized)


def normalize_environment(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in VALID_STEP_ENVIRONMENTS:
        return normalized
    return None


def parse_speed_level(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(\d+)", value)
    if not match:
        return None
    return int(match.group(1))


def is_probably_solid_step(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in SOLID_FOOD_TOKENS)


def infer_environment_from_text(step: RecipeStep) -> str:
    text = f"{step.description} {step.source_text or ''}".lower()
    if any(token in text for token in TRANSITION_STEP_TOKENS):
        return "transition"
    if any(token in text for token in EXTERNAL_STEP_TOKENS) and not any(token in text for token in MC_STEP_HINT_TOKENS):
        return "external"
    return "mc"


def infer_program_from_text(step: RecipeStep) -> str:
    text = f"{step.description} {step.source_text or ''}".lower()
    if any(token in text for token in ("pane", "pizza", "frolla")):
        return "Impasto compatto"
    if any(token in text for token in ("brioche", "babka", "lievitat", "panettone")):
        return "Impasto morbido"
    if any(token in text for token in ("pancake", "pastella", "crepes", "cr\u00eape")):
        return "Impasto liquido"
    if any(token in text for token in ("vapore", "cuocere al vapore", "steam")):
        return "Cottura al vapore"
    if any(token in text for token in ("soffrig", "soffritt", "rosol", "saltare", "dorare")):
        return "Rosolare"
    if "bilancia" in text or "pesa" in text:
        return "Bilancia"
    if "sous-vide" in text or "sous vide" in text:
        return "Cottura sous-vide"
    if any(token in text for token in ("cottura lenta", "slow cook", "stufare lentamente")):
        return "Cottura lenta"
    if "uova" in text and any(token in text for token in ("sode", "bollire", "barzotte")):
        return "Bollire uova"
    if any(token in text for token in ("ferment", "lievit")):
        return "Fermentare"
    if "turbo" in text:
        return "Turbo"
    if "riso" in text and any(token in text for token in ("cuoc", "less")):
        return "Cuocere il riso"
    if any(token in text for token in ("taglia", "affetta", "julienne", "grattugia")):
        return "Tagliaverdure"
    if any(token in text for token in ("purea", "vellutata", "crema", "passare", "pure")):
        return "Ridurre in purea"
    if any(token in text for token in ("smoothie", "frullato", "milkshake")):
        return "Smoothie"
    return "Cottura personalizzata"


def infer_accessory(step: RecipeStep, program: str) -> str | None:
    text = f"{step.description} {step.source_text or ''}".lower()
    if program == "Cottura al vapore":
        if any(token in text for token in ("filetti", "pesce", "verdure a fette", "asparagi")):
            return "Vaporiera piana"
        return "Vaporiera profonda"
    if program in {"Cuocere il riso", "Bollire uova"}:
        return "Cestello"
    if any(token in text for token in ("monta", "sbattere", "albumi", "panna")):
        return "Farfalla"
    return None


def infer_transition_program(step: RecipeStep) -> str | None:
    text = f"{step.description} {step.source_text or ''}".lower()
    if "prelavaggio" in text or "lavaggio completo" in text:
        return "Prelavaggio"
    return None


def format_duration_seconds(duration_seconds: int | None) -> str:
    if duration_seconds is None:
        return "-"
    minutes = max(1, round(duration_seconds / 60))
    return f"{minutes} min"


def format_motion(reverse: bool) -> str:
    return "Antiorario" if reverse else "Orario"


def build_parameters_summary(
    program: str | None,
    duration_seconds: int | None,
    temperature_c: int | None,
    speed: str | None,
    reverse: bool,
) -> str:
    if not program:
        parts: list[str] = []
        if duration_seconds is not None:
            parts.append(format_duration_seconds(duration_seconds))
        if temperature_c is not None:
            parts.append(f"{temperature_c} C")
        if speed:
            parts.append(f"Vel {speed}")
        if reverse:
            parts.append("Antiorario")
        return " | ".join(parts)
    if program == "Cottura personalizzata":
        return (
            f"{format_duration_seconds(duration_seconds)} | "
            f"{temperature_c if temperature_c is not None else '-'} C | "
            f"Vel {speed or '-'} | {format_motion(reverse)}"
        )
    parts = [program]
    if duration_seconds is not None:
        parts.append(format_duration_seconds(duration_seconds))
    if temperature_c is not None:
        parts.append(f"{temperature_c} C")
    if speed:
        parts.append(f"Vel {speed}")
    if reverse:
        parts.append("Antiorario")
    return " | ".join(parts)


def build_step_parameters_summary(
    environment: str,
    program: str | None,
    duration_seconds: int | None,
    temperature_c: int | None,
    speed: str | None,
    reverse: bool,
) -> str:
    if environment == "external":
        parts = ["Preparazione manuale"]
        if duration_seconds is not None:
            parts.append(format_duration_seconds(duration_seconds))
        if temperature_c is not None:
            parts.append(f"{temperature_c} C")
        return " | ".join(parts)
    if environment == "transition":
        if program:
            return build_parameters_summary(program, duration_seconds, temperature_c, speed, reverse)
        return "Transizione boccale"
    return build_parameters_summary(program, duration_seconds, temperature_c, speed, reverse)


def apply_program_defaults(step: RecipeStep, program: str) -> tuple[str, int | None, str | None, bool]:
    text = f"{step.description} {step.source_text or ''}".lower()
    temperature_c = step.temperature_c
    speed = step.speed
    reverse = step.reverse

    if program == "Rosolare" and temperature_c is None:
        temperature_c = 130
    if "appass" in text:
        program = "Cottura personalizzata"
        temperature_c = 100
        speed = speed or "1"
    if program == "Cottura personalizzata" and is_probably_solid_step(text):
        reverse = True
    if program == "Cottura personalizzata" and temperature_c is not None and not speed:
        speed = "1"
    if temperature_c is not None and temperature_c > 60:
        speed_level = parse_speed_level(speed)
        if speed_level and speed_level > 3 and program not in HIGH_SPEED_PROGRAMS:
            speed = "3"
    return program, temperature_c, speed, reverse


def build_output_schema() -> dict[str, object]:
    return {
        "summary": "string",
        "reason": "string",
        "final_note": "string|null",
        "recommended": "boolean",
        "warnings": ["string"],
        "adapted_steps": [
            {
                "description": "string",
                "environment": "mc|external|transition",
                "program": "string|null",
                "parameters_summary": "string|null",
                "accessory": "string|null",
                "detailed_instructions": "string",
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
    }


def build_adapted_step(recipe: RecipeData, step: RecipeStep, index: int) -> RecipeAdaptedStep:
    confidence = infer_step_confidence(recipe, step)
    rationale = build_step_rationale(recipe, step, confidence)
    adapted_description = build_step_headline(step)
    environment = infer_environment_from_text(step)
    detailed_instructions = (step.source_text or step.description).strip()

    if environment == "mc":
        program = infer_program_from_text(step)
        program, temperature_c, speed, reverse = apply_program_defaults(step, program)
        accessory = infer_accessory(step, program)
    elif environment == "transition":
        program = infer_transition_program(step)
        temperature_c = None
        speed = None
        reverse = False
        accessory = None
    else:
        program = None
        temperature_c = step.temperature_c
        speed = None
        reverse = False
        accessory = None

    return RecipeAdaptedStep(
        description=adapted_description,
        environment=environment,
        program=program,
        parameters_summary=build_step_parameters_summary(environment, program, step.duration_seconds, temperature_c, speed, reverse),
        accessory=accessory,
        detailed_instructions=detailed_instructions,
        duration_seconds=step.duration_seconds,
        temperature_c=temperature_c,
        speed=speed,
        reverse=reverse,
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

    recommended = True

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

    external_steps = sum(1 for step in recipe.steps if infer_environment_from_text(step) == "external")
    summary = "Timeline operativa sincronizzata preparata per la revisione: i passaggi Monsieur Cuisine vengono distinti dai passaggi manuali o esterni."
    reason = "Il flusso finale deve conservare l'ordine cronologico e separare cio' che va esportato verso Monsieur Cuisine da cio' che resta manuale."
    status = "ready_for_review"
    final_note = (
        f"Sono stati rilevati {external_steps} passaggi probabilmente esterni o manuali, da mostrare separatamente nella UI."
        if external_steps
        else "Non sono stati rilevati passaggi esterni evidenti nella bozza deterministica."
    )

    return RecipeAdaptation(
        status=status,
        recommended=recommended,
        summary=summary,
        reason=reason,
        final_note=final_note,
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
) -> tuple[list[dict[str, str]], str]:
    system_message = (
        "Sei un assistente che adatta ricette italiane per il form di Monsieur Cuisine. "
        "Non inventare ingredienti, quantita', temperature, velocita' o tempi non supportati dal testo. "
        "Rispondi solo con JSON valido coerente con lo schema richiesto."
    )
    output_schema = build_output_schema()
    user_sections = [
        load_adaptation_prompt_template(),
        f"Prompt version: {PROMPT_VERSION}",
        "Vincoli applicativi aggiuntivi:",
        "- Usa solo informazioni presenti nel testo o inferenze conservative.",
        "- Non cambiare ingredienti.",
        "- Se e' presente un target di porzioni, puoi proporre un adattamento ragionato delle quantita' solo quando la proporzione e' sensata e dichiarandolo nei warning o nelle note.",
        "- Mantieni una timeline unica e cronologica.",
        "- I passaggi environment = external o transition devono restare nella risposta, ma non verranno esportati verso Monsieur Cuisine.",
        "- I passaggi environment = mc sono l'unica base per l'upload automatico verso Monsieur Cuisine.",
        "- Se manca evidenza per tempo, temperatura o velocita, lascia null.",
        "- Aggiungi warning espliciti per ogni ambiguita rilevante.",
        "Schema JSON richiesto:",
        json.dumps(output_schema, ensure_ascii=False, indent=2),
        "Contesto porzioni:",
        json.dumps(
            {
                "source_servings": source_servings,
                "desired_servings": desired_servings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        "Ricetta estratta:",
        recipe.to_json(),
        "Bozza deterministica disponibile:",
        draft.to_json(),
    ]
    rendered_prompt = "\n\n".join(section for section in user_sections if section)
    prompt_text = f"[SYSTEM]\n{system_message}\n\n[USER]\n{rendered_prompt}"

    return [
        {"role": "system", "content": system_message},
        {"role": "user", "content": rendered_prompt},
    ], prompt_text


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
    prompt_text: str,
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

        environment = normalize_environment(item.get("environment")) or base_step.environment or "mc"
        same_environment_as_base = environment == base_step.environment

        if environment == "mc" and "program" in item:
            raw_program = item.get("program")
            if isinstance(raw_program, str) and not raw_program.strip():
                program = None
            else:
                program = normalize_program(raw_program) or base_step.program
        elif environment == "transition":
            raw_program = item.get("program")
            program = normalize_program(raw_program) if raw_program is not None else (base_step.program if same_environment_as_base else None)
        else:
            program = None
        duration_seconds = parse_int_or_none(item.get("duration_seconds")) if "duration_seconds" in item else (base_step.duration_seconds if same_environment_as_base else None)
        temperature_c = parse_int_or_none(item.get("temperature_c")) if "temperature_c" in item else (base_step.temperature_c if same_environment_as_base else None)
        speed = str(item.get("speed")).strip() if item.get("speed") not in (None, "") else (base_step.speed if same_environment_as_base else None)
        reverse = parse_bool(item.get("reverse")) if "reverse" in item else (base_step.reverse if same_environment_as_base else False)
        if environment != "mc":
            speed = None
            reverse = False
        if environment == "mc" and temperature_c is not None and temperature_c > 60:
            speed_level = parse_speed_level(speed)
            if speed_level and speed_level > 3 and program not in HIGH_SPEED_PROGRAMS:
                speed = "3"
        if environment == "mc" and program == "Cottura personalizzata" and is_probably_solid_step(description):
            reverse = True
        if environment == "transition" and program is None:
            program = infer_transition_program(RecipeStep(description=description, source_text=str(item.get("detailed_instructions") or description)))
        parameters_summary = str(item.get("parameters_summary") or "").strip() or build_step_parameters_summary(
            environment,
            program,
            duration_seconds,
            temperature_c,
            speed,
            reverse,
        )

        adapted_steps.append(
            RecipeAdaptedStep(
                description=description,
                environment=environment,
                program=program,
                parameters_summary=parameters_summary,
                accessory=str(item.get("accessory") or "").strip() or (base_step.accessory if same_environment_as_base else None),
                detailed_instructions=str(item.get("detailed_instructions") or "").strip() or (base_step.detailed_instructions if same_environment_as_base else description),
                duration_seconds=duration_seconds,
                temperature_c=temperature_c,
                speed=speed,
                reverse=reverse,
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
        prompt_text=prompt_text,
        summary=str(payload.get("summary") or draft.summary or "").strip() or draft.summary,
        reason=str(payload.get("reason") or draft.reason or "").strip() or draft.reason,
        final_note=str(payload.get("final_note") or draft.final_note or "").strip() or draft.final_note,
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
    messages, prompt_text = build_live_prompt(
        recipe,
        draft,
        desired_servings=desired_servings,
        source_servings=source_servings,
    )
    payload = {
        "model": settings["model"],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": messages,
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
    return build_adaptation_from_payload(
        recipe,
        draft,
        parsed_payload,
        model=str(settings["model"]),
        prompt_text=prompt_text,
    )


async def build_recipe_adaptation(
    recipe: RecipeData,
    *,
    desired_servings: int | None = None,
    source_servings: int | None = None,
) -> RecipeAdaptation:
    draft = build_adaptation_draft(recipe, desired_servings=desired_servings, source_servings=source_servings)
    draft.prompt_version = PROMPT_VERSION
    draft.prompt_text = None

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
        draft.prompt_text = build_live_prompt(
            recipe,
            draft,
            desired_servings=desired_servings,
            source_servings=source_servings,
        )[1]
        append_warning(draft, f"Chiamata AI non riuscita: {exc}. Uso il fallback deterministico.")
        return draft