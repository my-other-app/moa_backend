from typing import Optional

from pydantic import BaseModel, Field


class AdmissionSettings(BaseModel):
    prefix: Optional[str]
    digits: int
    include_code: bool
    # The event code the numbers end with when asked to.
    code: str
    # What the next ticket minted will read.
    next_number: str
    numbered: int
    unnumbered: int


class AdmissionPatch(BaseModel):
    prefix: Optional[str] = Field(default=None, max_length=12)
    digits: Optional[int] = Field(default=None, ge=1, le=8)
    include_code: Optional[bool] = None


class NumberedResult(BaseModel):
    numbered: int
    first: Optional[str]
    last: Optional[str]
