from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core import password_policy

# Роль — ключ записи в таблице roles (app/repositories/roles_repository.py): встроенные «admin», «user», «warehouse», «viewer», «absences» и созданные администратором «r_xxxxxx».
# Что роли доступно, определяют её права (app/core/permissions.py); проверка — на сервере для каждого запроса (app/core/perm_guard.py).
Role = str


class UserOut(BaseModel):
    id: int
    username: str
    role: Role
    role_name: str = ""
    must_change_password: bool
    created_at: str
    permissions: list[str] = Field(default_factory=list)       # права роли; у администратора ["*"]


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(max_length=128)
    role: Role = "user"

    @field_validator("username")
    @classmethod
    def username_no_spaces(cls, v: str) -> str:
        v = v.strip()
        if not v or " " in v:
            raise ValueError("Логин не должен содержать пробелы")
        return v

    @model_validator(mode="after")
    def password_meets_policy(self) -> "UserCreate":
        password_policy.check(self.password, self.username)
        return self


class UserLogin(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(max_length=128)

    @field_validator("new_password")
    @classmethod
    def new_password_meets_policy(cls, v: str) -> str:
        return password_policy.check(v)             # «не содержит логин» проверяет маршрут (там известен логин)


class AdminPasswordReset(BaseModel):
    new_password: str = Field(max_length=128)

    @field_validator("new_password")
    @classmethod
    def new_password_meets_policy(cls, v: str) -> str:
        return password_policy.check(v)


class RoleChange(BaseModel):
    role: Role
