"""Safe cron output-file access and markdown projection."""

from __future__ import annotations

import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path


CONTENT_LIMIT = 8000
HEADER_CONTEXT = 200
_JOB_ID_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}")


class InvalidCronOutputPath(ValueError):
    """A job ID or filename cannot name a cron output file."""


class CronRunNotFound(FileNotFoundError):
    """The requested cron output does not exist as a regular markdown file."""


def validate_job_id(job_id: str) -> str:
    value = str(job_id or "")
    if value in (".", "..") or not _JOB_ID_RE.fullmatch(value):
        raise InvalidCronOutputPath("invalid job_id")
    return value


def _validate_filename(filename: str) -> str:
    value = str(filename or "")
    if not value or Path(value).name != value or not value.endswith(".md"):
        raise InvalidCronOutputPath("invalid filename")
    return value


def response_marker_index(text: str) -> int:
    candidates = []
    for heading in ("## Response", "# Response"):
        if text.startswith(heading):
            candidates.append(0)
        index = text.find(f"\n{heading}")
        if index >= 0:
            candidates.append(index + 1)
    return min(candidates) if candidates else -1


def content_window(text: str, limit: int = CONTENT_LIMIT) -> str:
    """Return bounded output while preserving the useful response section."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    response_index = response_marker_index(text)
    if response_index >= 0:
        header = text[: min(HEADER_CONTEXT, response_index)].rstrip()
        response = text[response_index:].lstrip("\n")
        content = f"{header}\n...\n{response}" if header else response
        return content[:limit]
    return text[-limit:]


def usage_metadata(text: str) -> dict:
    """Extract optional token, cost, duration, model, and provider metadata."""
    head = text.split("## Response", 1)[0].split("# Response", 1)[0]
    usage: dict = {}

    def intish(value: str):
        cleaned = re.sub(r"[^0-9]", "", value or "")
        return int(cleaned) if cleaned else None

    def floatish(value: str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?", (value or "").replace(",", ""))
        return float(match.group(0)) if match else None

    for raw_line in head.splitlines():
        line = raw_line.strip()
        model_match = re.match(r"\*\*(?:Model|Model Used):\*\*\s*(.+)$", line, re.I)
        if model_match:
            usage["model"] = model_match.group(1).strip()
            continue
        provider_match = re.match(r"\*\*Provider:\*\*\s*(.+)$", line, re.I)
        if provider_match:
            usage["provider"] = provider_match.group(1).strip()
            continue
        cost_match = re.match(r"\*\*(?:Estimated cost|Cost):\*\*\s*(.+)$", line, re.I)
        if cost_match:
            cost = floatish(cost_match.group(1))
            if cost is not None:
                usage["estimated_cost_usd"] = cost
            continue
        duration_match = re.match(r"\*\*(?:Duration|Elapsed):\*\*\s*(.+)$", line, re.I)
        if duration_match:
            seconds = floatish(duration_match.group(1))
            if seconds is not None:
                usage["duration_seconds"] = seconds
            continue
        tokens_match = re.match(r"\*\*Tokens:\*\*\s*(.+)$", line, re.I)
        if not tokens_match:
            continue
        value = tokens_match.group(1)
        input_match = re.search(r"([0-9][0-9,]*)\s*(?:input|in)\b", value, re.I)
        output_match = re.search(r"([0-9][0-9,]*)\s*(?:output|out)\b", value, re.I)
        total_match = re.search(
            r"([0-9][0-9,]*)\s*(?:total\s*)?tokens?\b", value, re.I
        )
        if input_match:
            usage["input_tokens"] = intish(input_match.group(1))
        if output_match:
            usage["output_tokens"] = intish(output_match.group(1))
        if total_match:
            usage["total_tokens"] = intish(total_match.group(1))

    if "total_tokens" not in usage:
        total = sum(int(usage.get(key) or 0) for key in ("input_tokens", "output_tokens"))
        if total:
            usage["total_tokens"] = total
    return usage


def response_snippet(text: str, limit: int = 600) -> str:
    lines = text.split("\n")
    response_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith("## Response") or line.startswith("# Response")
        ),
        -1,
    )
    body = "\n".join(lines[response_index + 1 :] if response_index >= 0 else lines)
    return body.strip()[:limit] or "(empty)"


class CronOutputStore:
    """Read one profile's cron outputs through held directory/file handles."""

    def __init__(self, root: Path):
        self.root = Path(root)

    @contextmanager
    def _open_job_dir(self, job_id: str):
        value = validate_job_id(job_id)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.root / value, flags)
        except FileNotFoundError:
            yield None
            return
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _regular_markdown_entries(descriptor: int) -> list[tuple[str, os.stat_result]]:
        entries = []
        for name in os.listdir(descriptor):
            if not name.endswith(".md") or Path(name).name != name:
                continue
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                entries.append((name, info))
        return entries

    @staticmethod
    def _read_at(descriptor: int, filename: str) -> str:
        name = _validate_filename(filename)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            file_descriptor = os.open(name, flags, dir_fd=descriptor)
        except (FileNotFoundError, OSError) as error:
            raise CronRunNotFound(name) from error
        try:
            info = os.fstat(file_descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise CronRunNotFound(name)
            with os.fdopen(file_descriptor, "r", encoding="utf-8", errors="replace") as stream:
                file_descriptor = -1
                return stream.read()
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)

    def list_runs(self, job_id: str, *, offset: int, limit: int) -> tuple[list[dict], int]:
        with self._open_job_dir(job_id) as descriptor:
            if descriptor is None:
                return [], 0
            entries = sorted(
                self._regular_markdown_entries(descriptor),
                key=lambda entry: entry[1].st_mtime,
                reverse=True,
            )
            page = entries[offset : offset + limit]
            runs = []
            for filename, info in page:
                try:
                    text = self._read_at(descriptor, filename)
                except CronRunNotFound:
                    continue
                runs.append(
                    {
                        "filename": filename,
                        "size": info.st_size,
                        "modified": info.st_mtime,
                        "usage": usage_metadata(text),
                    }
                )
            return runs, len(entries)

    def read_run(self, job_id: str, filename: str) -> str:
        with self._open_job_dir(job_id) as descriptor:
            if descriptor is None:
                raise CronRunNotFound(filename)
            return self._read_at(descriptor, filename)

    def recent_outputs(self, job_id: str, *, limit: int) -> list[dict]:
        with self._open_job_dir(job_id) as descriptor:
            if descriptor is None:
                return []
            entries = sorted(
                self._regular_markdown_entries(descriptor),
                key=lambda entry: entry[1].st_mtime,
                reverse=True,
            )[:limit]
            outputs = []
            for filename, _ in entries:
                try:
                    text = self._read_at(descriptor, filename)
                except CronRunNotFound:
                    continue
                outputs.append({"filename": filename, "content": content_window(text)})
            return outputs


def active_output_store() -> CronOutputStore:
    from cron.jobs import OUTPUT_DIR

    return CronOutputStore(OUTPUT_DIR)
