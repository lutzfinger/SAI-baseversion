"""CLI entrypoint for transcript-based video analysis."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.control_plane.loaders import PromptStore
from app.shared.config import get_settings
from app.shared.models import WorkflowToolDefinition
from app.tools.video_analysis import VideoTranscriptAnalyzerTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a local video file and optionally summarize the transcript."
    )
    parser.add_argument("video_path", help="Absolute or relative path to the local video file.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional directory for transcript and summary artifacts.",
    )
    parser.add_argument(
        "--transcript-only",
        action="store_true",
        help="Skip transcript summarization and write transcript artifacts only.",
    )
    parser.add_argument(
        "--chunk-duration-seconds",
        type=int,
        default=600,
        help="Audio chunk size used for transcription. Default: 600 seconds.",
    )
    parser.add_argument(
        "--transcription-model",
        default="gpt-4o-transcribe",
        help="OpenAI transcription model to use. Default: gpt-4o-transcribe.",
    )
    parser.add_argument(
        "--transcription-backend",
        choices=["openai", "faster_whisper"],
        default="openai",
        help="Transcription backend to use. Default: openai.",
    )
    parser.add_argument(
        "--local-transcription-model",
        default="small",
        help="Local faster-whisper model size or path. Default: small.",
    )
    parser.add_argument(
        "--local-transcription-device",
        default="auto",
        help="Local faster-whisper device. Default: auto.",
    )
    parser.add_argument(
        "--local-transcription-compute-type",
        default="int8",
        help="Local faster-whisper compute type. Default: int8.",
    )
    parser.add_argument(
        "--summary-model",
        default="gpt-5.2",
        help="OpenAI text model used for structured transcript summaries. Default: gpt-5.2.",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Optional language hint for transcription, for example en or de.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = get_settings()
    prompt_store = PromptStore(settings.prompts_dir)
    prompt = prompt_store.load("video/summary.md")
    output_dir = Path(args.output_dir) if args.output_dir else _default_output_dir(
        video_path=Path(args.video_path),
        artifacts_dir=settings.artifacts_dir,
    )
    tool_definition = WorkflowToolDefinition(
        tool_id="video_transcript_analyzer",
        kind="video_transcript_analyzer",
        prompt="video/summary.md",
        provider="openai",
        model=args.summary_model,
        config={
            "chunk_duration_seconds": args.chunk_duration_seconds,
            "transcription_model": args.transcription_model,
            "transcription_backend": args.transcription_backend,
            "local_transcription_model": args.local_transcription_model,
            "local_transcription_device": args.local_transcription_device,
            "local_transcription_compute_type": args.local_transcription_compute_type,
            **({"language": args.language} if args.language else {}),
        },
    )
    tool = VideoTranscriptAnalyzerTool(
        tool_definition=tool_definition,
        prompt=prompt,
        settings=settings,
    )
    try:
        result, record = tool.analyze(
            video_path=args.video_path,
            output_dir=output_dir,
            summarize=not args.transcript_only,
        )
    except Exception as error:
        print(f"Video analysis failed: {error}", file=sys.stderr)
        return 1

    payload = {
        "result": result.model_dump(mode="json"),
        "tool_record": record.model_dump(mode="json"),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _default_output_dir(*, video_path: Path, artifacts_dir: Path) -> Path:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", video_path.stem).strip("._") or "video"
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return artifacts_dir / f"video_analysis_{stem}_{timestamp}"


if __name__ == "__main__":
    raise SystemExit(main())
