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
                    "- Per gli step di pesata usa program uguale a Bilancia, targetedWeight numerico, targetedWeightUnit uguale a g; i campi durationSeconds, temperatureC, speed devono essere SEMPRE null in uno step Bilancia.",
                    "- Ogni ingrediente da pesare richiede il proprio step Bilancia separato. Se la descrizione cita piu' ingredienti con il loro peso (es. '50g di quinoa e 100g di acqua'), genera TANTI step Bilancia quanti sono gli ingredienti pesati, uno per ciascuno. Non raggruppare mai piu' ingredienti in un unico step Bilancia.",
                    "- Regola pesata: se il peso e' >= 5 g, imposta targetedWeight al valore esatto. Se il peso e' < 5 g, imposta targetedWeight a 5 e usa ESATTAMENTE questo testo in description: 'Indicato il peso minimo proponibile. Il peso reale e' X g' dove X e' il peso reale; aggiungi la stessa nota anche in detailedInstructions.",
                    "- IMPORTANTE: se una frase descrive sia un ingrediente da pesare sia un'azione di cottura (es. 'aggiungere 10g di olio e riscaldare a 100 gradi per 2 minuti'), genera DUE step separati: (1) uno step Bilancia con solo il peso dell'ingrediente, (2) uno step Cottura personalizzata con durationSeconds, temperatureC e speed dell'azione di cottura. Non mescolare mai il tempo di cottura con il peso in uno stesso step.",
                    "- Quando il testo indica di aggiungere un ingrediente durante o prima di una cottura (es. 'aggiungere le cipolle e cuocere 5 min / 100°C / Vel. 1'), genera questi step nell'ordine: (1) se e' specificato il peso, uno step Bilancia per pesare l'ingrediente; (2) uno step descrittivo (environment mc o external, program null, senza parametri tecnici) che descrive l'aggiunta dell'ingrediente; (3) lo step Cottura personalizzata con i parametri tecnici della cottura. Il testo dell'aggiunta va nella description dello step descrittivo, non dentro lo step di cottura.",
                    "- Per gli altri step macchina usa program uguale a Cottura personalizzata.",
                    "- Per gli step external o transition usa program null, reverse false e tutti i campi tecnici a null.",
                    "- sourceRefs deve essere sempre un array, anche vuoto.",
                    servings_instruction,
                    "- Se mancano dettagli, fai assunzioni ragionevoli e dichiarale dentro warnings.",
                    "- Evita placeholder vaghi come q.b. o quanto basta quando puoi proporre una quantita plausibile.",
                    "",
                    "Notazione parametri MC: ogni volta che il testo contiene una notazione del tipo 'X sec / Vel. N' o 'X min / T°C / Vel. N' o simili, DEVI creare un apposito step mc con program Cottura personalizzata per quella notazione. Non ignorare mai una notazione MC presente nel testo di input.",
                    "Per ogni notazione MC trovata, estrai e valorizza ESATTAMENTE:",
                    "  - durationSeconds: converti i minuti in secondi (es. 2 min = 120) o usa i secondi direttamente. Se e' un range (es. 15-18 min), usa il valore medio arrotondato al minuto intero (es. (15+18)/2 = 16 min = 960 sec).",
                    "  - temperatureC: il numero prima di C o gradi (es. 100°C -> 100); null se assente.",
                    "  - speed: il numero dopo Vel. o Velocita (es. Vel. 5 -> '5').",
                    "  - reverse: true se il testo dice Antiorario, Ant. o simili; false altrimenti.",
                    "  - Se la notazione contiene solo tempo e velocita senza temperatura, usa program Cottura personalizzata con temperatureC null.",
                    "  - Non usare mai Bilancia per step che contengono tempo o temperatura anche se c'e' un peso nell'ingrediente.",
                    "  - Non usare Bilancia per ingredienti misurati in ml, cucchiai, cucchiaini o altre unita non in grammi; in quel caso usa uno step descrittivo (environment mc, program Cottura personalizzata, senza targetedWeight) o external.",
                    "",
                    "Vincoli pratici per gli step:",
                    "- Inserisci almeno uno step mc.",
                    "- Se la ricetta descrive un piatto caldo (cottura, stufato, risotto, zuppa, arrosto, ecc.), includi SEMPRE uno step mc di cottura principale con program Cottura personalizzata, valorizzando durationSeconds, temperatureC, speed e reverse in modo plausibile per la preparazione.",
                    "- Una frase descrittiva che parla di cottura (es. 'la cottura a temperatura costante impedisce di scuocere') indica che DEVI generare uno step mc di cottura, non ignorarla.",
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