import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from recipe_scrapers import scrape_html

from app.models import RecipeData, RecipeStep


LOGGER = logging.getLogger("monsieur_app.recipe")


class RecipeExtractionError(Exception):
    """Raised when a recipe cannot be extracted from the source site."""


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


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
