import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_html

from app.models import RecipeData, RecipeIngredient, RecipeStep


LOGGER = logging.getLogger("monsieur_app.recipe")

QUANTITY_PATTERN = r"\d+/\d+|\d+(?:[.,]\d+)?(?:\s+\d+/\d+)?|un|una|uno|mezzo|mezza"
UNIT_PATTERN = (
    r"kg|g|gr|grammi?|grammo|mg|ml|cl|dl|l|lt|litri?|litro|"
    r"cucchiaio|cucchiai|cucchiaino|cucchiaini|tazze?|bicchieri?|"
    r"bustin[ae]|buste?|spicchi?|ramett[oi]|fogli[ae]|foglioline|"
    r"mazzi?|pizzichi?|presa|prese|fette?|fettine|"
    r"scatolett[ae]|confezioni?|vasetti?|barattoli?|"
    r"pezzi?|pezzetti?|patate|carote|zucchine|uova?|limoni?|lime|"
    r"cipolle?|scalogni|pomodori?|pomodorini|mele?|pere?|banane?|"
    r"arance?|mandarini|fragole|olive|filetti?|tranci"
)
QB_PATTERN = r"q\s*\.?\s*b\s*\.?|quanto basta"


class RecipeExtractionError(Exception):
    """Raised when a recipe cannot be extracted from the source site."""


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_fraction_characters(value: str) -> str:
    normalized = value or ""
    replacements = {
        "½": "1/2",
        "¼": "1/4",
        "¾": "3/4",
        "⅓": "1/3",
        "⅔": "2/3",
        "⅛": "1/8",
    }
    for source, replacement in replacements.items():
        normalized = normalized.replace(source, replacement)
    return normalized


def cleanup_ingredient_name(value: str) -> str:
    cleaned = normalize_whitespace(value)
    cleaned = re.sub(r"^(di|della|del|dei|degli|delle)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .,;-")
    return cleaned


def parse_ingredient(raw_value: str) -> RecipeIngredient:
    original_text = normalize_whitespace(normalize_fraction_characters(raw_value))
    if not original_text:
        return RecipeIngredient(original_text="", name="")

    notes: list[str] = []
    for match in re.findall(r"\(([^()]*)\)", original_text):
        normalized = normalize_whitespace(match)
        if normalized:
            notes.append(normalized)

    text = re.sub(r"\([^()]*\)", "", original_text).strip(" ,;-")
    text = re.sub(r"\s+", " ", text).strip()

    if re.match(rf"^(?:{QB_PATTERN})\b", text, flags=re.IGNORECASE):
        name = cleanup_ingredient_name(re.sub(rf"^(?:{QB_PATTERN})\b", "", text, flags=re.IGNORECASE))
        notes.insert(0, "quanto basta")
        return RecipeIngredient(
            original_text=original_text,
            name=name or original_text,
            notes=", ".join(dict.fromkeys(notes)) or None,
        )

    quantity_match = re.match(
        rf"^(?P<quantity>{QUANTITY_PATTERN})\b",
        text,
        flags=re.IGNORECASE,
    )
    quantity = quantity_match.group("quantity") if quantity_match else None
    remainder = text[quantity_match.end():].strip(" ,;-") if quantity_match else text

    unit_match = None
    if quantity_match and remainder:
        unit_match = re.match(
            rf"^(?P<unit>{UNIT_PATTERN})\b",
            remainder,
            flags=re.IGNORECASE,
        )

    unit = unit_match.group("unit") if unit_match else None
    name = remainder[unit_match.end():].strip(" ,;-") if unit_match else remainder

    if quantity and unit and not name:
        name = unit
        unit = None

    if not quantity:
        trailing_qb_match = re.match(
            rf"^(?P<name>.+?)\s+(?P<qb>{QB_PATTERN})$",
            text,
            flags=re.IGNORECASE,
        )
        if trailing_qb_match:
            name = trailing_qb_match.group("name")
            notes.insert(0, "quanto basta")

    if not quantity and not unit:
        trailing_quantity_match = re.match(
            rf"^(?P<name>.+?)\s+(?P<quantity>{QUANTITY_PATTERN})(?:\s+(?P<unit>{UNIT_PATTERN}))?$",
            text,
            flags=re.IGNORECASE,
        )
        if trailing_quantity_match:
            name = trailing_quantity_match.group("name")
            quantity = trailing_quantity_match.group("quantity")
            unit = trailing_quantity_match.group("unit")
        else:
            parts = text.split()
            if len(parts) >= 2 and re.fullmatch(rf"(?:{UNIT_PATTERN})", parts[-1], flags=re.IGNORECASE):
                trailing_unit = parts[-1]
                trailing_quantity = parts[-2]
                if re.fullmatch(rf"(?:{QUANTITY_PATTERN})", trailing_quantity, flags=re.IGNORECASE):
                    unit = trailing_unit
                    quantity = trailing_quantity
                    name = " ".join(parts[:-2])
            if not quantity and parts:
                trailing_quantity = parts[-1]
                if re.fullmatch(rf"(?:{QUANTITY_PATTERN})", trailing_quantity, flags=re.IGNORECASE):
                    quantity = trailing_quantity
                    name = " ".join(parts[:-1])

    comma_parts = [part.strip() for part in name.split(",") if part.strip()]
    if len(comma_parts) > 1:
        name = comma_parts[0]
        notes.extend(comma_parts[1:])

    name = cleanup_ingredient_name(name)
    if not name:
        name = cleanup_ingredient_name(text)
        quantity = None
        unit = None

    unique_notes = ", ".join(dict.fromkeys(note for note in notes if note)) or None
    return RecipeIngredient(
        original_text=original_text,
        name=name or original_text,
        quantity=normalize_whitespace(quantity) if quantity else None,
        unit=normalize_whitespace(unit) if unit else None,
        notes=unique_notes,
    )


def parse_ingredients(items: list[str]) -> list[RecipeIngredient]:
    return [parse_ingredient(item) for item in items if normalize_whitespace(item)]


def detect_source(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    if "giallozafferano" in netloc:
        return "giallozafferano"
    if "cookidoo" in netloc or "vorwerk" in netloc:
        return "cookidoo"
    raise RecipeExtractionError(f"Sito non supportato per l'URL: {url}")


def parse_duration_to_seconds(raw_value: Optional[str]) -> Optional[int]:
    if not raw_value:
        return None

    text = normalize_whitespace(raw_value.lower())
    patterns = [
        (r"(\d+)\s*h", 3600),
        (r"(\d+)\s*ora", 3600),
        (r"(\d+)\s*ore", 3600),
        (r"(\d+)\s*min", 60),
        (r"(\d+)\s*m(?![a-z])", 60),
        (r"(\d+)\s*sec", 1),
        (r"(\d+)\s*s(?![a-z])", 1),
    ]

    total = 0
    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, text):
            total += int(match.group(1)) * multiplier

    if total:
        return total

    if text.isdigit():
        return int(text) * 60

    return None


def parse_temperature(raw_value: Optional[str]) -> Optional[int]:
    if not raw_value:
        return None

    text = normalize_whitespace(raw_value.lower())
    if "varoma" in text:
        return 120

    match = re.search(r"(\d{2,3})", text)
    return int(match.group(1)) if match else None


def map_bimby_speed_to_mc(raw_speed: Optional[str]) -> Optional[str]:
    if not raw_speed:
        return None

    text = normalize_whitespace(raw_speed.lower())
    speed_map = {
        "soft": "1",
        "velocità soft": "1",
        "spiga": "impasto",
        "velocità cucchiaio": "1",
        "cucchiaio": "1",
        "turbo": "turbo",
    }
    if text in speed_map:
        return speed_map[text]

    numeric_match = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if numeric_match:
        return numeric_match.group(1).replace(",", ".")

    return raw_speed


def map_bimby_flags_to_mc(step_text: str) -> dict[str, Any]:
    text = normalize_whitespace(step_text.lower())
    reverse_tokens = [
        "antiorario",
        "modalità antiorario",
        "lama in senso inverso",
        "reverse",
    ]
    return {"reverse": any(token in text for token in reverse_tokens)}


def build_step(
    description: str,
    duration: Optional[str] = None,
    temperature: Optional[str] = None,
    speed: Optional[str] = None,
    source_text: Optional[str] = None,
) -> RecipeStep:
    flags = map_bimby_flags_to_mc(source_text or description)
    return RecipeStep(
        description=normalize_whitespace(description),
        duration_seconds=parse_duration_to_seconds(duration),
        temperature_c=parse_temperature(temperature),
        speed=map_bimby_speed_to_mc(speed),
        reverse=flags["reverse"],
        source_speed=speed,
        source_text=source_text or description,
    )


async def fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/135.0.0.0 Safari/537.36"
            )
        },
        timeout=30.0,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def extract_giallozafferano(url: str) -> RecipeData:
    html = await fetch_html(url)
    scraper = scrape_html(html=html, org_url=url)

    instructions = scraper.instructions_list() or []
    steps = [build_step(description=step) for step in instructions if normalize_whitespace(step)]
    ingredients = [normalize_whitespace(item) for item in scraper.ingredients() if normalize_whitespace(item)]

    return RecipeData(
        source_url=url,
        source_site="giallozafferano",
        title=normalize_whitespace(scraper.title()),
        ingredients=ingredients,
        structured_ingredients=parse_ingredients(ingredients),
        steps=steps,
        yield_text=normalize_whitespace(scraper.yields() or ""),
        total_time_minutes=scraper.total_time(),
        author=normalize_whitespace(scraper.author() or ""),
    )


def extract_step_fields_from_text(step_text: str) -> tuple[str, Optional[str], Optional[str], Optional[str]]:
    normalized = normalize_whitespace(step_text)
    duration_match = re.search(
        r"(\d+\s*(?:h|ora|ore|min|m|sec|s)(?:\s+\d+\s*(?:min|m|sec|s))?)",
        normalized,
        flags=re.IGNORECASE,
    )
    temperature_match = re.search(r"((?:\d{2,3}\s*°?\s*c)|varoma)", normalized, flags=re.IGNORECASE)
    speed_match = re.search(
        r"(velocità\s*soft|soft|velocità\s*\d+(?:[.,]\d+)?|\bspiga\b|\bcucchiaio\b|\bturbo\b)",
        normalized,
        flags=re.IGNORECASE,
    )

    description = normalized
    for match in [duration_match, temperature_match, speed_match]:
        if match:
            description = description.replace(match.group(0), "").strip(" ,;-")

    return (
        description or normalized,
        duration_match.group(0) if duration_match else None,
        temperature_match.group(0) if temperature_match else None,
        speed_match.group(0) if speed_match else None,
    )


def extract_cookidoo_structured_data(html: str) -> Optional[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    for script in soup.select("script[type='application/ld+json']"):
        raw_json = script.string or script.get_text()
        if not raw_json:
            continue
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            continue

        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "Recipe":
                return item
    return None


async def extract_cookidoo(url: str) -> RecipeData:
    html = await fetch_html(url)
    soup = BeautifulSoup(html, "lxml")
    structured = extract_cookidoo_structured_data(html) or {}

    title_node = soup.select_one("h1")
    title = normalize_whitespace(
        (structured.get("name") or title_node.get_text(" ", strip=True)) if title_node or structured else ""
    )
    if not title:
        raise RecipeExtractionError("Titolo Cookidoo non trovato. Possibile cambio del markup.")

    ingredient_nodes = soup.select(
        "[data-test-id='ingredients'] li, .ingredients li, .recipe-ingredients__list-item"
    )
    ingredients = [normalize_whitespace(node.get_text(" ", strip=True)) for node in ingredient_nodes if node]
    if not ingredients:
        ingredients = [normalize_whitespace(item) for item in structured.get("recipeIngredient", []) if item]

    step_nodes = soup.select(
        "[data-test-id='recipe-step'], .recipe-preparation__step, .recipe-step, .preparation-step"
    )
    if not step_nodes and not structured.get("recipeInstructions"):
        raise RecipeExtractionError("Step Cookidoo non trovati. Verificare i selettori CSS.")

    steps: list[RecipeStep] = []
    for node in step_nodes:
        raw_text = normalize_whitespace(node.get_text(" ", strip=True))
        if not raw_text:
            continue
        description, duration, temperature, speed = extract_step_fields_from_text(raw_text)
        steps.append(build_step(description, duration, temperature, speed, raw_text))

    if not steps:
        for item in structured.get("recipeInstructions", []):
            if isinstance(item, dict):
                raw_text = normalize_whitespace(item.get("text", ""))
            else:
                raw_text = normalize_whitespace(str(item))
            if not raw_text:
                continue
            description, duration, temperature, speed = extract_step_fields_from_text(raw_text)
            steps.append(build_step(description, duration, temperature, speed, raw_text))

    servings_node = soup.select_one("[data-test-id='recipe-servings'], .recipe-serving")
    total_time_node = soup.select_one("[data-test-id='recipe-total-time'], .recipe-total-time")
    author_node = soup.select_one("[data-test-id='recipe-author'], .recipe-author")

    total_time_raw = (
        normalize_whitespace(total_time_node.get_text(" ", strip=True))
        if total_time_node
        else normalize_whitespace(structured.get("totalTime", ""))
    )
    total_seconds = parse_duration_to_seconds(total_time_raw)

    author = None
    author_data = structured.get("author")
    if isinstance(author_data, dict):
        author = normalize_whitespace(author_data.get("name", ""))
    elif isinstance(author_data, str):
        author = normalize_whitespace(author_data)
    elif author_node:
        author = normalize_whitespace(author_node.get_text(" ", strip=True))

    return RecipeData(
        source_url=url,
        source_site="cookidoo",
        title=title,
        ingredients=ingredients,
        structured_ingredients=parse_ingredients(ingredients),
        steps=steps,
        yield_text=normalize_whitespace(servings_node.get_text(" ", strip=True)) if servings_node else structured.get("recipeYield"),
        total_time_minutes=total_seconds // 60 if total_seconds else None,
        author=author,
    )


async def extract_recipe(url: str) -> RecipeData:
    source = detect_source(url)
    if source == "giallozafferano":
        return await extract_giallozafferano(url)
    if source == "cookidoo":
        return await extract_cookidoo(url)
    raise RecipeExtractionError(f"Fonte non gestita: {source}")
