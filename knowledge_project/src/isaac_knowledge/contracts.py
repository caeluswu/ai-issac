from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class GameTarget(Contract):
    game: Literal["binding_of_isaac"] = "binding_of_isaac"
    edition: Literal["repentance"] = "repentance"
    build: str = "1.7.9b.J835"
    platform: Literal["windows", "linux", "macos", "console"] = "windows"
    extensions: list[dict] = Field(default_factory=list)


class SubjectRef(Contract):
    kind: Literal["collectible", "trinket", "card", "pill_effect", "character", "enemy", "mechanic", "room_type", "grid_entity"]
    id: int | None = Field(default=None, ge=0)
    type: int | None = Field(default=None, ge=0)
    variant: int | None = Field(default=None, ge=0)
    subtype: int | None = Field(default=None, ge=0)
    key: str | None = None

    @model_validator(mode="after")
    def identity(self):
        if self.kind in ("collectible", "trinket", "card", "pill_effect", "character") and self.id is None:
            raise ValueError("Static subject requires ID")
        if self.kind == "enemy" and any(v is None for v in (self.type, self.variant, self.subtype)):
            raise ValueError("Enemy requires type, variant and subtype")
        if self.kind == "mechanic" and not self.key: raise ValueError("Mechanic requires key")
        if self.kind in ("room_type", "grid_entity") and self.type is None: raise ValueError("Subject requires type")
        return self


class QueryPolicy(Contract):
    version_match: Literal["exact_only", "exact_or_reviewed_compatible"] = "exact_only"
    include_unreviewed: bool = True
    include_evidence_text: bool = False


class LocalUsagePolicy(Contract):
    """Trusted host configuration, never a permission in a lookup request.

    Project authorization does not change the source's rights metadata.
    """
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    schema_version: Literal[1] = 1
    policy_id: str = Field(min_length=1, max_length=128)
    provider_id: Literal["eid_snapshot"] = "eid_snapshot"
    dataset_id: str = Field(min_length=1, max_length=128)
    allow_local_storage: StrictBool = False
    allow_ai_input: StrictBool = False
    purposes: tuple[Literal["display", "planning", "component_development"], ...] = (
        "display", "planning", "component_development")
    authorization_basis: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_purposes(self):
        if len(self.purposes) != len(set(self.purposes)): raise ValueError("Repeated policy purposes")
        return self


class Page(Contract):
    limit: int = Field(default=32, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1024)


class LookupRequest(Contract):
    api_version: Literal["1.0"] = "1.0"
    request_id: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(min_length=1, max_length=128)
    target: GameTarget = Field(default_factory=GameTarget)
    subjects: list[SubjectRef] = Field(min_length=1, max_length=32)
    facets: list[Literal["identity", "effects", "activation", "hazards", "conditions", "phase_model"]] = Field(default_factory=lambda: ["identity", "effects"], min_length=1)
    language: str = "zh-CN"
    purpose: Literal["planning", "component_development", "build_compilation", "display"] = "display"
    policy: QueryPolicy = Field(default_factory=QueryPolicy)
    page: Page = Field(default_factory=Page)

    @model_validator(mode="after")
    def unique_facets(self):
        if len(self.facets) != len(set(self.facets)): raise ValueError("Repeated facets")
        if self.purpose == "build_compilation" and self.policy.include_unreviewed:
            raise ValueError("Build compilation cannot accept unreviewed claims")
        return self
