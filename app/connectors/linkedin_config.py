"""Typed LinkedIn dataset policy helpers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.shared.models import PolicyDocument


class LinkedInDatasetPolicy(BaseModel):
    """Allowed LinkedIn dataset env keys derived from policy."""

    model_config = ConfigDict(extra="forbid")

    dataset_path_env: str = "SAI_LINKEDIN_DATASET_PATH"

    @classmethod
    def from_policy(cls, policy: PolicyDocument) -> LinkedInDatasetPolicy:
        linkedin_config = policy.linkedin
        env_keys = linkedin_config.get("allowed_env_keys", {})
        return cls(dataset_path_env=str(env_keys.get("dataset_path", "SAI_LINKEDIN_DATASET_PATH")))
