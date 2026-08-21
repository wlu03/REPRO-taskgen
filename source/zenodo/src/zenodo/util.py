from __future__ import annotations

import hashlib
import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def parse_json_bytes(payload: bytes, source: str) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid JSON from {source}: {exc}") from exc


_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def safe_component(value: Any, *, fallback_prefix: str = "item", max_length: int = 120) -> str:
    original = str(value or "").strip()
    cleaned = _UNSAFE_COMPONENT.sub("_", original).strip("._-")
    if cleaned in {"", ".", ".."}:
        digest = hashlib.sha256(original.encode("utf-8", "replace")).hexdigest()[:12]
        cleaned = f"{fallback_prefix}-{digest}"
    if cleaned.casefold().split(".", 1)[0] in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    if len(cleaned) > max_length:
        digest = hashlib.sha256(original.encode("utf-8", "replace")).hexdigest()[:12]
        cleaned = f"{cleaned[: max_length - 13]}-{digest}"
    return cleaned


def safe_filename(key: Any) -> str:
    raw = str(key or "file").replace("\\", "/")
    basename = raw.rsplit("/", 1)[-1]
    return safe_component(basename, fallback_prefix="file", max_length=180)


def stable_file_id(key: Any) -> str:
    digest = hashlib.sha256(str(key or "").encode("utf-8", "replace")).hexdigest()[:16]
    return f"f-{digest}"


def ensure_within(root: Path, candidate: Path) -> Path:
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_root and resolved_root not in resolved_candidate.parents:
        raise ValueError(f"Unsafe path outside output root: {candidate}")
    return resolved_candidate


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def html_to_text(value: Any) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(str(value))
        parser.close()
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]*>", " ", str(value))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def localized_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("en", "en_US", "default"):
            if value.get(key):
                return str(value[key])
        for candidate in value.values():
            if candidate:
                return str(candidate)
    return ""


def human_bytes(value: int | None) -> str:
    if value is None:
        return "unknown"
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024
    return f"{value} B"


def parse_checksum(value: Any) -> tuple[str, str] | None:
    if not value:
        return None
    text = str(value).strip().lower()
    if ":" in text:
        algorithm, digest = text.split(":", 1)
    elif re.fullmatch(r"[0-9a-f]{32}", text):
        algorithm, digest = "md5", text
    elif re.fullmatch(r"[0-9a-f]{64}", text):
        algorithm, digest = "sha256", text
    else:
        return None
    if algorithm not in hashlib.algorithms_available or not re.fullmatch(r"[0-9a-f]+", digest):
        return None
    return algorithm, digest


def hash_file(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm, usedforsecurity=False)
    except TypeError:
        digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def file_matches(path: Path, expected_size: int | None, checksum: Any) -> bool:
    if not path.is_file():
        return False
    if expected_size is not None and path.stat().st_size != expected_size:
        return False
    parsed = parse_checksum(checksum)
    if parsed:
        algorithm, expected = parsed
        return hash_file(path, algorithm) == expected
    return expected_size is not None
