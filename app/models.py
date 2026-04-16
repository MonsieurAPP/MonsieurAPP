from dataclasses import asdict, dataclass, field
from typing import Optional
import json


@dataclass
class RecipeIngredient:
    original_text: str
    name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    notes: Optional[str] = None


@dataclass
class RecipeStep:
    description: str
    duration_seconds: Optional[int] = None
    temperature_c: Optional[int] = None
    speed: Optional[str] = None
    targeted_weight: Optional[int] = None
    targeted_weight_unit: Optional[str] = None
    reverse: bool = False
    source_speed: Optional[str] = None
    source_text: Optional[str] = None


@dataclass
class RecipeAdaptedStep:
    description: str
    environment: str = "mc"
    program: Optional[str] = None
    parameters_summary: Optional[str] = None
    accessory: Optional[str] = None
    detailed_instructions: Optional[str] = None
    duration_seconds: Optional[int] = None
    temperature_c: Optional[int] = None
    speed: Optional[str] = None
    targeted_weight: Optional[int] = None
    targeted_weight_unit: Optional[str] = None
    reverse: bool = False
    confidence: str = "low"
    rationale: Optional[str] = None
    source_refs: list[int] = field(default_factory=list)


@dataclass
class IngredientAdaptationNote:
    ingredient_index: int
    issue: str
    suggestion: str


@dataclass
class RecipeAdaptation:
    status: str = "not_requested"
    recommended: bool = False
    mode: str = "deterministic-draft"
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    prompt_text: Optional[str] = None
    summary: Optional[str] = None
    reason: Optional[str] = None
    final_note: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    adapted_steps: list[RecipeAdaptedStep] = field(default_factory=list)
    ingredient_notes: list[IngredientAdaptationNote] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


@dataclass
class RecipeData:
    source_url: str
    source_site: str
    title: str
    ingredients: list[str] = field(default_factory=list)
    structured_ingredients: list[RecipeIngredient] = field(default_factory=list)
    steps: list[RecipeStep] = field(default_factory=list)
    yield_text: Optional[str] = None
    total_time_minutes: Optional[int] = None
    author: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)
