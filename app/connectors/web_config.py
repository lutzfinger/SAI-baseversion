"""Typed policy helpers for the deliberately restricted web connector."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.shared.models import PolicyDocument


class RestrictedWebPolicy(BaseModel):
    """Allowlist-driven policy for reading pages and submitting forms."""

    model_config = ConfigDict(extra="forbid")

    allowed_read_url_prefixes: list[str] = Field(default_factory=list)
    allowed_form_url_prefixes: list[str] = Field(default_factory=list)
    allowed_form_fields_by_url_prefix: dict[str, list[str]] = Field(default_factory=dict)
    allowed_form_methods: list[str] = Field(default_factory=lambda: ["POST"])
    allowed_content_types: list[str] = Field(
        default_factory=lambda: ["text/html", "text/plain"]
    )
    timeout_seconds: int = 10
    max_response_bytes: int = 50000
    max_text_chars: int = 4000
    max_redirects: int = 2
    block_private_networks: bool = True
    allow_insecure_url_prefixes: list[str] = Field(default_factory=list)

    @classmethod
    def from_policy(cls, policy: PolicyDocument) -> RestrictedWebPolicy:
        web_config = policy.web
        return cls(
            allowed_read_url_prefixes=[
                str(prefix).strip()
                for prefix in web_config.get("allowed_read_url_prefixes", [])
                if str(prefix).strip()
            ],
            allowed_form_url_prefixes=[
                str(prefix).strip()
                for prefix in web_config.get("allowed_form_url_prefixes", [])
                if str(prefix).strip()
            ],
            allowed_form_fields_by_url_prefix={
                str(prefix).strip(): [
                    str(field).strip()
                    for field in fields
                    if str(field).strip()
                ]
                for prefix, fields in web_config.get(
                    "allowed_form_fields_by_url_prefix", {}
                ).items()
                if str(prefix).strip()
            },
            allowed_form_methods=[
                str(method).strip().upper()
                for method in web_config.get("allowed_form_methods", ["POST"])
                if str(method).strip()
            ],
            allowed_content_types=[
                str(content_type).strip().lower()
                for content_type in web_config.get("allowed_content_types", ["text/html", "text/plain"])
                if str(content_type).strip()
            ],
            timeout_seconds=int(web_config.get("timeout_seconds", 10)),
            max_response_bytes=int(web_config.get("max_response_bytes", 50000)),
            max_text_chars=int(web_config.get("max_text_chars", 4000)),
            max_redirects=int(web_config.get("max_redirects", 2)),
            block_private_networks=bool(web_config.get("block_private_networks", True)),
            allow_insecure_url_prefixes=[
                str(prefix).strip()
                for prefix in web_config.get("allow_insecure_url_prefixes", [])
                if str(prefix).strip()
            ],
        )
