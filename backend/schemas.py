from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class UserCreate(BaseModel):
    email: str
    username: str
    password: str = Field(min_length=8)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or "." not in value.split("@")[-1]:
            raise ValueError("Invalid email")
        return value

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 2:
            raise ValueError("Username must contain at least 2 characters")
        return value


class UserLogin(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class UserOut(BaseModel):
    id: int
    email: str
    username: str
    created_at: datetime

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ForgotPasswordRequest(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class ForgotPasswordResponse(BaseModel):
    ok: bool
    message: str
    reset_token: str | None = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class DeleteAccountRequest(BaseModel):
    password: str


class TournamentBase(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    state: dict[str, Any] = Field(default_factory=dict)
    champion: str | None = None
    team_count: int | None = None
    max_losses: int | None = None
    has_grand_final_reset: bool = False


class TournamentCreate(TournamentBase):
    pass


class TournamentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    state: dict[str, Any] | None = None
    champion: str | None = None
    team_count: int | None = None
    max_losses: int | None = None
    has_grand_final_reset: bool | None = None


class TournamentOut(BaseModel):
    id: int
    title: str
    champion: str | None
    team_count: int | None
    max_losses: int | None
    has_grand_final_reset: bool
    state: dict[str, Any]
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TournamentListItem(BaseModel):
    id: int
    title: str
    champion: str | None
    team_count: int | None
    max_losses: int | None
    has_grand_final_reset: bool
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
