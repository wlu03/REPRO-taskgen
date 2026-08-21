#!/usr/bin/env python3
"""Archive the public CODECHECK register and its linked public artifacts.

Discovery and record metadata come from CODECHECK's machine-readable JSON.
Repository packages are an explicit opt-in and are resolved with provider APIs.
Downloaded files are never extracted or executed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlparse, urlunparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


VERSION = "1.0.0"
REGISTER_URL = "https://codecheck.org.uk/register/register.json"
REGISTER_FULL_URL = "https://codecheck.org.uk/register/register-full.json"
REGISTER_META_URL = "https://codecheck.org.uk/register/.meta.json"
DEFAULT_USER_AGENT = (
    f"codecheck-register-scraper/{VERSION} "
    "(+https://codecheck.org.uk/register/)"
)
SUPPORTED_PROVIDERS = {"github", "gitlab", "osf", "zenodo"}
CERTIFICATE_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{3}(?:/[0-9]{4}-[0-9]{3})?$")
CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
RESERVED_DOWNLOAD_NAMES = {".download.part", ".download.part.json"}
URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+")


class ScraperError(RuntimeError):
    """A controlled scraper failure."""


class ArtifactSkipped(ScraperError):
    """A download intentionally skipped by a safety guard."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    return hash_file(path, "sha256", chunk_size)


def hash_file(path: Path, algorithm: str, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
    ).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def sanitize_segment(
    value: str,
    fallback: str = "unnamed",
    max_length: int = 180,
    max_bytes: int = 240,
) -> str:
    """Return a safe single path segment without hiding path traversal."""
    value = unicodedata.normalize("NFKC", str(value))
    value = CONTROL_CHARS_RE.sub("_", value)
    value = value.replace("/", "__").replace("\\", "__")
    value = re.sub(r"\s+", " ", value).strip(" .")
    if value in {"", ".", ".."}:
        value = fallback
    if len(value) > max_length or len(value.encode("utf-8")) > max_bytes:
        stem, suffix = os.path.splitext(value)
        suffix = suffix[:20]
        marker = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
        tail = f"-{marker}{suffix}"
        char_budget = max(1, max_length - len(tail))
        byte_budget = max(1, max_bytes - len(tail.encode("utf-8")))
        shortened = stem[:char_budget].encode("utf-8")[:byte_budget].decode(
            "utf-8", errors="ignore"
        )
        value = f"{shortened or fallback[:1]}{tail}"
    return value


def safe_relative_path(parts: Iterable[str]) -> Path:
    clean: list[str] = []
    for raw_part in parts:
        for part in re.split(r"[/\\]+", str(raw_part)):
            if not part or part == ".":
                continue
            if part == "..":
                raise ScraperError("Upstream path contains '..'")
            clean.append(sanitize_segment(part))
    return Path(*clean) if clean else Path("unnamed")


def ensure_http_url(url: Any, field: str = "URL") -> str:
    if not isinstance(url, str) or not url.strip():
        raise ScraperError(f"{field} is missing")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ScraperError(f"{field} is not an HTTP(S) URL: {url!r}")
    if parsed.username or parsed.password:
        raise ScraperError(f"{field} must not contain credentials")
    return url


def safe_final_url(url: str) -> tuple[str, bool]:
    """Remove temporary credentials from redirected object-storage URLs."""
    parsed = urlparse(url)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    sensitive = any(
        re.search(r"(?:signature|credential|token|secret|x-amz-|x-goog-)", key, re.I)
        for key, _ in query_items
    )
    if not sensitive:
        return url, False
    return urlunparse(parsed._replace(query="")), True


def redact_sensitive_urls(value: Any) -> str:
    """Redact temporary credentials from URLs embedded in error messages."""
    text = str(value)

    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;)]}":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        try:
            safe, _ = safe_final_url(raw)
        except Exception:
            safe = raw
        return safe + trailing

    return URL_IN_TEXT_RE.sub(replace, text)


def content_disposition_filename(value: str | None) -> str | None:
    if not value:
        return None
    message = Message()
    message["content-disposition"] = value
    filename = message.get_filename()
    return sanitize_segment(filename) if filename else None


def filename_from_url(url: str) -> str | None:
    path_parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if not path_parts:
        return None
    ignored = {"content", "download", "raw", "archive.zip"}
    candidate = path_parts[-1]
    if candidate.lower() in ignored and len(path_parts) > 1:
        candidate = path_parts[-2]
    candidate = sanitize_segment(candidate)
    return candidate if "." in candidate else None


def choose_download_filename(
    response: requests.Response,
    fallback: str,
) -> str:
    filename = content_disposition_filename(response.headers.get("Content-Disposition"))
    filename = filename or filename_from_url(response.url) or sanitize_segment(fallback)
    stem, suffix = os.path.splitext(filename)
    if not suffix or suffix == ".bin":
        media_type = normalize_media_type(response.headers.get("Content-Type"))
        guessed = mimetypes.guess_extension(media_type) if media_type else None
        if guessed:
            filename = f"{stem or 'download'}{guessed}"
    return sanitize_segment(filename)


def normalize_media_type(value: str | None) -> str | None:
    if not value:
        return None
    # Some storage services emit the same Content-Type header twice; Requests
    # joins duplicate values with a comma.
    media_type = value.split(",", 1)[0].split(";", 1)[0].strip().lower()
    return media_type or None


def content_length_value(value: str | None) -> int | None:
    if not value:
        return None
    candidate = value.split(",", 1)[0].strip()
    return int(candidate) if candidate.isdigit() else None


def is_html_response(response: requests.Response, first_chunk: bytes) -> bool:
    media_type = normalize_media_type(response.headers.get("Content-Type"))
    prefix = first_chunk[:512].lstrip().lower()
    return media_type in {"text/html", "application/xhtml+xml"} or prefix.startswith(
        (b"<!doctype html", b"<html")
    )


def stable_artifact_id(role: str, source_url: str, locator: str = "") -> str:
    value = f"{role}\n{source_url}\n{locator}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:16]


@dataclass(frozen=True)
class RepositoryReference:
    original: str | None
    provider: str | None
    locator: str | None
    subpath: str | None


def parse_repository_reference(value: Any) -> RepositoryReference:
    if value is None or value == "":
        return RepositoryReference(None, None, None, None)
    if not isinstance(value, str):
        raise ScraperError("Repository reference must be a string")
    original = value.strip()
    if "::" not in original:
        return RepositoryReference(original, None, original, None)
    provider, locator = original.split("::", 1)
    provider = provider.strip().lower()
    locator = locator.strip()
    if not provider or not locator:
        raise ScraperError(f"Invalid repository reference: {original!r}")
    subpath = None
    if "|" in locator:
        locator, subpath = locator.split("|", 1)
        locator = locator.strip()
        subpath = subpath.strip().strip("/") or None
        if subpath:
            safe_relative_path([subpath])
    return RepositoryReference(original, provider, locator, subpath)


def normalized_people(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    people: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            people.append({"name": item.strip(), "orcid": None})
        elif isinstance(item, Mapping) and item.get("name"):
            people.append(
                {
                    "name": str(item["name"]).strip(),
                    "orcid": item.get("orcid"),
                }
            )
    return people


def normalize_record(
    register_entry: Mapping[str, Any],
    detail: Mapping[str, Any] | None,
    full_entry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize one rendered register row plus its per-certificate JSON."""
    detail = detail if isinstance(detail, Mapping) else {}
    full_entry = full_entry if isinstance(full_entry, Mapping) else {}
    certificate = detail.get("certificate") if isinstance(detail.get("certificate"), Mapping) else {}
    paper = detail.get("paper") if isinstance(detail.get("paper"), Mapping) else {}
    check = detail.get("codecheck") if isinstance(detail.get("codecheck"), Mapping) else {}

    certificate_id = register_entry.get("Certificate ID") or certificate.get("id")
    if not isinstance(certificate_id, str) or not certificate_id.strip():
        raise ScraperError("Register entry has no Certificate ID")
    certificate_id = certificate_id.strip()
    if not CERTIFICATE_ID_RE.fullmatch(certificate_id):
        raise ScraperError(f"Unexpected Certificate ID: {certificate_id!r}")
    detail_certificate_id = certificate.get("id")
    if (
        detail_certificate_id is not None
        and str(detail_certificate_id).strip() != certificate_id
    ):
        raise ScraperError(
            f"Certificate detail ID {detail_certificate_id!r} does not match "
            f"register ID {certificate_id!r}"
        )

    repository_descriptor = register_entry.get("Repository") or check.get("repository")
    repository = parse_repository_reference(repository_descriptor)
    abstract = paper.get("abstract")
    abstract_text = None
    abstract_source = None
    if isinstance(abstract, Mapping):
        abstract_text = abstract.get("text")
        abstract_source = abstract.get("source")
    elif isinstance(abstract, str):
        abstract_text = abstract

    authors = normalized_people(paper.get("authors"))
    if not authors:
        authors = normalized_people(
            full_entry.get("Paper authors") or full_entry.get("Authors")
        )
    codecheckers = normalized_people(check.get("codecheckers"))
    if not codecheckers:
        codecheckers = normalized_people(full_entry.get("Codecheckers"))

    manifest = check.get("manifest") if isinstance(check.get("manifest"), list) else []
    check_time = check.get("check_time")
    check_date = register_entry.get("Check date")
    if not check_date and isinstance(check_time, str):
        check_date = check_time[:10]

    certificate_url = register_entry.get("Certificate Link") or certificate.get("url")
    if certificate_url:
        certificate_url = ensure_http_url(certificate_url, "Certificate Link")

    return {
        "schema_version": "1.0",
        "certificate_id": certificate_id,
        "certificate_url": certificate_url,
        "title": paper.get("title") or register_entry.get("Title") or full_entry.get("Title"),
        "paper": {
            "reference_url": paper.get("reference") or register_entry.get("Paper reference"),
            "openalex_url": paper.get("openalex") or register_entry.get("OpenAlex"),
            "authors": authors,
            "abstract": {
                "text": abstract_text,
                "source": abstract_source,
                "format": "upstream_unmodified",
            }
            if abstract_text is not None
            else None,
        },
        "check": {
            "date": check_date,
            "timestamp": check_time,
            "type": check.get("type") or register_entry.get("Type"),
            "venue": check.get("venue") or register_entry.get("Venue"),
            "codecheckers": codecheckers,
            "summary": check.get("summary") if "summary" in check else full_entry.get("Summary"),
            # register-full.json documents this as free-form provenance.  It can
            # contain prose and more than one URL, so preserve it verbatim and
            # never treat it as a crawl target.
            "source_note": full_entry.get("Source"),
            "manifest": manifest,
        },
        "repository": {
            "descriptor": repository.original,
            "provider": repository.provider,
            "locator": repository.locator,
            "subpath": repository.subpath,
            "landing_url": register_entry.get("Repository Link"),
            "license": None,
            "revision": None,
        },
        "report": {
            "url": check.get("report") or register_entry.get("Report"),
            "registered_artifact_url": register_entry.get("Certificate PDF"),
        },
        "artifacts": [],
        "methods": [
            {
                "stage": "discover",
                "label": "codecheck_register_json",
                "request_url": REGISTER_URL,
                "request_ref": "../raw/register.json",
            },
            {
                "stage": "enrich",
                "label": "codecheck_certificate_json",
                "request_url": urljoin(certificate_url or "", "index.json") if certificate_url else None,
                "request_ref": "certificate_response.json",
            },
        ],
        "errors": [],
    }


def registered_certificate_artifact(record: Mapping[str, Any]) -> dict[str, Any] | None:
    url = record.get("report", {}).get("registered_artifact_url")
    if not url:
        return None
    url = ensure_http_url(url, "Certificate PDF")
    certificate_id = str(record["certificate_id"])
    filename = filename_from_url(url) or f"certificate-{sanitize_segment(certificate_id)}.bin"
    return {
        "artifact_id": stable_artifact_id("certificate", url),
        "role": "registered_certificate_artifact",
        "provider": urlparse(url).hostname,
        "discovery_method": "codecheck_registered_artifact_link",
        "source_url": url,
        "final_url": None,
        "filename": filename,
        "relative_path": None,
        "media_type": None,
        "size_bytes": None,
        "sha256": None,
        "source_checksum": None,
        "etag": None,
        "last_modified": None,
        "status": "discovered",
        "local_path": None,
    }


class ErrorLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def write(
        self,
        stage: str,
        message: str,
        *,
        certificate_id: str | None = None,
        url: str | None = None,
        exception: BaseException | None = None,
    ) -> dict[str, Any]:
        item = {
            "time": utc_now(),
            "certificate_id": certificate_id,
            "stage": stage,
            "url": safe_final_url(url)[0] if isinstance(url, str) else url,
            "message": redact_sensitive_urls(message),
            "exception_type": type(exception).__name__ if exception else None,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        retries: int,
        delay: float,
        user_agent: str,
    ) -> None:
        self.timeout = timeout
        self.delay = delay
        self.last_request_started = 0.0
        retry = Retry(
            total=retries,
            connect=retries,
            read=retries,
            status=retries,
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def close(self) -> None:
        self.session.close()

    def _throttle(self) -> None:
        remaining = self.delay - (time.monotonic() - self.last_request_started)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request_started = time.monotonic()

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        ensure_http_url(url)
        self._throttle()
        kwargs.setdefault("timeout", self.timeout)
        return self.session.get(url, **kwargs)

    def fetch_json(
        self,
        url: str,
        cache_path: Path,
        *,
        prefer_cache: bool,
        headers: Mapping[str, str] | None = None,
        fallback_to_cache: bool = True,
        cache_only: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        if prefer_cache and cache_path.exists():
            raw = cache_path.read_bytes()
            return json.loads(raw), {
                "url": url,
                "retrieved_at": None,
                "sha256": sha256_bytes(raw),
                "cache": "hit",
            }
        if cache_only:
            raise ScraperError(f"Offline cache is missing: {cache_path}")
        try:
            response = self.get(url, headers=dict(headers or {}))
            response.raise_for_status()
            raw = response.content
            parsed = json.loads(raw)
            atomic_write_bytes(cache_path, raw)
            return parsed, {
                "url": url,
                "retrieved_at": utc_now(),
                "sha256": sha256_bytes(raw),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "cache": "miss",
            }
        except (requests.RequestException, json.JSONDecodeError) as exc:
            if fallback_to_cache and cache_path.exists():
                raw = cache_path.read_bytes()
                return json.loads(raw), {
                    "url": url,
                    "retrieved_at": None,
                    "sha256": sha256_bytes(raw),
                    "cache": "stale_fallback",
                    "network_error": str(exc),
                }
            raise


def _related_href(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        href = value.get("href")
        return href if isinstance(href, str) else None
    return None


class ProviderResolver:
    def __init__(
        self,
        http: HttpClient,
        *,
        refresh: bool,
        github_token: str | None,
    ) -> None:
        self.http = http
        self.refresh = refresh
        self.github_token = github_token

    def resolve(
        self,
        repository: MutableMapping[str, Any],
        response_dir: Path,
    ) -> list[dict[str, Any]]:
        provider = repository.get("provider")
        if provider not in SUPPORTED_PROVIDERS:
            if provider:
                raise ScraperError(f"Unsupported repository provider: {provider}")
            return []
        response_dir.mkdir(parents=True, exist_ok=True)
        method = getattr(self, f"_{provider}")
        return method(repository, response_dir)

    def resolve_researchequals_certificate(
        self,
        artifact: MutableMapping[str, Any],
        response_dir: Path,
        certificate_id: str,
    ) -> bool:
        """Resolve ResearchEquals' retired v1 certificate route through v2."""
        source_url = str(artifact.get("source_url") or "")
        parsed = urlparse(source_url)
        if parsed.hostname not in {"researchequals.com", "www.researchequals.com"}:
            return False
        match = re.fullmatch(
            r"/api/modules/main/([0-9a-fA-F-]{36})/?",
            parsed.path,
        )
        if not match:
            return False
        version_id = match.group(1)
        resolution_url = f"https://researchequals.com/api/versions/{version_id}"
        response_dir.mkdir(parents=True, exist_ok=True)
        metadata = self._fetch(
            resolution_url,
            response_dir,
            "researchequals-version",
        )
        if metadata.get("published") is not True:
            raise ScraperError("ResearchEquals version is not confirmed published")
        content_key = metadata.get("content_s3")
        if not isinstance(content_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._-]+", content_key
        ):
            raise ScraperError("ResearchEquals returned an invalid content key")
        media_type = normalize_media_type(metadata.get("content_mediatype"))
        suffix = mimetypes.guess_extension(media_type) if media_type else None
        artifact.update(
            {
                "registered_source_url": source_url,
                "download_url": (
                    f"https://researchequals.com/api/files/{quote(content_key, safe='')}"
                ),
                "resolution_method": "researchequals_v2_version_api",
                "resolution_url": resolution_url,
                "provider_revision": metadata.get("id") or version_id,
                "provider_license_id": metadata.get("license_id"),
                "provider_metadata": {
                    key: metadata.get(key)
                    for key in (
                        "id",
                        "output_id",
                        "version",
                        "version_label",
                        "published",
                        "published_at",
                        "updated_at",
                        "title",
                        "pids",
                        "license_id",
                        "refs",
                        "content_s3",
                        "content_mediatype",
                    )
                },
                "media_type": media_type,
                "filename": f"certificate-{sanitize_segment(certificate_id)}{suffix or '.bin'}",
            }
        )
        return True

    def _fetch(
        self,
        url: str,
        response_dir: Path,
        label: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        data, _ = self.http.fetch_json(
            url,
            response_dir / f"{sanitize_segment(label)}-{key}.json",
            prefer_cache=not self.refresh,
            headers=headers,
        )
        return data

    def _github_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.github_token:
            headers["Authorization"] = f"Bearer {self.github_token}"
        return headers

    def _github(
        self,
        repository: MutableMapping[str, Any],
        response_dir: Path,
    ) -> list[dict[str, Any]]:
        locator = str(repository.get("locator") or "").strip("/")
        parts = locator.split("/")
        if len(parts) != 2 or not all(parts):
            raise ScraperError(f"Invalid GitHub locator: {locator!r}")
        owner, name = parts
        base = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        headers = self._github_headers()
        metadata = self._fetch(base, response_dir, "repository", headers=headers)
        visibility = metadata.get("visibility")
        if metadata.get("private") is not False or (
            visibility is not None and visibility != "public"
        ):
            raise ScraperError("GitHub repository is not confirmed public")
        default_branch = metadata.get("default_branch")
        if not default_branch:
            raise ScraperError("GitHub repository has no default branch")
        commit_url = f"{base}/commits/{quote(str(default_branch), safe='')}"
        commit = self._fetch(commit_url, response_dir, "commit", headers=headers)
        revision = commit.get("sha")
        if not revision:
            raise ScraperError("GitHub API returned no commit SHA")
        license_data = metadata.get("license") if isinstance(metadata.get("license"), Mapping) else {}
        repository["license"] = license_data.get("spdx_id") or license_data.get("name")
        repository["revision"] = revision
        repository["default_branch"] = default_branch
        archive_url = f"{base}/zipball/{quote(str(revision), safe='')}"
        filename = f"{sanitize_segment(owner)}-{sanitize_segment(name)}-{str(revision)[:12]}.zip"
        artifact = self._repository_archive(
            provider="github",
            method="github_repository_api_archive",
            url=archive_url,
            filename=filename,
            revision=str(revision),
            subpath=repository.get("subpath"),
            request_headers=headers,
        )
        return [artifact]

    def _gitlab(
        self,
        repository: MutableMapping[str, Any],
        response_dir: Path,
    ) -> list[dict[str, Any]]:
        locator = str(repository.get("locator") or "").strip("/")
        if "/" not in locator:
            raise ScraperError(f"Invalid GitLab locator: {locator!r}")
        encoded = quote(locator, safe="")
        base = f"https://gitlab.com/api/v4/projects/{encoded}"
        metadata = self._fetch(base, response_dir, "project")
        if metadata.get("visibility") != "public":
            raise ScraperError("GitLab repository is not confirmed public")
        default_branch = metadata.get("default_branch")
        if not default_branch:
            raise ScraperError("GitLab repository has no default branch")
        commit_url = f"{base}/repository/commits/{quote(str(default_branch), safe='')}"
        commit = self._fetch(commit_url, response_dir, "commit")
        revision = commit.get("id")
        if not revision:
            raise ScraperError("GitLab API returned no commit SHA")
        repository["revision"] = revision
        repository["default_branch"] = default_branch
        query: dict[str, str] = {"sha": str(revision)}
        if repository.get("subpath"):
            query["path"] = str(repository["subpath"])
        archive_url = f"{base}/repository/archive.zip?{urlencode(query)}"
        filename = f"{sanitize_segment(locator.replace('/', '-'))}-{str(revision)[:12]}.zip"
        return [
            self._repository_archive(
                provider="gitlab",
                method="gitlab_repository_api_archive",
                url=archive_url,
                filename=filename,
                revision=str(revision),
                subpath=repository.get("subpath"),
            )
        ]

    def _zenodo(
        self,
        repository: MutableMapping[str, Any],
        response_dir: Path,
    ) -> list[dict[str, Any]]:
        locator = str(repository.get("locator") or "").strip()
        if not re.fullmatch(r"[0-9]+", locator):
            raise ScraperError(f"Invalid Zenodo record ID: {locator!r}")
        url = f"https://zenodo.org/api/records/{locator}"
        metadata = self._fetch(url, response_dir, "record")
        record_metadata = (
            metadata.get("metadata")
            if isinstance(metadata.get("metadata"), Mapping)
            else {}
        )
        access = metadata.get("access") if isinstance(metadata.get("access"), Mapping) else {}
        access_values = {
            str(value).lower()
            for key in ("status", "record", "files")
            if (value := access.get(key)) is not None
        }
        access_right = record_metadata.get("access_right")
        if (
            str(metadata.get("status", "")).lower() == "restricted"
            or access_values.intersection({"restricted", "private", "embargoed"})
            or (
                access_right is not None
                and str(access_right).lower() not in {"open", "public"}
            )
        ):
            raise ScraperError("Zenodo record is restricted")
        repository["revision"] = str(metadata.get("id") or locator)
        license_data = record_metadata.get("license")
        if isinstance(license_data, Mapping):
            repository["license"] = license_data.get("id") or license_data.get("title")
        elif isinstance(license_data, str):
            repository["license"] = license_data
        artifacts: list[dict[str, Any]] = []
        for item in metadata.get("files", []) if isinstance(metadata.get("files"), list) else []:
            if not isinstance(item, Mapping):
                continue
            links = item.get("links") if isinstance(item.get("links"), Mapping) else {}
            download_url = links.get("content") or links.get("download") or links.get("self")
            if not download_url:
                continue
            download_url = ensure_http_url(download_url, "Zenodo file link")
            key = str(item.get("key") or item.get("filename") or item.get("id") or "file")
            relative = safe_relative_path([key])
            artifact_id = stable_artifact_id("repository_file", download_url, key)
            artifacts.append(
                {
                    "artifact_id": artifact_id,
                    "role": "repository_file",
                    "provider": "zenodo",
                    "discovery_method": "zenodo_records_api",
                    "source_url": download_url,
                    "final_url": None,
                    "filename": relative.name,
                    "relative_path": str(relative),
                    "media_type": item.get("mimetype"),
                    "size_bytes": item.get("size"),
                    "sha256": None,
                    "source_checksum": item.get("checksum"),
                    "etag": None,
                    "last_modified": None,
                    "status": "discovered",
                    "local_path": None,
                }
            )
        return artifacts

    def _osf(
        self,
        repository: MutableMapping[str, Any],
        response_dir: Path,
    ) -> list[dict[str, Any]]:
        root_id = str(repository.get("locator") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9]+", root_id):
            raise ScraperError(f"Invalid OSF node ID: {root_id!r}")
        node_url = f"https://api.osf.io/v2/nodes/{quote(root_id, safe='')}/"
        root = self._fetch(node_url, response_dir, "root-node")
        root_data = root.get("data") if isinstance(root, Mapping) else None
        if not isinstance(root_data, Mapping):
            raise ScraperError("OSF API returned no root node")
        attributes = root_data.get("attributes") if isinstance(root_data.get("attributes"), Mapping) else {}
        if attributes.get("public") is not True:
            raise ScraperError("OSF repository is not confirmed public")
        repository["revision"] = attributes.get("date_modified")
        node_license = attributes.get("node_license")
        if isinstance(node_license, Mapping):
            repository["license_details"] = dict(node_license)
        root_relationships = (
            root_data.get("relationships")
            if isinstance(root_data.get("relationships"), Mapping)
            else {}
        )
        license_relation = (
            root_relationships.get("license")
            if isinstance(root_relationships.get("license"), Mapping)
            else {}
        )
        license_links = (
            license_relation.get("links")
            if isinstance(license_relation.get("links"), Mapping)
            else {}
        )
        license_url = _related_href(license_links.get("related"))
        if license_url:
            license_payload = self._fetch(license_url, response_dir, "license")
            license_record = (
                license_payload.get("data")
                if isinstance(license_payload, Mapping)
                and isinstance(license_payload.get("data"), Mapping)
                else {}
            )
            license_attributes = (
                license_record.get("attributes")
                if isinstance(license_record.get("attributes"), Mapping)
                else {}
            )
            license_relation_data = (
                license_relation.get("data")
                if isinstance(license_relation.get("data"), Mapping)
                else {}
            )
            repository["license"] = (
                license_attributes.get("name")
                or license_record.get("id")
                or license_relation_data.get("id")
            )

        nodes: deque[tuple[str, tuple[str, ...]]] = deque([(root_id, tuple())])
        visited_nodes: set[str] = set()
        artifacts: list[dict[str, Any]] = []
        while nodes:
            node_id, component_path = nodes.popleft()
            if node_id in visited_nodes:
                continue
            visited_nodes.add(node_id)
            if len(visited_nodes) > 2000:
                raise ScraperError("OSF component limit exceeded")

            children_url = (
                f"https://api.osf.io/v2/nodes/{quote(node_id, safe='')}/children/"
                "?page[size]=100"
            )
            for child in self._paginate(children_url, response_dir, f"children-{node_id}"):
                if not isinstance(child, Mapping):
                    continue
                child_id = child.get("id")
                child_attrs = child.get("attributes") if isinstance(child.get("attributes"), Mapping) else {}
                if child_id and child_attrs.get("public") is True:
                    child_title = sanitize_segment(str(child_attrs.get("title") or child_id))
                    nodes.append((str(child_id), component_path + (f"{child_title}-{child_id}",)))

            storage_url = f"https://api.osf.io/v2/nodes/{quote(node_id, safe='')}/files/"
            for storage in self._paginate(storage_url, response_dir, f"storages-{node_id}"):
                if not isinstance(storage, Mapping):
                    continue
                storage_attrs = storage.get("attributes") if isinstance(storage.get("attributes"), Mapping) else {}
                provider_name = str(storage_attrs.get("provider") or storage_attrs.get("name") or "storage")
                relationships = storage.get("relationships") if isinstance(storage.get("relationships"), Mapping) else {}
                files_relation = relationships.get("files") if isinstance(relationships.get("files"), Mapping) else {}
                relation_links = files_relation.get("links") if isinstance(files_relation.get("links"), Mapping) else {}
                listing_url = _related_href(relation_links.get("related"))
                if not listing_url:
                    continue
                prefix = component_path + (sanitize_segment(provider_name),)
                artifacts.extend(
                    self._osf_file_tree(
                        listing_url,
                        response_dir,
                        node_id=node_id,
                        prefix=prefix,
                    )
                )
        return deduplicate_artifacts(artifacts)

    def _paginate(
        self,
        url: str,
        response_dir: Path,
        label: str,
    ) -> Iterator[Any]:
        seen: set[str] = set()
        page = 0
        next_url: str | None = ensure_http_url(url)
        while next_url:
            if next_url in seen:
                raise ScraperError(f"Pagination loop at {next_url}")
            seen.add(next_url)
            page += 1
            if page > 10000:
                raise ScraperError("Pagination limit exceeded")
            payload = self._fetch(next_url, response_dir, f"{label}-page-{page:04d}")
            data = payload.get("data", []) if isinstance(payload, Mapping) else []
            if isinstance(data, list):
                yield from data
            links = payload.get("links") if isinstance(payload, Mapping) else None
            candidate = links.get("next") if isinstance(links, Mapping) else None
            next_url = _related_href(candidate)

    def _osf_file_tree(
        self,
        root_url: str,
        response_dir: Path,
        *,
        node_id: str,
        prefix: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        queue: deque[tuple[str, tuple[str, ...]]] = deque([(root_url, prefix)])
        visited: set[str] = set()
        artifacts: list[dict[str, Any]] = []
        while queue:
            listing_url, path_parts = queue.popleft()
            if listing_url in visited:
                continue
            visited.add(listing_url)
            if len(visited) > 10000:
                raise ScraperError("OSF folder limit exceeded")
            for item in self._paginate(listing_url, response_dir, f"files-{node_id}"):
                if not isinstance(item, Mapping):
                    continue
                attrs = item.get("attributes") if isinstance(item.get("attributes"), Mapping) else {}
                name = str(attrs.get("name") or item.get("id") or "unnamed")
                kind = attrs.get("kind")
                if kind == "folder":
                    relationships = item.get("relationships") if isinstance(item.get("relationships"), Mapping) else {}
                    files_relation = relationships.get("files") if isinstance(relationships.get("files"), Mapping) else {}
                    rel_links = files_relation.get("links") if isinstance(files_relation.get("links"), Mapping) else {}
                    child_url = _related_href(rel_links.get("related"))
                    if child_url:
                        queue.append((child_url, path_parts + (sanitize_segment(name),)))
                    continue
                if kind != "file":
                    continue
                links = item.get("links") if isinstance(item.get("links"), Mapping) else {}
                download_url = links.get("download")
                if not download_url:
                    continue
                download_url = ensure_http_url(download_url, "OSF file download link")
                relative = safe_relative_path(path_parts + (name,))
                extra = attrs.get("extra") if isinstance(attrs.get("extra"), Mapping) else {}
                hashes = extra.get("hashes") if isinstance(extra.get("hashes"), Mapping) else {}
                source_checksum = None
                if hashes.get("sha256"):
                    source_checksum = f"sha256:{hashes['sha256']}"
                elif hashes.get("md5"):
                    source_checksum = f"md5:{hashes['md5']}"
                artifact_id = stable_artifact_id(
                    "repository_file",
                    download_url,
                    str(item.get("id") or relative),
                )
                artifacts.append(
                    {
                        "artifact_id": artifact_id,
                        "role": "repository_file",
                        "provider": "osf",
                        "discovery_method": "osf_files_api_recursive",
                        "source_url": download_url,
                        "final_url": None,
                        "filename": relative.name,
                        "relative_path": str(relative),
                        "media_type": None,
                        "size_bytes": attrs.get("size"),
                        "sha256": None,
                        "source_checksum": source_checksum,
                        "etag": None,
                        "last_modified": None,
                        "source_modified_at": attrs.get("date_modified") or attrs.get("modified"),
                        "status": "discovered",
                        "local_path": None,
                    }
                )
        return artifacts

    @staticmethod
    def _repository_archive(
        *,
        provider: str,
        method: str,
        url: str,
        filename: str,
        revision: str,
        subpath: Any,
        request_headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        return {
            "artifact_id": stable_artifact_id("repository_archive", url, revision),
            "role": "repository_archive",
            "provider": provider,
            "discovery_method": method,
            "source_url": url,
            "final_url": None,
            "filename": filename,
            "relative_path": filename,
            "media_type": "application/zip",
            "size_bytes": None,
            "sha256": None,
            "source_checksum": None,
            "etag": None,
            "last_modified": None,
            "status": "discovered",
            "local_path": None,
            "revision": revision,
            "requested_subpath": subpath,
            "request_headers": dict(request_headers or {}),
        }


def deduplicate_artifacts(artifacts: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in artifacts:
        key = str(artifact.get("artifact_id") or artifact.get("source_url"))
        if key not in seen:
            seen.add(key)
            result.append(artifact)
    return result


def validate_preserved_catalog_record(value: Any) -> str:
    """Validate the normalized shape before reusing an older catalog record."""
    if not isinstance(value, Mapping):
        raise ScraperError("Existing catalog contains a non-object record")
    certificate_id = value.get("certificate_id")
    if not isinstance(certificate_id, str) or not CERTIFICATE_ID_RE.fullmatch(
        certificate_id
    ):
        raise ScraperError("Existing catalog contains an invalid Certificate ID")
    if value.get("schema_version") != "1.0":
        raise ScraperError(
            f"Existing catalog record {certificate_id} has an incompatible schema"
        )
    for field in ("paper", "check", "repository", "report"):
        if not isinstance(value.get(field), Mapping):
            raise ScraperError(
                f"Existing catalog record {certificate_id} has invalid {field!r} data"
            )
    for field in ("artifacts", "methods", "errors"):
        if not isinstance(value.get(field), list):
            raise ScraperError(
                f"Existing catalog record {certificate_id} has invalid {field!r} data"
            )
    artifact_ids: set[str] = set()
    for artifact in value["artifacts"]:
        if (
            not isinstance(artifact, Mapping)
            or not isinstance(artifact.get("artifact_id"), str)
            or not artifact.get("artifact_id")
            or not isinstance(artifact.get("role"), str)
            or not isinstance(artifact.get("source_url"), str)
        ):
            raise ScraperError(
                f"Existing catalog record {certificate_id} has a malformed artifact"
            )
        artifact_id = str(artifact["artifact_id"])
        if artifact_id in artifact_ids:
            raise ScraperError(
                f"Existing catalog record {certificate_id} has duplicate artifacts"
            )
        artifact_ids.add(artifact_id)
    if not all(isinstance(item, Mapping) for item in value["methods"]):
        raise ScraperError(
            f"Existing catalog record {certificate_id} has malformed methods"
        )
    if not all(isinstance(item, Mapping) for item in value["errors"]):
        raise ScraperError(
            f"Existing catalog record {certificate_id} has malformed errors"
        )
    return certificate_id


def merge_previous_artifact_state(
    artifacts: Sequence[dict[str, Any]],
    previous_record: Mapping[str, Any] | None,
) -> None:
    if not isinstance(previous_record, Mapping):
        return
    previous_artifacts = previous_record.get("artifacts")
    if not isinstance(previous_artifacts, list):
        return
    by_id = {
        str(item.get("artifact_id")): item
        for item in previous_artifacts
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    persistent_fields = {
        "download_url",
        "registered_source_url",
        "resolution_method",
        "resolution_url",
        "provider_revision",
        "provider_license_id",
        "provider_metadata",
        "final_url",
        "final_url_query_redacted",
        "filename",
        "media_type",
        "size_bytes",
        "sha256",
        "etag",
        "last_modified",
        "status",
        "local_path",
        "downloaded_at",
    }
    for artifact in artifacts:
        old = by_id.get(str(artifact.get("artifact_id")))
        if old:
            if old.get("source_changed_since_previous") is True:
                # A prior refresh observed a changed source but did not finish
                # replacing it.  Keep the guard pending across retries.
                artifact["source_changed_since_previous"] = True
                continue
            source_changed = False
            for field in ("source_checksum", "source_modified_at", "size_bytes"):
                fresh_value = artifact.get(field)
                old_value = old.get(field)
                if (
                    fresh_value is not None
                    and old_value is not None
                    and fresh_value != old_value
                ):
                    source_changed = True
                    break
                if (
                    field == "source_modified_at"
                    and fresh_value is not None
                    and old_value is None
                    and old.get("local_path")
                ):
                    source_changed = True
                    break
            if source_changed:
                artifact["source_changed_since_previous"] = True
                continue
            for field in persistent_fields:
                if field == "status" and old.get(field) is not None:
                    artifact[field] = old[field]
                elif field == "filename" and old.get("local_path") and old.get(field):
                    artifact[field] = old[field]
                elif artifact.get(field) is None and old.get(field) is not None:
                    artifact[field] = copy.deepcopy(old[field])


def carry_forward_repository_artifacts(
    record: MutableMapping[str, Any],
    previous_record: Mapping[str, Any] | None,
) -> None:
    """Retain prior package inventory when this run did not re-resolve it."""
    if not isinstance(previous_record, Mapping):
        return
    previous_repository = previous_record.get("repository")
    current_repository = record.get("repository")
    if not isinstance(previous_repository, Mapping) or not isinstance(
        current_repository, Mapping
    ):
        return
    if previous_repository.get("descriptor") != current_repository.get("descriptor"):
        return
    carried_metadata = False
    for field in ("license", "license_details", "revision", "default_branch"):
        if current_repository.get(field) is None and previous_repository.get(field) is not None:
            current_repository[field] = copy.deepcopy(previous_repository[field])
            carried_metadata = True
    if carried_metadata:
        current_repository["metadata_freshness"] = (
            "carried_forward_not_resolved_this_run"
        )
    previous_artifacts = previous_record.get("artifacts")
    current_artifacts = record.get("artifacts")
    if not isinstance(previous_artifacts, list) or not isinstance(current_artifacts, list):
        return
    seen = {
        str(item.get("artifact_id"))
        for item in current_artifacts
        if isinstance(item, Mapping) and item.get("artifact_id")
    }
    for old in previous_artifacts:
        if not isinstance(old, Mapping) or old.get("role") not in {
            "repository_file",
            "repository_archive",
        }:
            continue
        artifact_id = str(old.get("artifact_id") or "")
        if not artifact_id or artifact_id in seen:
            continue
        carried = copy.deepcopy(dict(old))
        carried["metadata_freshness"] = "carried_forward_not_resolved_this_run"
        current_artifacts.append(carried)
        seen.add(artifact_id)

    previous_methods = previous_record.get("methods")
    current_methods = record.get("methods")
    if isinstance(previous_methods, list) and isinstance(current_methods, list):
        existing_method_keys = {
            (item.get("stage"), item.get("label"))
            for item in current_methods
            if isinstance(item, Mapping)
        }
        for old_method in previous_methods:
            if not isinstance(old_method, Mapping) or old_method.get("stage") != "repository":
                continue
            key = (old_method.get("stage"), old_method.get("label"))
            if key in existing_method_keys:
                continue
            carried_method = copy.deepcopy(dict(old_method))
            carried_method["status"] = "carried_forward"
            carried_method["metadata_freshness"] = (
                "carried_forward_not_resolved_this_run"
            )
            current_methods.append(carried_method)
            existing_method_keys.add(key)


class Downloader:
    def __init__(
        self,
        http: HttpClient,
        *,
        output_root: Path,
        max_file_bytes: int | None,
        min_free_bytes: int,
        resume: bool,
    ) -> None:
        self.http = http
        self.output_root = output_root.resolve()
        self.max_file_bytes = max_file_bytes
        self.min_free_bytes = min_free_bytes
        self.resume = resume

    def _existing_file(
        self,
        artifact: Mapping[str, Any],
        _artifact_dir: Path,
    ) -> Path | None:
        if artifact.get("source_changed_since_previous") is True:
            return None
        local = artifact.get("local_path")
        if isinstance(local, str):
            candidate = (self.output_root / local).resolve()
            if self.output_root in candidate.parents and candidate.is_file():
                return candidate
        return None

    def _validate_existing(self, artifact: MutableMapping[str, Any], path: Path) -> bool:
        actual_size = path.stat().st_size
        expected_size = artifact.get("size_bytes")
        if isinstance(expected_size, int) and expected_size != actual_size:
            return False
        actual_hash = sha256_file(path)
        expected_hash = artifact.get("sha256")
        if expected_hash and expected_hash != actual_hash:
            return False
        source_checksum = artifact.get("source_checksum")
        if isinstance(source_checksum, str) and ":" in source_checksum:
            algorithm, expected = source_checksum.split(":", 1)
            algorithm = algorithm.lower()
            if algorithm in {"sha256", "md5"}:
                actual = actual_hash if algorithm == "sha256" else hash_file(path, "md5")
                if actual.lower() != expected.lower():
                    return False
        artifact.update(
            {
                "size_bytes": actual_size,
                "sha256": actual_hash,
                "status": "skipped_existing",
                "local_path": str(path.relative_to(self.output_root)),
            }
        )
        return True

    @staticmethod
    def _source_fingerprint(artifact: Mapping[str, Any]) -> dict[str, Any]:
        """Fields that identify the immutable provider object behind a URL."""
        return {
            field: artifact.get(field)
            for field in (
                "artifact_id",
                "source_url",
                "source_checksum",
                "source_modified_at",
                "revision",
                "provider_revision",
            )
        }

    @staticmethod
    def _resume_validator(state: Mapping[str, Any]) -> tuple[str, str] | None:
        etag = state.get("etag")
        if (
            isinstance(etag, str)
            and etag.strip()
            and not etag.strip().lower().startswith("w/")
        ):
            return "etag", etag.strip()
        last_modified = state.get("last_modified")
        if isinstance(last_modified, str) and last_modified.strip():
            return "last_modified", last_modified.strip()
        return None

    @classmethod
    def _resume_response_matches(
        cls,
        state: Mapping[str, Any],
        response: requests.Response,
    ) -> bool:
        validator = cls._resume_validator(state)
        if validator is None:
            return False
        kind, expected = validator
        header = "ETag" if kind == "etag" else "Last-Modified"
        actual = response.headers.get(header)
        return isinstance(actual, str) and actual.strip() == expected

    def download(self, artifact: MutableMapping[str, Any], artifact_dir: Path) -> None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        existing = self._existing_file(artifact, artifact_dir)
        if existing and self._validate_existing(artifact, existing):
            (artifact_dir / ".download.part").unlink(missing_ok=True)
            (artifact_dir / ".download.part.json").unlink(missing_ok=True)
            return
        declared_size = artifact.get("size_bytes")
        if (
            self.max_file_bytes is not None
            and isinstance(declared_size, int)
            and declared_size > self.max_file_bytes
        ):
            artifact["status"] = "skipped_size_limit"
            raise ArtifactSkipped(
                f"Provider reports {declared_size} bytes; limit is {self.max_file_bytes} bytes"
            )

        download_url = ensure_http_url(
            artifact.get("download_url") or artifact.get("source_url"),
            "artifact download URL",
        )
        part_path = artifact_dir / ".download.part"
        part_state_path = artifact_dir / ".download.part.json"
        if not part_path.exists():
            part_state_path.unlink(missing_ok=True)
        if artifact.get("source_changed_since_previous") is True or not self.resume:
            part_path.unlink(missing_ok=True)
            part_state_path.unlink(missing_ok=True)

        offset = 0
        resume_state: Mapping[str, Any] = {}
        if self.resume and part_path.exists() and part_path.stat().st_size > 0:
            try:
                loaded_state = load_json(part_state_path)
            except (OSError, json.JSONDecodeError):
                loaded_state = None
            valid_state = (
                isinstance(loaded_state, Mapping)
                and loaded_state.get("download_url") == download_url
                and loaded_state.get("source_fingerprint")
                == self._source_fingerprint(artifact)
                and self._resume_validator(loaded_state) is not None
            )
            if valid_state:
                resume_state = loaded_state
                offset = part_path.stat().st_size
                state_total = loaded_state.get("expected_total")
                if isinstance(state_total, int) and offset > state_total:
                    valid_state = False
            if not valid_state:
                part_path.unlink(missing_ok=True)
                part_state_path.unlink(missing_ok=True)
                resume_state = {}
                offset = 0
        elif part_path.exists():
            part_path.unlink(missing_ok=True)
            part_state_path.unlink(missing_ok=True)

        if self.max_file_bytes is not None and offset > self.max_file_bytes:
            artifact["status"] = "skipped_size_limit"
            raise ArtifactSkipped(
                f"Partial file is already {offset} bytes; limit is {self.max_file_bytes} bytes"
            )
        headers = dict(artifact.get("request_headers") or {})
        # Byte ranges must refer to the same representation that is written to
        # disk.  Requests transparently decodes compressed transfer bodies.
        headers["Accept-Encoding"] = "identity"
        if offset:
            headers["Range"] = f"bytes={offset}-"
            validator = self._resume_validator(resume_state)
            if validator is not None:
                headers["If-Range"] = validator[1]

        response = self.http.get(
            download_url,
            headers=headers,
            stream=True,
            allow_redirects=True,
        )
        if response.status_code == 416 and offset:
            # A same-length object is not necessarily the same object.  Never
            # promote a partial file using a 416 error response as evidence.
            response.close()
            part_path.unlink(missing_ok=True)
            part_state_path.unlink(missing_ok=True)
            offset = 0
            resume_state = {}
            headers.pop("Range", None)
            headers.pop("If-Range", None)
            response = self.http.get(
                download_url,
                headers=headers,
                stream=True,
                allow_redirects=True,
            )
        response.raise_for_status()

        append = False
        range_end: int | None = None
        range_total: int | None = None
        if offset and response.status_code == 206:
            range_match = re.fullmatch(
                r"bytes\s+([0-9]+)-([0-9]+)/([0-9]+)",
                response.headers.get("Content-Range", "").strip(),
                flags=re.IGNORECASE,
            )
            if range_match:
                range_start, range_end, range_total = map(int, range_match.groups())
                range_size = range_end - range_start + 1
                response_length = content_length_value(response.headers.get("Content-Length"))
                append = (
                    range_start == offset
                    and range_end >= range_start
                    and range_total > range_end
                    and (response_length is None or response_length == range_size)
                    and normalize_media_type(response.headers.get("Content-Encoding"))
                    in {None, "identity"}
                    and self._resume_response_matches(resume_state, response)
                    and (
                        not isinstance(resume_state.get("expected_total"), int)
                        or resume_state.get("expected_total") == range_total
                    )
                )
        if offset and not append:
            response.close()
            part_path.unlink(missing_ok=True)
            part_state_path.unlink(missing_ok=True)
            offset = 0
            resume_state = {}
            headers.pop("Range", None)
            headers.pop("If-Range", None)
            response = self.http.get(
                download_url,
                headers=headers,
                stream=True,
                allow_redirects=True,
            )
            response.raise_for_status()

        fresh_range_total: int | None = None
        if not append and response.status_code == 206:
            full_range_match = re.fullmatch(
                r"bytes\s+0-([0-9]+)/([0-9]+)",
                response.headers.get("Content-Range", "").strip(),
                flags=re.IGNORECASE,
            )
            response_length = content_length_value(response.headers.get("Content-Length"))
            if full_range_match:
                full_range_end, fresh_range_total = map(int, full_range_match.groups())
            if (
                not full_range_match
                or full_range_end + 1 != fresh_range_total
                or (response_length is not None and response_length != fresh_range_total)
            ):
                response.close()
                raise ScraperError(
                    "Server returned a partial 206 response to a fresh download"
                )

        content_encoding = normalize_media_type(response.headers.get("Content-Encoding"))
        content_length = (
            content_length_value(response.headers.get("Content-Length"))
            if content_encoding in {None, "identity"}
            else None
        )
        expected_total = (
            range_total
            if append
            else fresh_range_total
            if fresh_range_total is not None
            else content_length
        )
        if self.max_file_bytes is not None and expected_total is not None:
            if expected_total > self.max_file_bytes:
                response.close()
                artifact["status"] = "skipped_size_limit"
                raise ArtifactSkipped(
                    f"File is {expected_total} bytes; limit is {self.max_file_bytes} bytes"
                )

        atomic_write_json(
            part_state_path,
            {
                "schema_version": "1.0",
                "download_url": download_url,
                "source_fingerprint": self._source_fingerprint(artifact),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "expected_total": expected_total,
                "content_encoding": content_encoding,
                "updated_at": utc_now(),
            },
        )

        mode = "ab" if append else "wb"
        total = offset if append else 0
        first = True
        try:
            with part_path.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if first:
                        first = False
                        filename_hint = str(artifact.get("filename") or "").lower()
                        legitimate_html_file = (
                            artifact.get("role") == "repository_file"
                            and filename_hint.endswith((".html", ".htm", ".xhtml"))
                        )
                        if is_html_response(response, chunk) and not legitimate_html_file:
                            raise ScraperError("Download returned HTML instead of a file")
                    total += len(chunk)
                    if self.max_file_bytes is not None and total > self.max_file_bytes:
                        artifact["status"] = "skipped_size_limit"
                        raise ArtifactSkipped(
                            f"File exceeded the {self.max_file_bytes}-byte limit while streaming"
                        )
                    free = shutil.disk_usage(self.output_root).free
                    if free - len(chunk) < self.min_free_bytes:
                        artifact["status"] = "skipped_disk_guard"
                        raise ArtifactSkipped(
                            f"Free-space guard would fall below {self.min_free_bytes} bytes"
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            response.close()
            raise

        if not append and expected_total is not None and total != expected_total:
            response.close()
            raise ScraperError(
                f"Download body ended at {total} bytes; response headers promised "
                f"{expected_total} bytes"
            )
        if append and range_end is not None and total != range_end + 1:
            with part_path.open("r+b") as handle:
                handle.truncate(offset)
            response.close()
            raise ScraperError(
                f"Range body ended at {total - 1}; Content-Range ended at {range_end}"
            )
        if append and range_total is not None and total != range_total:
            response.close()
            raise ScraperError(
                f"Server returned an incomplete range ending at {total - 1} of "
                f"{range_total} bytes; rerun with --resume"
            )

        filename = choose_download_filename(
            response,
            str(artifact.get("filename") or "download.bin"),
        )
        if filename.casefold() in RESERVED_DOWNLOAD_NAMES:
            filename = sanitize_segment(f"upstream-{filename.lstrip('.')}")
        target = artifact_dir / filename
        actual_sha256 = self._validated_download_sha256(artifact, part_path)
        response.close()
        os.replace(part_path, target)
        self._finish(artifact, target, response, actual_sha256)
        part_state_path.unlink(missing_ok=True)

    @staticmethod
    def _validated_download_sha256(
        artifact: Mapping[str, Any],
        path: Path,
    ) -> str:
        actual_sha256 = sha256_file(path)
        source_checksum = artifact.get("source_checksum")
        if isinstance(source_checksum, str) and ":" in source_checksum:
            algorithm, expected = source_checksum.split(":", 1)
            algorithm = algorithm.lower()
            if algorithm in {"sha256", "md5"}:
                actual = (
                    actual_sha256
                    if algorithm == "sha256"
                    else hash_file(path, "md5")
                )
                if actual.lower() != expected.lower():
                    raise ScraperError(
                        f"Downloaded file checksum mismatch: expected {source_checksum}, "
                        f"got {algorithm}:{actual}"
                    )
        return actual_sha256

    def _finish(
        self,
        artifact: MutableMapping[str, Any],
        target: Path,
        response: requests.Response,
        actual_sha256: str,
    ) -> None:
        final_url, query_redacted = safe_final_url(response.url)
        artifact.update(
            {
                "final_url": final_url,
                "final_url_query_redacted": query_redacted,
                "filename": target.name,
                "media_type": normalize_media_type(response.headers.get("Content-Type")),
                "size_bytes": target.stat().st_size,
                "sha256": actual_sha256,
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
                "status": "downloaded",
                "local_path": str(target.relative_to(self.output_root)),
                "downloaded_at": utc_now(),
            }
        )
        artifact.pop("source_changed_since_previous", None)
        artifact.pop("request_headers", None)


def build_summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    providers: Counter[str] = Counter()
    types: Counter[str] = Counter()
    venues: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    known_bytes = 0
    unknown_size = 0
    repository_artifacts = 0
    certificate_artifacts = 0
    error_count = 0
    for record in records:
        repository = record.get("repository") if isinstance(record.get("repository"), Mapping) else {}
        check = record.get("check") if isinstance(record.get("check"), Mapping) else {}
        if repository.get("provider"):
            providers[str(repository["provider"])] += 1
        if check.get("type"):
            types[str(check["type"])] += 1
        if check.get("venue"):
            venues[str(check["venue"])] += 1
        errors = record.get("errors") if isinstance(record.get("errors"), list) else []
        error_count += len(errors)
        for artifact in record.get("artifacts", []) if isinstance(record.get("artifacts"), list) else []:
            if not isinstance(artifact, Mapping):
                continue
            role = artifact.get("role")
            if role == "registered_certificate_artifact":
                certificate_artifacts += 1
            elif role in {"repository_file", "repository_archive"}:
                repository_artifacts += 1
            status_counts[str(artifact.get("status") or "unknown")] += 1
            size = artifact.get("size_bytes")
            if isinstance(size, int):
                known_bytes += size
            else:
                unknown_size += 1
    return {
        "records": len(records),
        "records_with_paper_links": sum(
            1 for record in records if record.get("paper", {}).get("reference_url")
        ),
        "records_with_repositories": sum(
            1 for record in records if record.get("repository", {}).get("locator")
        ),
        "registered_certificate_artifacts": certificate_artifacts,
        "repository_artifacts": repository_artifacts,
        "artifact_statuses": dict(sorted(status_counts.items())),
        "known_artifact_bytes": known_bytes,
        "unknown_size_artifacts": unknown_size,
        "repository_providers": dict(sorted(providers.items())),
        "check_types": dict(sorted(types.items())),
        "venues": dict(sorted(venues.items())),
        "errors": error_count,
    }


class Harvester:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.output_root = args.output.resolve()
        self.data_dir = self.output_root / "data"
        self.raw_dir = self.data_dir / "raw"
        self.state_dir = self.output_root / "state"
        self.log_dir = self.output_root / "logs"
        for path in (self.raw_dir, self.state_dir, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)
        self.errors = ErrorLog(self.log_dir / "errors.jsonl")
        self.global_errors: list[dict[str, Any]] = []
        self.http = HttpClient(
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            user_agent=args.user_agent,
        )
        self.resolver = ProviderResolver(
            self.http,
            refresh=args.refresh,
            github_token=os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_PAT"),
        )
        self.downloader = Downloader(
            self.http,
            output_root=self.output_root,
            max_file_bytes=args.max_file_bytes,
            min_free_bytes=args.min_free_bytes,
            resume=args.resume,
        )
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.checkpoint = self._load_checkpoint()

    def _load_checkpoint(self) -> dict[str, Any]:
        if self.checkpoint_path.exists():
            try:
                value = load_json(self.checkpoint_path)
                if isinstance(value, dict):
                    return value
            except (OSError, json.JSONDecodeError):
                pass
        return {"schema_version": "1.0", "records": {}}

    def _save_checkpoint(self, certificate_id: str, status: str) -> None:
        records = self.checkpoint.setdefault("records", {})
        records[certificate_id] = {"status": status, "updated_at": utc_now()}
        self.checkpoint["last_certificate_id"] = certificate_id
        self.checkpoint["updated_at"] = utc_now()
        atomic_write_json(self.checkpoint_path, self.checkpoint)

    def _fetch_optional_global(self, url: str, name: str) -> tuple[Any, dict[str, Any]]:
        try:
            return self.http.fetch_json(
                url,
                self.raw_dir / name,
                prefer_cache=not self.args.refresh and self.args.offline,
                fallback_to_cache=True,
                cache_only=self.args.offline,
            )
        except Exception as exc:
            error = self.errors.write("global_metadata", str(exc), url=url, exception=exc)
            self.global_errors.append(error)
            return None, {"url": url, "error": str(exc)}

    def run(self) -> dict[str, Any]:
        if self.args.offline and self.args.action != "inventory":
            raise ScraperError("--offline can only be used for inventory generation")
        register, register_source = self.http.fetch_json(
            REGISTER_URL,
            self.raw_dir / "register.json",
            prefer_cache=self.args.offline,
            fallback_to_cache=True,
            cache_only=self.args.offline,
        )
        if not isinstance(register, list):
            raise ScraperError("CODECHECK register JSON is not a list")
        full_register, full_source = self._fetch_optional_global(
            REGISTER_FULL_URL,
            "register-full.json",
        )
        meta, meta_source = self._fetch_optional_global(REGISTER_META_URL, ".meta.json")
        register_source["method_label"] = "codecheck_register_json"
        full_source["method_label"] = "codecheck_register_full_json"
        meta_source["method_label"] = "codecheck_register_meta_json"
        full_by_id = {}
        if isinstance(full_register, list):
            full_by_id = {
                str(item.get("Certificate ID")): item
                for item in full_register
                if isinstance(item, Mapping) and item.get("Certificate ID")
            }

        selected: list[Mapping[str, Any]] = []
        seen_ids: set[str] = set()
        requested_ids = set(self.args.certificate_ids or [])
        for item in register:
            if not isinstance(item, Mapping):
                continue
            certificate_id = item.get("Certificate ID")
            if not isinstance(certificate_id, str):
                raise ScraperError("Register contains a row without a Certificate ID")
            if certificate_id in seen_ids:
                raise ScraperError(f"Duplicate Certificate ID: {certificate_id}")
            seen_ids.add(certificate_id)
            if requested_ids and certificate_id not in requested_ids:
                continue
            selected.append(item)
        missing = requested_ids - seen_ids
        if missing:
            raise ScraperError(f"Certificate IDs not found: {', '.join(sorted(missing))}")
        if self.args.max_records is not None:
            selected = selected[: self.args.max_records]

        records: list[dict[str, Any]] = []
        total = len(selected)
        for index, entry in enumerate(selected, start=1):
            certificate_id = str(entry["Certificate ID"])
            print(f"[{index}/{total}] {certificate_id}", flush=True)
            record = self._process_record(entry, full_by_id.get(certificate_id))
            records.append(record)

        processed_ids = {record["certificate_id"] for record in records}
        current_run_error_count = len(self.global_errors) + sum(
            len(record.get("errors", []))
            for record in records
            if isinstance(record.get("errors"), list)
        )
        partial_run = bool(requested_ids) or len(selected) < len(register)
        preserved_records = 0
        preserved_ids: list[str] = []
        previous_catalog_generated_at = None
        previous_register_sha256 = None
        existing_catalog_path = self.output_root / "catalog.json"
        if partial_run and existing_catalog_path.exists():
            try:
                existing_catalog = load_json(existing_catalog_path)
            except (OSError, json.JSONDecodeError) as exc:
                raise ScraperError(
                    "Refusing to overwrite an unreadable catalog during a partial run"
                ) from exc
            if (
                not isinstance(existing_catalog, Mapping)
                or existing_catalog.get("schema_version") != "1.0"
                or not isinstance(existing_catalog.get("software"), Mapping)
                or existing_catalog["software"].get("name")
                != "codecheck-register-scraper"
                or not isinstance(existing_catalog.get("records"), list)
            ):
                raise ScraperError(
                    "Refusing to merge a partial run into an incompatible catalog"
                )
            old_records = existing_catalog["records"]
            old_by_id: dict[str, Mapping[str, Any]] = {}
            for item in old_records:
                try:
                    item_id = validate_preserved_catalog_record(item)
                except ScraperError as exc:
                    raise ScraperError(
                        "Refusing to merge a partial run into a malformed catalog: "
                        f"{exc}"
                    ) from exc
                if item_id in old_by_id:
                    raise ScraperError(
                        f"Existing catalog contains duplicate Certificate ID: {item_id}"
                    )
                old_by_id[item_id] = item
            previous_catalog_generated_at = existing_catalog.get("generated_at")
            old_source = existing_catalog.get("source")
            if isinstance(old_source, Mapping):
                old_register_source = old_source.get("register")
                if isinstance(old_register_source, Mapping):
                    previous_register_sha256 = old_register_source.get("sha256")
            new_by_id = {record["certificate_id"]: record for record in records}
            merged_records: list[dict[str, Any]] = []
            for register_entry in register:
                if not isinstance(register_entry, Mapping):
                    continue
                item_id = str(register_entry.get("Certificate ID") or "")
                if item_id in new_by_id:
                    merged_records.append(new_by_id[item_id])
                elif item_id in old_by_id:
                    merged_records.append(copy.deepcopy(dict(old_by_id[item_id])))
                    preserved_ids.append(item_id)
            preserved_records = len(preserved_ids)
            records = merged_records

        represented_ids = {record["certificate_id"] for record in records}
        unrepresented_ids = [
            str(item["Certificate ID"])
            for item in register
            if isinstance(item, Mapping)
            and item.get("Certificate ID") not in represented_ids
        ]

        summary = build_summary(records)
        summary["errors"] += len(self.global_errors)
        catalog = {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "software": {
                "name": "codecheck-register-scraper",
                "version": VERSION,
            },
            "source": {
                "register": register_source,
                "register_full": full_source,
                "register_build": meta,
                "register_build_source": meta_source,
                "database_license": "ODC-By-1.0",
            },
            "summary": summary,
            "global_errors": self.global_errors,
            "run_scope": {
                "partial": partial_run,
                "processed_certificate_ids": sorted(processed_ids),
                "processed_records": len(processed_ids),
                "current_run_errors": current_run_error_count,
                "preserved_prior_records": preserved_records,
                "preserved_certificate_ids": preserved_ids,
                "unrepresented_certificate_ids": unrepresented_ids,
                "preserved_record_provenance": {
                    "catalog_generated_at": previous_catalog_generated_at,
                    "register_sha256": previous_register_sha256,
                }
                if preserved_ids
                else None,
            },
            "records": records,
        }
        atomic_write_json(self.output_root / "catalog.json", catalog)
        self.checkpoint["catalog_written_at"] = utc_now()
        self.checkpoint["register_sha256"] = register_source.get("sha256")
        atomic_write_json(self.checkpoint_path, self.checkpoint)
        return catalog

    def _process_record(
        self,
        entry: Mapping[str, Any],
        full_entry: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        certificate_id = str(entry["Certificate ID"])
        folder_name = sanitize_segment(certificate_id)
        record_dir = self.data_dir / folder_name
        record_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(record_dir / "register_entry.json", entry)
        previous_record = None
        if (record_dir / "record.json").exists():
            try:
                previous_record = load_json(record_dir / "record.json")
            except (OSError, json.JSONDecodeError):
                previous_record = None

        detail: Mapping[str, Any] | None = None
        detail_error = None
        certificate_url = entry.get("Certificate Link")
        detail_url = urljoin(str(certificate_url), "index.json") if certificate_url else None
        if detail_url:
            try:
                value, _ = self.http.fetch_json(
                    detail_url,
                    record_dir / "certificate_response.json",
                    prefer_cache=not self.args.refresh,
                    fallback_to_cache=True,
                    cache_only=self.args.offline,
                )
                detail = value if isinstance(value, Mapping) else None
            except Exception as exc:
                detail_error = self.errors.write(
                    "certificate_metadata",
                    str(exc),
                    certificate_id=certificate_id,
                    url=detail_url,
                    exception=exc,
                )
                if self.args.fail_fast:
                    raise

        try:
            record = normalize_record(entry, detail, full_entry)
        except Exception as exc:
            self._save_checkpoint(certificate_id, "failed")
            if self.args.fail_fast:
                raise
            error = self.errors.write(
                "normalize",
                str(exc),
                certificate_id=certificate_id,
                exception=exc,
            )
            record = {
                "schema_version": "1.0",
                "certificate_id": certificate_id,
                "title": entry.get("Title"),
                "paper": {},
                "check": {},
                "repository": {},
                "report": {},
                "artifacts": [],
                "methods": [],
                "errors": [error],
            }
            atomic_write_json(record_dir / "record.json", record)
            return record
        if detail_error:
            record["errors"].append(detail_error)

        try:
            certificate_artifact = registered_certificate_artifact(record)
            if certificate_artifact:
                record["artifacts"].append(certificate_artifact)
        except Exception as exc:
            error = self.errors.write(
                "certificate_artifact",
                str(exc),
                certificate_id=certificate_id,
                url=record.get("report", {}).get("registered_artifact_url"),
                exception=exc,
            )
            record["errors"].append(error)
            if self.args.fail_fast:
                raise

        repository_resolved_this_run = False
        if self.args.action in {"repository_inventory", "repositories", "all"}:
            provider = record["repository"].get("provider")
            if self.args.providers and provider not in self.args.providers:
                record["methods"].append(
                    {
                        "stage": "repository",
                        "label": "provider_filter",
                        "status": "skipped",
                        "provider": provider,
                    }
                )
            else:
                try:
                    repository_artifacts = self.resolver.resolve(
                        record["repository"],
                        record_dir / "provider_responses",
                    )
                    record["artifacts"].extend(repository_artifacts)
                    repository_resolved_this_run = True
                    if provider:
                        record["methods"].append(
                            {
                                "stage": "repository",
                                "label": f"{provider}_provider_api",
                                "status": "completed",
                                "request_ref": "provider_responses/",
                            }
                        )
                except Exception as exc:
                    error = self.errors.write(
                        "repository_inventory",
                        str(exc),
                        certificate_id=certificate_id,
                        url=record["repository"].get("landing_url"),
                        exception=exc,
                    )
                    record["errors"].append(error)
                    if self.args.fail_fast:
                        raise

        if not repository_resolved_this_run:
            carry_forward_repository_artifacts(record, previous_record)
        record["artifacts"] = deduplicate_artifacts(record["artifacts"])
        merge_previous_artifact_state(record["artifacts"], previous_record)

        for artifact in record["artifacts"]:
            is_repository_artifact = artifact.get("role") in {
                "repository_file",
                "repository_archive",
            }
            provider_allowed = (
                not self.args.providers
                or artifact.get("provider") in self.args.providers
            )
            should_download = (
                (
                    self.args.action == "all"
                    and (
                        not is_repository_artifact
                        or (repository_resolved_this_run and provider_allowed)
                    )
                )
                or (
                    self.args.action == "certificates"
                    and artifact.get("role") == "registered_certificate_artifact"
                )
                or (
                    self.args.action == "repositories"
                    and is_repository_artifact
                    and repository_resolved_this_run
                    and provider_allowed
                )
            )
            if not should_download:
                continue
            if artifact.get("role") == "registered_certificate_artifact":
                try:
                    resolved = self.resolver.resolve_researchequals_certificate(
                        artifact,
                        record_dir / "provider_responses",
                        certificate_id,
                    )
                    if resolved:
                        record["methods"].append(
                            {
                                "stage": "certificate_artifact",
                                "label": "researchequals_v2_version_api",
                                "status": "completed",
                                "request_ref": "provider_responses/",
                            }
                        )
                except Exception as exc:
                    error = self.errors.write(
                        "certificate_artifact_resolution",
                        str(exc),
                        certificate_id=certificate_id,
                        url=artifact.get("source_url"),
                        exception=exc,
                    )
                    record["errors"].append(error)
                    if self.args.fail_fast:
                        raise
                    artifact["resolution_error"] = error["message"]
                    # Recognized retired v1 routes are never retried directly:
                    # that would contradict the explicit public-v2 migration.
                    continue
            role_folder = (
                "certificate"
                if artifact.get("role") == "registered_certificate_artifact"
                else "repository"
            )
            artifact_dir = (
                record_dir
                / "files"
                / role_folder
                / sanitize_segment(str(artifact["artifact_id"]))
            )
            try:
                self.downloader.download(artifact, artifact_dir)
            except ArtifactSkipped as exc:
                error = self.errors.write(
                    "download_skipped",
                    str(exc),
                    certificate_id=certificate_id,
                    url=artifact.get("download_url") or artifact.get("source_url"),
                    exception=exc,
                )
                artifact["error"] = error["message"]
            except Exception as exc:
                artifact["status"] = "error"
                error = self.errors.write(
                    "download",
                    str(exc),
                    certificate_id=certificate_id,
                    url=artifact.get("download_url") or artifact.get("source_url"),
                    exception=exc,
                )
                artifact["error"] = error["message"]
                record["errors"].append(error)
                if self.args.fail_fast:
                    raise

        for artifact in record["artifacts"]:
            artifact.pop("request_headers", None)
        atomic_write_json(record_dir / "record.json", record)
        has_errors = bool(record["errors"]) or any(
            artifact.get("status") == "error" for artifact in record["artifacts"]
        )
        self._save_checkpoint(
            certificate_id,
            "completed_with_errors" if has_errors else "completed",
        )
        return record

    def close(self) -> None:
        self.http.close()


def nonnegative_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite non-negative number")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory CODECHECK certificates and optionally download their exact "
            "registered artifacts and public repository packages."
        )
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--inventory-only",
        dest="action",
        action="store_const",
        const="inventory",
        help="Collect official CODECHECK register metadata only (default).",
    )
    actions.add_argument(
        "--inventory-repositories",
        dest="action",
        action="store_const",
        const="repository_inventory",
        help=(
            "Resolve public provider package listings and revisions without "
            "downloading their files."
        ),
    )
    actions.add_argument(
        "--download-certificates",
        dest="action",
        action="store_const",
        const="certificates",
        help="Download only exact Certificate PDF/artifact links from the register.",
    )
    actions.add_argument(
        "--download-repositories",
        dest="action",
        action="store_const",
        const="repositories",
        help="Download public repository packages using official provider APIs.",
    )
    actions.add_argument(
        "--download-files",
        dest="action",
        action="store_const",
        const="all",
        help="Download registered certificate artifacts and public repository packages.",
    )
    parser.set_defaults(action="inventory")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Inventory three records in a separate smoke-output directory.",
    )
    parser.add_argument(
        "--output",
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Output directory (default: current directory).",
    )
    parser.add_argument(
        "--certificate-id",
        "--only-id",
        dest="certificate_ids",
        action="append",
        help="Process one certificate ID; repeat to select several.",
    )
    parser.add_argument("--max-records", type=positive_int, help="Stop after N records.")
    parser.add_argument(
        "--provider",
        dest="providers",
        action="append",
        choices=sorted(SUPPORTED_PROVIDERS),
        help="Restrict repository package retrieval to a provider; repeat as needed.",
    )
    parser.add_argument(
        "--delay",
        type=nonnegative_float,
        default=0.25,
        help="Minimum delay between HTTP requests in seconds (default: 0.25).",
    )
    parser.add_argument(
        "--timeout",
        type=nonnegative_float,
        default=60.0,
        help="Per-request timeout in seconds (default: 60).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Retries for rate limits and transient HTTP errors (default: 4).",
    )
    parser.add_argument(
        "--max-file-mb",
        type=nonnegative_float,
        help="Skip any individual file larger than this many MiB.",
    )
    parser.add_argument(
        "--min-free-gb",
        type=nonnegative_float,
        default=2.0,
        help="Keep at least this many GiB free (default: 2).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refetch cached record and provider metadata.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Resume validated .part downloads with HTTP Range and If-Range; "
            "otherwise restart safely."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Rebuild inventory from cached JSON without network requests.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first record/provider/download error.",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="HTTP User-Agent string.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.offline and args.refresh:
        parser.error("--offline cannot be combined with --refresh")
    if args.certificate_ids:
        invalid = [item for item in args.certificate_ids if not CERTIFICATE_ID_RE.fullmatch(item)]
        if invalid:
            parser.error(f"invalid certificate ID(s): {', '.join(invalid)}")
    if args.smoke_test:
        if args.action != "inventory":
            parser.error("--smoke-test must be used with --inventory-only")
        args.output = Path("smoke-output")
        args.max_records = min(args.max_records or 3, 3)
    args.max_file_bytes = (
        int(args.max_file_mb * 1024 * 1024) if args.max_file_mb is not None else None
    )
    args.min_free_bytes = int(args.min_free_gb * 1024 * 1024 * 1024)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(args, parser)
    harvester: Harvester | None = None
    try:
        harvester = Harvester(args)
        catalog = harvester.run()
        summary = catalog["summary"]
        print(
            f"Wrote {summary['records']} records to {args.output.resolve() / 'catalog.json'} "
            f"({summary['errors']} errors).",
            flush=True,
        )
        return 0 if summary["errors"] == 0 else 2
    except KeyboardInterrupt:
        print("Interrupted; cached metadata and .part files were preserved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"fatal: {redact_sensitive_urls(exc)}", file=sys.stderr)
        return 1
    finally:
        if harvester:
            harvester.close()


if __name__ == "__main__":
    raise SystemExit(main())
