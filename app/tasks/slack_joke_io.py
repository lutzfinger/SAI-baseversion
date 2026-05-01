"""I/O schemas for the slack_joke task.

These are PUBLIC because:
  - `registry/tasks/slack_joke.yaml` references them by dotted path
  - the private `app/tasks/slack_joke.py` TaskFactory imports them to
    build the CloudLLMTier's response_schema

Keeping the schemas in public also lets other operators ship a similar
joke task by reusing the request/response shape — they only need to wire
their own prompt + channel.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JokeRequest(BaseModel):
    """One request for a safe-for-work joke from a Slack user.

    The control plane / DM dispatcher transforms a Slack DM into one of
    these and feeds it to `TieredTaskRunner.run`. `request_text` is the
    untrusted user-typed input and is treated as data — never as
    instructions to the LLM.
    """

    model_config = ConfigDict(extra="forbid")

    request_text: str = Field(min_length=1, max_length=2000)
    requester_user_id: str | None = None       # Slack user_id, optional
    reply_channel: str                          # where to post the joke
    reply_thread_ts: str | None = None          # threaded reply, optional


class JokeResponse(BaseModel):
    """One generated SFW joke.

    `safe_for_work` and `content_rating` are the model's self-assessment;
    a downstream output-guard tier (or the operator) is the final
    arbiter. The runtime keeps this response in the EvalRecord so the
    operator can audit jokes that were posted.
    """

    model_config = ConfigDict(extra="forbid")

    request_summary: str
    joke_text: str = Field(min_length=1)
    safe_for_work: bool = True
    content_rating: Literal["g", "pg"] = "g"
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
