import json
import logging
import time

import httpx

from app.services.recipe_adaptation import extract_json_payload, get_ai_settings


LOGGER = logging.getLogger("monsieur_app.payload_generation")


class PayloadGenerationError(Exception):
    """Raised when the AI payload generation request cannot complete successfully."""


def build_payload_generation_messages(recipe_description: str, desired_servings: int | None = None) -> list[dict[str, str]]:
    normalized_description = " ".join(recipe_description.split())
    servings_instruction = (
        f"- Genera la ricetta direttamente per {desired_servings} persone e imposta desiredServings, sourceServings, yieldText e sourceYieldText coerenti con questo valore."
        if desired_servings
        else "- Se la descrizione cita le porzioni, valorizza desiredServings, sourceServings, yieldText e sourceYieldText in modo coerente."
    )
    return [
        {
            "role": "system",
            "content": (
                "Sei un assistente che genera payload JSON compatibili con il bridge di Monsieur Cuisine. "
                "Rispondi solo con un oggetto JSON valido, senza markdown, senza commenti e senza testo extra."
            ),
        },
        {
            "role": "user",
            "content": "\n".join(
                [
                    "Genera un payload JSON completo e plausibile partendo da questa descrizione breve di ricetta:",
                    normalized_description,
                    "",
                    "Requisiti obbligatori:",
                    "- Usa solo lingua italiana.",
                    "- Restituisci un solo oggetto JSON valido.",
                    "- Il JSON deve contenere almeno questi campi: title, sourceUrl, sourceSite, ingredients, operationalTimeline, yieldText, sourceYieldText, desiredServings, sourceServings, totalTimeMinutes, warnings, selectedReviewMode, exportMode.",
                    "- Imposta sourceUrl a payload://ai-generated e sourceSite a Payload generato da AI.",
                    "- Imposta selectedReviewMode a manual e exportMode a manual.",
                    "- ingredients deve essere un array di stringhe leggibili e concrete.",
                    "- operationalTimeline deve contenere tra 3 e 10 step utili.",
                    "- Ogni step deve contenere questi campi: environment, description, program, parametersSummary, accessory, detailedInstructions, durationSeconds, temperatureC, speed, targetedWeight, targetedWeightUnit, reverse, confidence, rationale, sourceRefs.",
                    "- environment puo' essere solo mc, external o transition.",
                    "- Per gli step di pesata usa program uguale a Bilancia, targetedWeight numerico, targetedWeightUnit uguale a g e gli altri parametri tecnici a null quando non servono.",
                    "- Per gli altri step macchina usa program uguale a Cottura personalizzata.",
                    "- Per gli step external o transition usa program null, reverse false e tutti i campi tecnici a null.",
                    "- sourceRefs deve essere sempre un array, anche vuoto.",
                    servings_instruction,
                    "- Se mancano dettagli, fai assunzioni ragionevoli e dichiarale dentro warnings.",
                    "- Evita placeholder vaghi come q.b. o quanto basta quando puoi proporre una quantita plausibile.",
                    "",
                    "Vincoli pratici per gli step:",
                    "- Inserisci almeno uno step mc.",
                    "- Usa environment transition solo per lavare, svuotare o risciacquare il boccale.",
                    "- Usa environment external per riposo, servizio, forno, padella o altre azioni fuori macchina.",
                    "- description deve essere breve e chiara.",
                    "- detailedInstructions puo' essere piu' esplicito ma deve restare sintetico.",
                    "- confidence puo' essere ai-generated.",
                    "",
                    "Non aggiungere testo fuori dal JSON.",
                ]
            ),
        },
    ]


async def generate_payload_from_description(recipe_description: str, desired_servings: int | None = None) -> dict[str, object]:
    normalized_description = " ".join(recipe_description.split())
    if not normalized_description:
        raise PayloadGenerationError("Descrivi brevemente la ricetta prima di inviare la richiesta.")

    settings = get_ai_settings()
    if not settings["enabled"]:
        raise PayloadGenerationError("Integrazione AI disabilitata da configurazione.")
    if not settings["api_key"] or not settings["model"]:
        raise PayloadGenerationError("Configurazione AI incompleta: servono AI_API_KEY e AI_MODEL.")

    endpoint = f"{settings['base_url']}{settings['path']}"
    payload = {
        "model": settings["model"],
        "temperature": 0.4,
        "response_format": {"type": "json_object"},
        "messages": build_payload_generation_messages(normalized_description, desired_servings=desired_servings),
    }

    started_at = time.perf_counter()
    try:
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
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text.strip()
        if len(detail) > 280:
            detail = detail[:277].rstrip() + "..."
        detail_suffix = f": {detail}" if detail else ""
        raise PayloadGenerationError(
            f"Il provider AI ha risposto con HTTP {exc.response.status_code}{detail_suffix}"
        ) from exc
    except httpx.HTTPError as exc:
        raise PayloadGenerationError(f"Chiamata al provider AI fallita: {exc}") from exc

    elapsed_ms = round((time.perf_counter() - started_at) * 1000)
    LOGGER.info(
        "Payload AI generato | model=%s | elapsed_ms=%s",
        settings["model"],
        elapsed_ms,
    )

    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise PayloadGenerationError("Il provider AI ha restituito una risposta non parsabile come JSON.") from exc

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise PayloadGenerationError("La risposta AI non contiene choices[0].message.content.") from exc

    try:
        generated_payload = extract_json_payload(content)
    except Exception as exc:
        raise PayloadGenerationError("La risposta AI non contiene un payload JSON valido.") from exc

    if not isinstance(generated_payload, dict):
        raise PayloadGenerationError("La risposta AI non contiene un oggetto JSON valido.")

    return generated_payload