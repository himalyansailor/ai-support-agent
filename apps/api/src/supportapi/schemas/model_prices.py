from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt

MAX_MICROS = 10**12

Micros = Annotated[StrictInt, Field(ge=0, le=MAX_MICROS)]


class ModelPriceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, protected_namespaces=())

    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=200)
    input_per_million_micros: Micros
    output_per_million_micros: Micros


class ModelPriceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    id: UUID
    provider: str
    model: str
    input_per_million_micros: int
    output_per_million_micros: int
    currency: str
    updated_by: UUID | None
    updated_at: datetime
