from pydantic import BaseModel, Field, field_validator


def _normalize_email(value: str) -> str:
    email = (value or "").strip().lower()
    if "@" not in email or "." not in email.split("@", 1)[1]:
        raise ValueError("Invalid email address")
    local, domain = email.split("@", 1)
    if not local or not domain:
        raise ValueError("Invalid email address")
    return email


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str
    is_superuser: bool = False
    is_active: bool = True


class AuthResponse(BaseModel):
    token: str
    user: UserResponse
