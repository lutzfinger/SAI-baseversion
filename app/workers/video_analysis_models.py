"""Structured models for video transcript analysis artifacts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class VideoSummary(BaseModel):
    """Grounded summary derived from a video transcript or chunk rollup."""

    model_config = ConfigDict(extra="forbid")

    summary_text: str = Field(min_length=1, max_length=1200)
    key_points: list[str] = Field(default_factory=list, max_length=10)
    action_items: list[str] = Field(default_factory=list, max_length=10)
    decisions: list[str] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=10)


class VideoTranscriptChunkArtifact(BaseModel):
    """One extracted audio chunk and its transcript artifact paths."""

    model_config = ConfigDict(extra="forbid")

    chunk_index: int = Field(ge=1)
    start_seconds: int = Field(ge=0)
    end_seconds: int = Field(ge=0)
    duration_seconds: int = Field(ge=1)
    audio_file_path: str
    transcript_file_path: str
    transcript_char_count: int = Field(ge=0)
    summary_text: str | None = None


class VideoAnalysisResult(BaseModel):
    """Operator-facing result for a completed video transcript analysis run."""

    model_config = ConfigDict(extra="forbid")

    source_video_path: str
    output_dir: str
    duration_seconds: int = Field(ge=1)
    chunk_duration_seconds: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    transcript_char_count: int = Field(ge=0)
    transcript_text_path: str
    transcript_markdown_path: str
    summary_markdown_path: str | None = None
    analysis_json_path: str
    summary: VideoSummary | None = None
    chunks: list[VideoTranscriptChunkArtifact] = Field(default_factory=list)
