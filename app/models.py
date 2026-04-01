from dataclasses import asdict, dataclass, field
from typing import Optional
import json


@dataclass
class RecipeStep:
    description: str
    duration_seconds: Optional[int] = None
    temperature_c: Optional[int] = None
    speed: Optional[str] = None
    reverse: bool = False
    source_speed: Optional[str] = None
    source_text: Optional[str] = None


@dataclass
class RecipeData:
    source_url: str
    source_site: str
    title: str
    ingredients: list[str] = field(default_factory=list)
    steps: list[RecipeStep] = field(default_factory=list)
    yield_text: Optional[str] = None
    total_time_minutes: Optional[int] = None
    author: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)
