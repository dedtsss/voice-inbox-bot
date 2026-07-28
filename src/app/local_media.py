from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.config import Settings
from app.subscription_queue import SubscriptionQueueBundle
from app.voice_processor import DriveOriginal, classify_media


class MediaTemporaryError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedMedia:
    extracted_text: str
    sanitized_manifest: dict[str, Any]
    image_paths: list[Path]
    all_essential_processed: bool
    review_reasons: list[str] = field(default_factory=list)


class LocalMediaProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def prepare(self, bundle: SubscriptionQueueBundle, prepared_dir: Path) -> PreparedMedia:
        prepared_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        text_parts: list[str] = []
        image_paths: list[Path] = []
        reasons: list[str] = []

        self._append_text(text_parts, bundle.item.raw_text)
        self._append_text(text_parts, bundle.manifest.get("text"))

        for original in bundle.originals:
            kind = self._kind(original)
            if kind == "text":
                try:
                    self._append_text(text_parts, original.path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError):
                    reasons.append("text_original_unreadable")
                continue
            if kind == "audio":
                transcript, ambiguous = self._transcribe(original.path, prepared_dir)
                self._append_text(text_parts, transcript)
                if ambiguous:
                    reasons.append("audio_transcription_ambiguous")
                continue
            if kind == "image":
                image_path = self._prepare_image(original.path, prepared_dir, len(image_paths) + 1)
                if image_path is None:
                    reasons.append("image_unreadable")
                    continue
                if len(image_paths) >= max(1, self.settings.subscription_max_images):
                    reasons.append("image_limit_exceeded")
                    continue
                image_paths.append(image_path)
                self._append_text(text_parts, self._ocr_image(image_path))
                self._append_text(text_parts, self._image_metadata(image_path))
                continue
            if kind == "pdf":
                pdf_text, pdf_images, pdf_reasons = self._extract_pdf(original.path, prepared_dir)
                self._append_text(text_parts, pdf_text)
                available = max(0, self.settings.subscription_max_images - len(image_paths))
                image_paths.extend(pdf_images[:available])
                if len(pdf_images) > available:
                    pdf_reasons.append("image_limit_exceeded")
                reasons.extend(pdf_reasons)
                continue
            if kind == "video":
                video_text, frames, video_reasons = self._extract_video(original.path, prepared_dir)
                self._append_text(text_parts, video_text)
                available = max(0, self.settings.subscription_max_images - len(image_paths))
                image_paths.extend(frames[:available])
                if len(frames) > available:
                    video_reasons.append("image_limit_exceeded")
                reasons.extend(video_reasons)
                continue
            reasons.append("unsupported_original")

        extracted_text = "\n\n".join(part for part in text_parts if part).strip()
        if not extracted_text and not image_paths:
            reasons.append("content_empty")
        unique_reasons = list(dict.fromkeys(reasons))
        return PreparedMedia(
            extracted_text=extracted_text,
            sanitized_manifest=sanitize_manifest(bundle.manifest, bundle.originals),
            image_paths=image_paths,
            all_essential_processed=not unique_reasons,
            review_reasons=unique_reasons,
        )

    @staticmethod
    def _append_text(parts: list[str], value: Any) -> None:
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    @staticmethod
    def _kind(original: DriveOriginal) -> str:
        mime_type = original.mime_type.split(";", 1)[0].strip().casefold()
        suffix = original.path.suffix.casefold()
        if mime_type == "application/pdf" or suffix == ".pdf":
            return "pdf"
        if mime_type.startswith("text/") or suffix in {".txt", ".md", ".csv", ".json", ".log"}:
            return "text"
        return classify_media(original)

    def _transcribe(self, media_path: Path, prepared_dir: Path) -> tuple[str, bool]:
        result_path = prepared_dir / f"stt_{len(list(prepared_dir.glob('stt_*.json'))) + 1:03d}.json"
        command = [
            sys.executable,
            "-m",
            "app.local_media",
            "transcribe",
            "--input",
            str(media_path),
            "--output",
            str(result_path),
            "--model",
            self.settings.subscription_stt_model,
            "--device",
            self.settings.subscription_stt_device,
            "--compute-type",
            self.settings.subscription_stt_compute_type,
            "--language",
            self.settings.subscription_stt_language,
            "--cache-dir",
            self.settings.subscription_stt_cache_dir,
        ]
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": _absolute_pythonpath(),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
            "HF_HOME": self.settings.subscription_stt_cache_dir,
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
        }
        try:
            completed = subprocess.run(
                command,
                cwd=prepared_dir,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1, self.settings.subscription_stt_timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaTemporaryError("local_stt_timeout") from exc
        if completed.returncode != 0 or not result_path.is_file():
            raise MediaTemporaryError("local_stt_unavailable")
        try:
            if result_path.stat().st_size > 2_000_000:
                raise ValueError("STT result is too large")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            transcript = str(payload.get("text") or "").strip()
            ambiguous = bool(payload.get("ambiguous"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise MediaTemporaryError("local_stt_result_invalid") from exc
        finally:
            result_path.unlink(missing_ok=True)
        if not transcript:
            return "", True
        return transcript, ambiguous

    def _prepare_image(self, source: Path, prepared_dir: Path, index: int) -> Path | None:
        suffix = source.suffix.casefold()
        needs_resize = source.stat().st_size > max(1, self.settings.voice_processor_max_image_bytes)
        target_suffix = suffix if suffix in {".jpg", ".jpeg", ".png", ".webp"} and not needs_resize else ".jpg"
        target = prepared_dir / f"image_{index:03d}{target_suffix}"
        scale_filter = (
            f"scale='if(gt(iw,{self.settings.voice_processor_image_max_edge}),"
            f"{self.settings.voice_processor_image_max_edge},iw)':-2"
        )
        try:
            if target_suffix == suffix and not needs_resize:
                shutil.copyfile(source, target)
            else:
                self._run_media_command(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-y",
                        "-i",
                        str(source),
                        "-frames:v",
                        "1",
                        "-vf",
                        scale_filter,
                        str(target),
                    ],
                    "image_conversion_failed",
                )
        except (OSError, MediaTemporaryError):
            return None
        return target if target.is_file() and target.stat().st_size else None

    def _ocr_image(self, image_path: Path) -> str:
        if not shutil.which("tesseract"):
            return ""
        try:
            completed = subprocess.run(
                ["tesseract", str(image_path), "stdout", "-l", "rus+eng"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(1, self.settings.subscription_media_timeout_seconds),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if completed.returncode != 0 or len(completed.stdout) > self.settings.voice_processor_max_file_bytes:
            return ""
        return completed.stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _image_metadata(image_path: Path) -> str:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                width, height = image.size
                return f"Image metadata: width={width}, height={height}, format={image.format or 'unknown'}"
        except (ImportError, OSError, ValueError):
            return ""

    def _extract_pdf(self, source: Path, prepared_dir: Path) -> tuple[str, list[Path], list[str]]:
        required = ("pdfinfo", "pdftotext", "pdftoppm")
        if any(not shutil.which(binary) for binary in required):
            raise MediaTemporaryError("pdf_tools_unavailable")
        page_count = self._pdf_page_count(source)
        limit = max(1, self.settings.subscription_max_pdf_pages)
        pages = min(page_count or limit, limit)
        reasons = ["pdf_page_limit_exceeded"] if page_count > limit else []
        text_path = prepared_dir / f"pdf_{len(list(prepared_dir.glob('pdf_*.txt'))) + 1:03d}.txt"
        self._run_media_command(
            ["pdftotext", "-f", "1", "-l", str(pages), str(source), str(text_path)],
            "pdf_text_extraction_failed",
        )
        text = text_path.read_text(encoding="utf-8", errors="replace").strip() if text_path.exists() else ""
        text_path.unlink(missing_ok=True)
        if len(re.sub(r"\s+", "", text)) >= 40:
            return text, [], reasons

        render_dir = prepared_dir / f"pdf_scan_{len(list(prepared_dir.glob('pdf_scan_*'))) + 1:03d}"
        render_dir.mkdir(mode=0o700)
        prefix = render_dir / "page"
        self._run_media_command(
            [
                "pdftoppm",
                "-f",
                "1",
                "-l",
                str(pages),
                "-jpeg",
                "-scale-to",
                "1800",
                str(source),
                str(prefix),
            ],
            "pdf_render_failed",
        )
        images = sorted(render_dir.glob("page-*.jpg"))[:pages]
        ocr_parts = [self._ocr_image(path) for path in images]
        ocr_text = "\n\n".join(part for part in ocr_parts if part).strip()
        if not ocr_text:
            reasons.append("pdf_ocr_insufficient")
        return ocr_text, images, reasons

    def _pdf_page_count(self, source: Path) -> int:
        try:
            completed = subprocess.run(
                ["pdfinfo", str(source)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(1, self.settings.subscription_media_timeout_seconds),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaTemporaryError("pdf_info_failed") from exc
        if completed.returncode != 0:
            return 0
        match = re.search(rb"^Pages:\s+(\d+)\s*$", completed.stdout, re.MULTILINE)
        return int(match.group(1)) if match else 0

    def _extract_video(self, source: Path, prepared_dir: Path) -> tuple[str, list[Path], list[str]]:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise MediaTemporaryError("video_tools_unavailable")
        reasons: list[str] = []
        transcript = ""
        audio_path = prepared_dir / f"video_audio_{len(list(prepared_dir.glob('video_audio_*'))) + 1:03d}.wav"
        audio_result = self._run_media_command(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(audio_path),
            ],
            "video_audio_extraction_failed",
            allow_failure=True,
        )
        if audio_result and audio_path.is_file() and audio_path.stat().st_size:
            transcript, ambiguous = self._transcribe(audio_path, prepared_dir)
            if ambiguous:
                reasons.append("audio_transcription_ambiguous")

        duration = self._video_duration(source)
        frame_count = max(1, self.settings.subscription_max_video_frames)
        timestamps = _representative_timestamps(duration, frame_count)
        frame_dir = prepared_dir / f"video_frames_{len(list(prepared_dir.glob('video_frames_*'))) + 1:03d}"
        frame_dir.mkdir(mode=0o700)
        frames: list[Path] = []
        for index, timestamp in enumerate(timestamps, start=1):
            frame = frame_dir / f"frame_{index:03d}.jpg"
            succeeded = self._run_media_command(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale='if(gt(iw,1600),1600,iw)':-2",
                    str(frame),
                ],
                "video_frame_extraction_failed",
                allow_failure=True,
            )
            if succeeded and frame.is_file() and frame.stat().st_size:
                frames.append(frame)
        if not frames and not transcript:
            reasons.append("video_content_unavailable")
        return transcript, frames, reasons

    def _video_duration(self, source: Path) -> float:
        try:
            completed = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=max(1, self.settings.subscription_media_timeout_seconds),
                check=False,
            )
            return max(0.0, float(completed.stdout.decode("ascii", errors="ignore").strip()))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0.0

    def _run_media_command(self, command: list[str], code: str, *, allow_failure: bool = False) -> bool:
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1, self.settings.subscription_media_timeout_seconds),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            if allow_failure:
                return False
            raise MediaTemporaryError(code) from exc
        if completed.returncode != 0:
            if allow_failure:
                return False
            raise MediaTemporaryError(code)
        return True


def sanitize_manifest(manifest: dict[str, Any], originals: list[DriveOriginal]) -> dict[str, Any]:
    files = []
    for original in originals:
        files.append(
            {
                "ordinal": len(files) + 1,
                "mime_type": original.mime_type.split(";", 1)[0].strip().casefold(),
                "size": original.size,
            }
        )
    sanitized: dict[str, Any] = {"files": files}
    for key in ("created_at", "source", "type"):
        value = manifest.get(key)
        if isinstance(value, str) and value.strip():
            sanitized[key] = value.strip()[:500]
    return sanitized


def _representative_timestamps(duration: float, max_frames: int) -> list[float]:
    if duration <= 0:
        return [0.0]
    count = max(1, min(max_frames, math.ceil(duration / 5.0)))
    if count == 1:
        return [min(duration / 2.0, max(0.0, duration - 0.1))]
    last_timestamp = max(0.0, duration - 0.1)
    return [max(0.0, min(last_timestamp, duration * index / (count - 1))) for index in range(count)]


def _absolute_pythonpath() -> str:
    configured = os.environ.get("PYTHONPATH", "")
    if not configured:
        return str(Path(__file__).resolve().parents[1])
    resolved: list[str] = []
    for entry in configured.split(os.pathsep):
        if not entry:
            continue
        path = Path(entry)
        resolved.append(str(path if path.is_absolute() else path.resolve()))
    return os.pathsep.join(resolved) or str(Path(__file__).resolve().parents[1])


def _normalize_transcript(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _transcribe_once(model: Any, source: Path, language: str, *, normalized_pass: bool) -> tuple[str, float, float]:
    segments, info = model.transcribe(
        str(source),
        language=language,
        beam_size=8 if normalized_pass else 5,
        vad_filter=not normalized_pass,
        condition_on_previous_text=not normalized_pass,
    )
    materialized = list(segments)
    text = _normalize_transcript(" ".join(str(segment.text or "") for segment in materialized))
    log_probabilities = [float(segment.avg_logprob) for segment in materialized if segment.avg_logprob is not None]
    average_log_probability = sum(log_probabilities) / len(log_probabilities) if log_probabilities else -10.0
    language_probability = float(getattr(info, "language_probability", 0.0) or 0.0)
    return text, average_log_probability, language_probability


def run_transcription(args: argparse.Namespace) -> int:
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            args.model,
            device=args.device,
            compute_type=args.compute_type,
            download_root=args.cache_dir,
            local_files_only=True,
        )
        source = Path(args.input)
        first, average_log_probability, language_probability = _transcribe_once(
            model,
            source,
            args.language,
            normalized_pass=False,
        )
        ambiguous = not first or average_log_probability < -1.0 or language_probability < 0.65
        selected = first
        if ambiguous:
            normalized_source = source
            with tempfile.TemporaryDirectory(prefix="voice-inbox-stt-") as temp_name:
                normalized = Path(temp_name) / "normalized.wav"
                completed = subprocess.run(
                    [
                        "ffmpeg",
                        "-nostdin",
                        "-y",
                        "-i",
                        str(source),
                        "-af",
                        "loudnorm",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        str(normalized),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=120,
                    check=False,
                )
                if completed.returncode == 0 and normalized.is_file():
                    normalized_source = normalized
                second, second_log_probability, _ = _transcribe_once(
                    model,
                    normalized_source,
                    args.language,
                    normalized_pass=True,
                )
                if second and (not first or second_log_probability > average_log_probability):
                    selected = second
                if first and second:
                    similarity = SequenceMatcher(None, first.casefold(), second.casefold()).ratio()
                    ambiguous = similarity < 0.72 or max(average_log_probability, second_log_probability) < -1.0
                else:
                    ambiguous = not selected
        output = Path(args.output)
        output.write_text(json.dumps({"text": selected, "ambiguous": ambiguous}, ensure_ascii=False), encoding="utf-8")
        os.chmod(output, 0o600)
        return 0
    except Exception:  # noqa: BLE001 - the helper must fail closed without emitting library diagnostics
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local media helper for the subscription worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    transcribe = subparsers.add_parser("transcribe")
    transcribe.add_argument("--input", required=True)
    transcribe.add_argument("--output", required=True)
    transcribe.add_argument("--model", default="small")
    transcribe.add_argument("--device", default="cpu")
    transcribe.add_argument("--compute-type", default="int8")
    transcribe.add_argument("--language", default="ru")
    transcribe.add_argument("--cache-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "transcribe":
        return run_transcription(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
