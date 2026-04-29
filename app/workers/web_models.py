"""Strict input/output schemas for the restricted web tool."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RestrictedWebRequest(BaseModel):
    """One deliberate web operation: page read or form submission."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["read_page", "submit_form"]
    url: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=240)
    method: Literal["GET", "POST"] = "GET"
    form_fields: dict[str, str] = Field(default_factory=dict)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped.startswith(("https://", "http://")):
            raise ValueError("URL must start with https:// or http://")
        return stripped

    @field_validator("form_fields")
    @classmethod
    def _validate_form_fields(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, field_value in value.items():
            clean_key = key.strip()
            if not clean_key:
                raise ValueError("Form field names must be non-empty")
            if len(clean_key) > 120:
                raise ValueError("Form field names must be 120 characters or fewer")
            if len(field_value) > 4000:
                raise ValueError("Form field values must be 4000 characters or fewer")
            normalized[clean_key] = field_value
        return normalized

    @field_validator("method")
    @classmethod
    def _validate_method(cls, value: str) -> str:
        return value.upper()

    def model_post_init(self, __context: object) -> None:
        if self.action == "read_page":
            if self.method != "GET":
                raise ValueError("read_page requests must use GET")
            if self.form_fields:
                raise ValueError("read_page requests cannot include form_fields")
        elif self.action == "submit_form" and not self.form_fields:
            raise ValueError("submit_form requests must include at least one form field")


class RestrictedWebResult(BaseModel):
    """Strict, low-risk summary of a completed web operation."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["read_page", "submit_form"]
    requested_url: str
    final_url: str
    status_code: int = Field(ge=100, le=599)
    content_type: str
    title: str | None = None
    text_excerpt: str
    submitted_field_names: list[str] = Field(default_factory=list)
    redirect_count: int = Field(ge=0)
    truncated: bool = False
