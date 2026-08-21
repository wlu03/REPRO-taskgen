from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
ARTICLE_DOI_RE = re.compile(r"10\.18718/81781\.\d+", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_whitespace(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    text = unquote(value).strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    match = DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;:)]}>'\"")
    return doi.lower()


def extract_doi(value: str | None) -> str | None:
    return normalize_doi(value)


def extract_dois(value: str | None) -> set[str]:
    """Return every normalized DOI in a string, not only the first one."""
    if not value:
        return set()
    text = unquote(value)
    return {
        normalized
        for match in DOI_RE.finditer(text)
        if (normalized := normalize_doi(match.group(0))) is not None
    }


def extract_article_doi(value: str | None) -> str | None:
    if not value:
        return None
    match = ARTICLE_DOI_RE.search(unquote(value))
    if not match:
        return None
    return match.group(0).lower()


def record_id_from_doi(journal_code: str, doi: str | None, fallback_seed: str) -> str:
    prefix = re.sub(r"[^A-Za-z0-9]+", "_", journal_code.upper()).strip("_") or "JCRE"
    if doi:
        suffix = doi.split("/", 1)[-1]
        suffix = re.sub(r"[^A-Za-z0-9]+", "_", suffix).strip("_")
        return f"{prefix}_{suffix}"
    digest = hashlib.sha256(fallback_seed.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_NO_DOI_{digest}"


def safe_filename(value: str | None, fallback: str = "download.bin", max_length: int = 180) -> str:
    candidate = unquote(value or "").strip()
    candidate = os.path.basename(candidate.replace("\\", "/"))
    candidate = unicodedata.normalize("NFKC", candidate)
    candidate = "".join(ch for ch in candidate if ch >= " " and ch != "\x7f")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    if not candidate or candidate in {".", ".."}:
        candidate = fallback
    stem, suffix = os.path.splitext(candidate)
    if len(candidate) > max_length:
        keep = max(1, max_length - len(suffix))
        candidate = stem[:keep].rstrip(" .") + suffix[:20]
    return candidate or fallback


def filename_from_url(url: str | None, fallback: str = "download.bin") -> str:
    if not url:
        return fallback
    path = urlparse(url).path
    name = os.path.basename(path.rstrip("/"))
    if name.lower() in {"download", "resource"} or not name:
        return fallback
    return safe_filename(name, fallback=fallback)


def parse_content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", value, flags=re.IGNORECASE)
    if utf8_match:
        return safe_filename(unquote(utf8_match.group(1)))
    plain_match = re.search(r'filename\s*=\s*"?([^";]+)"?', value, flags=re.IGNORECASE)
    if plain_match:
        return safe_filename(plain_match.group(1))
    return None


def parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def path_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
