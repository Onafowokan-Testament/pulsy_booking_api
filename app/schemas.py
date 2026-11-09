from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


class SlotInput(BaseModel):
    id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    start: datetime
    end: datetime

    @field_validator("id", "provider")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def end_after_start(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class ResetInput(BaseModel):
    slots: list[SlotInput]


class BookingInput(BaseModel):
    slot_id: str
    name: str
    email: EmailStr

    @field_validator("slot_id", "name", mode="before")
    @classmethod
    def trim_required(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must not be empty")
        return value.strip()

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("must not be empty")
        return value.strip().lower()


class BookingOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slot_id: str
    name: str
    email: str
    status: str
    created_at: datetime

