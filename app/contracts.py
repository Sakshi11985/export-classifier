"""Published data contracts for every external interface (D1). The JSON Schemas are served at
GET /v1/contract and documented in docs/DATA_CONTRACT.md."""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROUTES = ("clear-for-filing", "seek-product-clarification", "specialist-classification-review")


class QuantityUnit(str, Enum):
    pcs = "pcs"
    kg = "kg"
    litre = "litre"
    set = "set"


class ShipmentMode(str, Enum):
    sea = "sea"
    air = "air"
    road = "road"
    rail = "rail"


class RequestOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Demonstration-only switch, honoured only when ENABLE_FAULT_INJECTION=true on the server
    fault: Literal["none", "naive_first_pass", "fabricated_citation", "wrong_product"] = "none"
    bypass_cache: bool = False


class ClassificationRequest(BaseModel):
    """POST /v1/classify body. Unknown fields are rejected (schema), values are range-checked,
    categoricals are enums and several fields are cross-validated."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    product_description: str = Field(..., min_length=3, max_length=2000,
                                     description="Free-text description of the product being exported")
    product_name: str | None = Field(None, max_length=120)
    materials: list[str] | None = Field(None, max_length=10, description="Materials, if known")
    intended_use: str | None = Field(None, max_length=300)
    destination_country: str | None = Field(None, pattern=r"^[A-Z]{2}$", description="ISO 3166-1 alpha-2")
    shipment_mode: ShipmentMode | None = None
    quantity: int | None = Field(None, ge=1, le=10_000_000)
    quantity_unit: QuantityUnit | None = None
    unit_value_usd: float | None = Field(None, gt=0, le=10_000_000)
    capacity_litres: float | None = Field(None, gt=0, le=100_000)
    weight_kg: float | None = Field(None, gt=0, le=1_000_000)
    is_set_for_retail: bool | None = None
    essential_character_hint: str | None = Field(None, max_length=200)
    client_reference: str | None = Field(None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")
    options: RequestOptions | None = None

    @field_validator("product_description")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]{2,}", v):
            raise ValueError("must contain at least one word of letters")
        return v

    @field_validator("materials")
    @classmethod
    def _materials_ok(cls, v):
        if v is None:
            return v
        out = []
        for m in v:
            m = (m or "").strip()
            if not (2 <= len(m) <= 60):
                raise ValueError("each material must be 2-60 characters")
            if not re.fullmatch(r"[A-Za-z][A-Za-z \-/&,.()]*", m):
                raise ValueError(f"material {m!r} contains unsupported characters")
            out.append(m)
        return out

    @model_validator(mode="after")
    def _cross_field(self):
        if (self.quantity is None) != (self.quantity_unit is None):
            raise ValueError("quantity and quantity_unit must be provided together")
        if self.is_set_for_retail and self.quantity_unit is not None and self.quantity_unit != QuantityUnit.set \
                and self.quantity_unit != QuantityUnit.pcs:
            raise ValueError("is_set_for_retail=true requires quantity_unit 'set' or 'pcs'")
        if self.quantity is not None and self.unit_value_usd is not None \
                and self.quantity * self.unit_value_usd > 1_000_000_000:
            raise ValueError("quantity x unit_value_usd exceeds the 1e9 USD consignment limit")
        if self.quantity_unit == QuantityUnit.litre and self.capacity_litres is not None \
                and self.quantity is not None and self.capacity_litres > self.quantity * 1000:
            raise ValueError("capacity_litres is inconsistent with quantity (litre)")
        return self


class Citation(BaseModel):
    id: str
    quote: str = Field(..., min_length=3)


class Alternative(BaseModel):
    heading: str
    status: Literal["excluded_by_note", "less_specific", "not_applicable", "tied", "chosen_elsewhere"]
    basis: str = ""
    note_id: str | None = None


class Proposal(BaseModel):
    """What the classifier agent must return (validated before the verifier sees it)."""
    model_config = ConfigDict(extra="ignore")
    article: str = ""
    materials: list[str] = Field(default_factory=list)
    heading: str | None = Field(None, pattern=r"^\d{4}$")
    gri_applied: str | None = None
    reasoning_summary: str = ""
    citations: list[Citation] = Field(default_factory=list)
    alternatives: list[Alternative] = Field(default_factory=list)
    needs_clarification: list[str] = Field(default_factory=list)
    out_of_scope: bool = False
    tie: bool = False
    confidence: Literal["high", "medium", "low"] = "low"


class FeedbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    rating: Literal["satisfactory", "unsatisfactory"]
    reason: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _reason_required(self):
        if self.rating == "unsatisfactory" and not (self.reason and len(self.reason) >= 3):
            raise ValueError("a reason is required when rating is 'unsatisfactory'")
        return self


class SubmitResponse(BaseModel):
    run_id: str
    status: Literal["queued", "completed"]
    status_url: str
    trace_url: str
    poll_after_ms: int
    cache_hit: bool = False


def contract_schemas() -> dict:
    return {
        "ClassificationRequest": ClassificationRequest.model_json_schema(),
        "SubmitResponse": SubmitResponse.model_json_schema(),
        "FeedbackIn": FeedbackIn.model_json_schema(),
        "Proposal": Proposal.model_json_schema(),
        "routes": list(ROUTES),
    }
