"""Transcribe local videos and produce grounded summaries from the transcript."""

from __future__ import annotations

import json
import math
import platform
import subprocess
from pathlib import Path
from time import perf_counter
from typing import Any

from app.observability.langsmith import instrument_openai_client
from app.shared.config import Settings
from app.shared.models import PromptDocument, WorkflowToolDefinition
from app.tools.local_llm_classifier import OpenAILLMClient
from app.tools.models import ToolExecutionRecord, ToolExecutionStatus
from app.workers.video_analysis_models import (
    VideoAnalysisResult,
    VideoSummary,
    VideoTranscriptChunkArtifact,
)


class VideoTranscriptAnalyzerTool:
    """Extract audio from a video, transcribe it, and summarize the transcript."""

    def __init__(
        self,
        *,
        tool_definition: WorkflowToolDefinition,
        prompt: PromptDocument,
        settings: Settings,
        transcription_client: Any | None = None,
        summary_client: OpenAILLMClient | None = None,
    ) -> None:
        self.tool_definition = tool_definition
        self.prompt = prompt
        self.settings = settings
        self.provider = tool_definition.provider or "openai"
        self.summary_model = tool_definition.model or "gpt-5.2"
        self.transcription_model = (
            str(tool_definition.config.get("transcription_model", "gpt-4o-transcribe")).strip()
            or "gpt-4o-transcribe"
        )
        self.transcription_backend = (
            str(tool_definition.config.get("transcription_backend", "openai")).strip()
            or "openai"
        )
        self.local_transcription_model = (
            str(tool_definition.config.get("local_transcription_model", "small")).strip()
            or "small"
        )
        self.local_transcription_device = (
            str(tool_definition.config.get("local_transcription_device", "auto")).strip()
            or "auto"
        )
        self.local_transcription_compute_type = (
            str(tool_definition.config.get("local_transcription_compute_type", "int8")).strip()
            or "int8"
        )
        self.chunk_duration_seconds = _resolve_positive_int(
            tool_definition.config.get("chunk_duration_seconds"),
            default=600,
        )
        self.timeout_seconds = _resolve_positive_int(
            tool_definition.config.get("timeout_seconds"),
            default=int(settings.openai_timeout_seconds),
        )
        self.max_output_tokens = _resolve_optional_positive_int(
            tool_definition.config.get("max_output_tokens")
        )
        raw_language = tool_definition.config.get("language")
        if raw_language in {None, ""}:
            self.language = None
        else:
            normalized_language = str(raw_language).strip()
            self.language = normalized_language or None
        if self.provider != "openai":
            raise ValueError(
                "VideoTranscriptAnalyzerTool currently supports only the OpenAI provider"
            )
        self.transcription_client = transcription_client
        self.summary_client = summary_client
        self._local_whisper_model: Any | None = None

    def analyze(
        self,
        *,
        video_path: str | Path,
        output_dir: str | Path,
        summarize: bool = True,
    ) -> tuple[VideoAnalysisResult, ToolExecutionRecord]:
        resolved_video_path = Path(video_path).expanduser().resolve()
        if not resolved_video_path.exists():
            raise FileNotFoundError(f"Video file does not exist: {resolved_video_path}")
        if not resolved_video_path.is_file():
            raise ValueError(f"Video path must be a file: {resolved_video_path}")

        resolved_output_dir = Path(output_dir).expanduser().resolve()
        resolved_output_dir.mkdir(parents=True, exist_ok=True)
        chunk_audio_dir = resolved_output_dir / "chunks" / "audio"
        chunk_text_dir = resolved_output_dir / "chunks" / "transcripts"
        chunk_audio_dir.mkdir(parents=True, exist_ok=True)
        chunk_text_dir.mkdir(parents=True, exist_ok=True)

        started = perf_counter()
        duration_seconds = self._probe_video_duration_seconds(resolved_video_path)
        chunk_windows = _build_chunk_windows(
            duration_seconds=duration_seconds,
            chunk_duration_seconds=self.chunk_duration_seconds,
        )
        chunk_artifacts: list[VideoTranscriptChunkArtifact] = []
        transcript_sections: list[str] = []
        transcript_markdown_sections: list[str] = [
            f"# Transcript: {resolved_video_path.name}",
            "",
            f"- source video: `{resolved_video_path}`",
            f"- duration: `{_format_seconds(duration_seconds)}`",
            f"- chunk count: `{len(chunk_windows)}`",
            "",
        ]
        chunk_summaries: list[VideoSummary] = []

        for chunk_index, (start_seconds, end_seconds) in enumerate(chunk_windows, start=1):
            chunk_audio_path = chunk_audio_dir / f"chunk_{chunk_index:03d}.mp3"
            chunk_transcript_path = chunk_text_dir / f"chunk_{chunk_index:03d}.txt"
            self._render_audio_chunk(
                video_path=resolved_video_path,
                output_path=chunk_audio_path,
                start_seconds=start_seconds,
                duration_seconds=max(1, end_seconds - start_seconds),
            )
            transcript_text = self._transcribe_audio_chunk(chunk_audio_path).strip()
            chunk_transcript_path.write_text(transcript_text + "\n", encoding="utf-8")
            transcript_sections.append(
                (
                    f"[Chunk {chunk_index} | {_format_seconds(start_seconds)}"
                    f" - {_format_seconds(end_seconds)}]\n{transcript_text}"
                ).strip()
            )
            transcript_markdown_sections.extend(
                [
                    (
                        f"## Chunk {chunk_index}"
                        f" ({_format_seconds(start_seconds)} - {_format_seconds(end_seconds)})"
                    ),
                    "",
                    transcript_text or "_No spoken content detected in this chunk._",
                    "",
                ]
            )
            chunk_summary_text: str | None = None
            if summarize and transcript_text:
                chunk_summary = self._summarize_chunk(
                    transcript_text=transcript_text,
                    chunk_index=chunk_index,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    source_video_path=resolved_video_path,
                )
                chunk_summaries.append(chunk_summary)
                chunk_summary_text = chunk_summary.summary_text
            chunk_artifacts.append(
                VideoTranscriptChunkArtifact(
                    chunk_index=chunk_index,
                    start_seconds=start_seconds,
                    end_seconds=end_seconds,
                    duration_seconds=max(1, end_seconds - start_seconds),
                    audio_file_path=str(chunk_audio_path),
                    transcript_file_path=str(chunk_transcript_path),
                    transcript_char_count=len(transcript_text),
                    summary_text=chunk_summary_text,
                )
            )

        transcript_text_path = resolved_output_dir / "transcript.txt"
        transcript_markdown_path = resolved_output_dir / "transcript.md"
        transcript_text = "\n\n".join(section for section in transcript_sections if section).strip()
        transcript_text_path.write_text(transcript_text + "\n", encoding="utf-8")
        transcript_markdown_path.write_text(
            "\n".join(transcript_markdown_sections).rstrip() + "\n",
            encoding="utf-8",
        )

        summary: VideoSummary | None = None
        summary_markdown_path: Path | None = None
        if summarize:
            if not chunk_summaries:
                summary = VideoSummary(
                    summary_text="No spoken content detected in the video transcript.",
                    key_points=[],
                    action_items=[],
                    decisions=[],
                    open_questions=[],
                )
            elif len(chunk_summaries) == 1:
                summary = chunk_summaries[0]
            else:
                summary = self._synthesize_final_summary(
                    chunk_summaries=chunk_summaries,
                    source_video_path=resolved_video_path,
                    duration_seconds=duration_seconds,
                )
            summary_markdown_path = resolved_output_dir / "summary.md"
            summary_markdown_path.write_text(
                _render_summary_markdown(
                    summary=summary,
                    video_path=resolved_video_path,
                    duration_seconds=duration_seconds,
                    chunk_count=len(chunk_artifacts),
                ),
                encoding="utf-8",
            )

        analysis_json_path = resolved_output_dir / "analysis.json"
        result = VideoAnalysisResult(
            source_video_path=str(resolved_video_path),
            output_dir=str(resolved_output_dir),
            duration_seconds=duration_seconds,
            chunk_duration_seconds=self.chunk_duration_seconds,
            chunk_count=len(chunk_artifacts),
            transcript_char_count=len(transcript_text),
            transcript_text_path=str(transcript_text_path),
            transcript_markdown_path=str(transcript_markdown_path),
            summary_markdown_path=str(summary_markdown_path) if summary_markdown_path else None,
            analysis_json_path=str(analysis_json_path),
            summary=summary,
            chunks=chunk_artifacts,
        )
        analysis_json_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        elapsed_ms = int((perf_counter() - started) * 1000)
        record = ToolExecutionRecord(
            tool_id=self.tool_definition.tool_id,
            tool_kind=self.tool_definition.kind,
            status=ToolExecutionStatus.COMPLETED,
            details={
                "provider": self.provider,
                "transcription_model": self.transcription_model,
                "summary_model": self.summary_model,
                "transcription_backend": self.transcription_backend,
                "video_path": str(resolved_video_path),
                "output_dir": str(resolved_output_dir),
                "duration_seconds": duration_seconds,
                "chunk_duration_seconds": self.chunk_duration_seconds,
                "chunk_count": len(chunk_artifacts),
                "transcript_char_count": len(transcript_text),
                "summary_enabled": summarize,
                "elapsed_ms": elapsed_ms,
            },
        )
        return result, record

    def _probe_video_duration_seconds(self, video_path: Path) -> int:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "json",
                    str(video_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffprobe is required for video analysis but is not installed."
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                "Unable to inspect video duration with ffprobe: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        payload = json.loads(completed.stdout or "{}")
        raw_duration = payload.get("format", {}).get("duration")
        try:
            duration_seconds = float(str(raw_duration))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("ffprobe returned an invalid video duration.") from exc
        if duration_seconds <= 0:
            raise RuntimeError("Video duration must be greater than zero.")
        return max(1, math.ceil(duration_seconds))

    def _render_audio_chunk(
        self,
        *,
        video_path: Path,
        output_path: Path,
        start_seconds: int,
        duration_seconds: int,
    ) -> None:
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    str(start_seconds),
                    "-t",
                    str(duration_seconds),
                    "-i",
                    str(video_path),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "ffmpeg is required for video analysis but is not installed."
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed while extracting audio from the video: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        if not output_path.exists():
            raise RuntimeError(f"Expected ffmpeg to write audio chunk: {output_path}")

    def _transcribe_audio_chunk(self, audio_path: Path) -> str:
        if self.transcription_backend != "openai":
            return self._transcribe_audio_chunk_local(audio_path)
        client = self.transcription_client or self._build_transcription_client()
        request_kwargs: dict[str, Any] = {
            "model": self.transcription_model,
            "file": audio_path.open("rb"),
            "response_format": "text",
        }
        if self.language:
            request_kwargs["language"] = self.language
        try:
            with request_kwargs["file"]:
                response = client.audio.transcriptions.create(**request_kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"OpenAI transcription failed for {audio_path.name}: {exc}"
            ) from exc
        if isinstance(response, str):
            return response
        text_value = getattr(response, "text", None)
        if isinstance(text_value, str):
            return text_value
        return str(response).strip()

    def _transcribe_audio_chunk_local(self, audio_path: Path) -> str:
        model = self._local_whisper_model or self._build_local_whisper_model()
        segments, _info = model.transcribe(
            str(audio_path),
            language=self.language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        texts: list[str] = []
        for segment in segments:
            text = getattr(segment, "text", "")
            if text:
                texts.append(str(text).strip())
        return " ".join(part for part in texts if part).strip()

    def _summarize_chunk(
        self,
        *,
        transcript_text: str,
        chunk_index: int,
        start_seconds: int,
        end_seconds: int,
        source_video_path: Path,
    ) -> VideoSummary:
        payload = {
            "analysis_mode": "transcript_chunk",
            "source_video_path": str(source_video_path),
            "chunk_index": chunk_index,
            "start_seconds": start_seconds,
            "end_seconds": end_seconds,
            "transcript_text": transcript_text,
        }
        return self._run_summary_prompt(payload)

    def _synthesize_final_summary(
        self,
        *,
        chunk_summaries: list[VideoSummary],
        source_video_path: Path,
        duration_seconds: int,
    ) -> VideoSummary:
        payload = {
            "analysis_mode": "summary_rollup",
            "source_video_path": str(source_video_path),
            "duration_seconds": duration_seconds,
            "chunk_summaries": [summary.model_dump(mode="json") for summary in chunk_summaries],
        }
        return self._run_summary_prompt(payload)

    def _run_summary_prompt(self, payload: dict[str, Any]) -> VideoSummary:
        prompt_text = (
            f"{self.prompt.instructions.strip()}\n\n"
            "VIDEO_ANALYSIS_INPUT_JSON:\n"
            f"{json.dumps(payload, sort_keys=True)}\n\n"
            "Return one JSON object that matches the video summary schema exactly.\n"
            "Stay grounded in the provided transcript text or chunk summaries only.\n"
            "Do not infer anything from visuals that are not explicitly stated in the transcript.\n"
        )
        client = self.summary_client or self._build_summary_client()
        response = client.classify(
            prompt=prompt_text,
            model=self.summary_model,
            response_schema=VideoSummary.model_json_schema(),
            response_model=VideoSummary,
            max_output_tokens=self.max_output_tokens,
        )
        return VideoSummary.model_validate(response.payload)

    def _build_transcription_client(self) -> Any:
        if not self.settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured for video transcription.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("The openai package is not installed.") from exc
        client = OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=(self.settings.openai_base_url or "https://api.openai.com/v1").rstrip("/"),
            timeout=self.timeout_seconds,
        )
        return instrument_openai_client(
            client,
            settings=self.settings,
            run_name=self.tool_definition.tool_id,
            metadata={
                "tool_id": self.tool_definition.tool_id,
                "tool_kind": self.tool_definition.kind,
                "provider": self.provider,
                "operation": "video_transcription",
            },
        )

    def _build_summary_client(self) -> OpenAILLMClient:
        return OpenAILLMClient(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_base_url,
            timeout_seconds=self.timeout_seconds,
            settings=self.settings,
            tracing_name=self.tool_definition.tool_id,
            tracing_metadata={
                "tool_id": self.tool_definition.tool_id,
                "tool_kind": self.tool_definition.kind,
                "provider": self.provider,
                "operation": "video_summary",
            },
        )

    def _build_local_whisper_model(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "Local transcription requires the faster-whisper package. "
                "Install repo dependencies again or use the OpenAI transcription backend."
            ) from exc

        resolved_device = self.local_transcription_device
        if resolved_device == "auto":
            resolved_device = "cpu"
        if (
            platform.system() == "Darwin"
            and platform.machine().lower() in {"arm64", "aarch64"}
            and resolved_device == "cpu"
        ):
            # faster-whisper currently uses CPU on macOS; keep the choice explicit.
            resolved_device = "cpu"

        self._local_whisper_model = WhisperModel(
            self.local_transcription_model,
            device=resolved_device,
            compute_type=self.local_transcription_compute_type,
        )
        return self._local_whisper_model


def _build_chunk_windows(
    *,
    duration_seconds: int,
    chunk_duration_seconds: int,
) -> list[tuple[int, int]]:
    windows: list[tuple[int, int]] = []
    start_seconds = 0
    while start_seconds < duration_seconds:
        end_seconds = min(duration_seconds, start_seconds + chunk_duration_seconds)
        windows.append((start_seconds, end_seconds))
        start_seconds = end_seconds
    return windows


def _render_summary_markdown(
    *,
    summary: VideoSummary,
    video_path: Path,
    duration_seconds: int,
    chunk_count: int,
) -> str:
    lines = [
        f"# Video Summary: {video_path.name}",
        "",
        f"- source video: `{video_path}`",
        f"- duration: `{_format_seconds(duration_seconds)}`",
        f"- chunk count: `{chunk_count}`",
        "",
        "## Summary",
        "",
        summary.summary_text,
        "",
        "## Key Points",
        "",
    ]
    lines.extend([f"- {item}" for item in summary.key_points] or ["- none"])
    lines.extend(["", "## Action Items", ""])
    lines.extend([f"- {item}" for item in summary.action_items] or ["- none"])
    lines.extend(["", "## Decisions", ""])
    lines.extend([f"- {item}" for item in summary.decisions] or ["- none"])
    lines.extend(["", "## Open Questions", ""])
    lines.extend([f"- {item}" for item in summary.open_questions] or ["- none", ""])
    return "\n".join(lines).rstrip() + "\n"


def _format_seconds(total_seconds: int) -> str:
    hours, remainder = divmod(max(0, total_seconds), 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _resolve_positive_int(value: object, *, default: int) -> int:
    if value in {None, ""}:
        return default
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _resolve_optional_positive_int(value: object) -> int | None:
    if value in {None, ""}:
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return max(1, parsed)
