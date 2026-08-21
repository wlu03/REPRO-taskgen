#!/usr/bin/env python3
"""Scraper for the Harvard Dataverse PAN collection.

Two discovery sources are supported.

``--source api`` (default) uses the public Dataverse endpoints that the dataset
pages themselves advertise in their ``Link: rel="describedby"`` headers: the
Search endpoint for discovery and the Croissant metadata export for each
dataset. These return complete file lists with exact ``contentUrl`` download
links and checksums.

``--source html`` keeps the original behaviour: discovery and metadata come from
rendered HTML pages, parsing Schema.org/Croissant JSON-LD embedded in each
dataset page. Harvard now serves a file-less Croissant block in that HTML and
gates HTML routes behind a browser-verification challenge, so this path needs
``--cookie-file`` and cannot produce download URLs for most datasets.

Under both sources, file URLs are used exactly as the site supplies them. The
scraper never constructs a download URL from a filename or numeric identifier.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_module
import http.cookiejar
import json
import logging
import mimetypes
import os
import random
import re
import shutil
import sys
import tempfile
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup, Tag

VERSION = "1.0.0"
LOGGER = logging.getLogger("pan_html_scraper")
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Harvard answers a sustained burst with a plain nginx 403 that covers the whole
# API, not just the endpoint being called. 429 is the documented signal; 403 is
# the one actually observed, so both are treated as backpressure.
RATE_LIMIT_STATUS = {403, 429}
# Measured: ~3 requests/second was cut off partway through the collection and
# the block persisted for minutes. This ceiling stays well under that.
DEFAULT_REQUESTS_PER_MINUTE = 40
RATE_LIMIT_COOLDOWN_SECONDS = 60.0
MAX_ADAPTIVE_DELAY_SECONDS = 30.0
RATE_LIMIT_CIRCUIT_BREAK = 4
DELAY_DECAY_AFTER_SUCCESSES = 25
CHALLENGE_MARKERS = (
    "verify that you're not a robot",
    "verify you are human",
    "checking your browser",
    "enable javascript and then reload the page",
    "cf-chl-",
    "captcha",
)
SEARCH_PATH = "/api/search"
EXPORT_PATH = "/api/datasets/export"
CROISSANT_EXPORTER = "croissant"
SEARCH_PAGE_SIZE = 1000
# /api/access/datafile/<id> answers with a 303 to presigned object storage.
# Those hosts are the only off-site redirect targets a download may land on.
STORAGE_HOSTS = frozenset(
    {
        "dvn-cloud-iqss.s3.amazonaws.com",
        "dvn-cloud.s3.amazonaws.com",
    }
)
DATASET_PATH_RE = re.compile(r"/(?:dataset(?:\.xhtml)?|dataset/[^/?#]+)", re.IGNORECASE)
FILE_ID_RE = re.compile(r"/(?:api/)?access/datafile/(\d+)(?:$|[/?#])", re.IGNORECASE)
HUMAN_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)?\s*$", re.IGNORECASE)


class ScraperError(RuntimeError):
    """Base exception for expected scraper failures."""


class BotChallengeError(ScraperError):
    """Raised when the site returns a browser-verification page."""


class RateLimitError(ScraperError):
    """Raised when the repository keeps refusing requests as too frequent.

    This stops the crawl instead of continuing to send requests that the server
    has already told us to slow down. Cached work is kept, so a later run
    resumes rather than restarting.
    """


@dataclass(frozen=True)
class Settings:
    base_url: str
    collection: str
    source: str
    output_root: Path
    delay_seconds: float
    requests_per_minute: int
    timeout_seconds: float
    max_retries: int
    max_pages: int
    max_records: int | None
    refresh: bool
    download_files: bool
    resume: bool
    dry_run: bool
    max_file_bytes: int
    min_free_bytes: int
    include_extensions: frozenset[str]
    exclude_extensions: frozenset[str]
    allow_external_downloads: bool
    cookie_file: Path | None
    user_agent: str

    @property
    def data_dir(self) -> Path:
        return self.output_root / "data"

    @property
    def collection_pages_dir(self) -> Path:
        return self.data_dir / "_collection_pages"

    @property
    def state_dir(self) -> Path:
        return self.output_root / "state"

    @property
    def logs_dir(self) -> Path:
        return self.output_root / "logs"

    @property
    def search_pages_dir(self) -> Path:
        return self.data_dir / "_search_pages"

    @property
    def catalog_path(self) -> Path:
        return self.output_root / "catalog.json"

    @property
    def summary_path(self) -> Path:
        return self.output_root / "inventory_summary.json"

    @property
    def missing_papers_path(self) -> Path:
        return self.output_root / "missing_paper_links.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.state_dir / "checkpoint.json"

    @property
    def errors_path(self) -> Path:
        return self.logs_dir / "errors.jsonl"


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    from_cache: bool


@dataclass(frozen=True)
class CollectionPage:
    datasets: tuple[dict[str, Any], ...]
    subcollections: tuple[str, ...]
    max_page: int
    reported_total: int | None


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False)
    atomic_write_bytes(path, (payload + "\n").encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False))
        handle.write("\n")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def safe_segment(value: str, fallback: str = "unnamed", max_length: int = 160) -> str:
    cleaned = value.replace("\x00", "").strip()
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", cleaned)
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    return cleaned[:max_length] or fallback


def normalize_extension(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    return value if value.startswith(".") else f".{value}"


def parse_extension_set(values: Sequence[str] | None) -> frozenset[str]:
    result: set[str] = set()
    for raw in values or []:
        for item in raw.split(","):
            ext = normalize_extension(item)
            if ext:
                result.add(ext)
    return frozenset(result)


def normalize_url(url: str) -> str:
    split = urlsplit(url)
    return urlunsplit((split.scheme.lower(), split.netloc.lower(), split.path, split.query, ""))


def same_origin(left: str, right: str) -> bool:
    a, b = urlsplit(left), urlsplit(right)
    a_port = a.port or (443 if a.scheme == "https" else 80)
    b_port = b.port or (443 if b.scheme == "https" else 80)
    return (a.scheme.lower(), (a.hostname or "").lower(), a_port) == (
        b.scheme.lower(),
        (b.hostname or "").lower(),
        b_port,
    )


def redirect_target_allowed(final_url: str, base_url: str, allow_external: bool) -> bool:
    """Decide whether a download may finish on the host it was redirected to.

    Dataverse answers /api/access/datafile/<id> with a 303 to presigned object
    storage, so the bytes legitimately arrive from another host. Only the known
    Dataverse storage hosts are accepted; anything else needs the explicit
    --allow-external-downloads opt-in.
    """

    if allow_external or same_origin(final_url, base_url):
        return True
    host = (urlsplit(final_url).hostname or "").lower()
    return host in STORAGE_HOSTS


def is_bot_challenge(body: bytes | str) -> bool:
    text = body.decode("utf-8", "ignore") if isinstance(body, bytes) else body
    lowered = text.lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=UTC)
            return max(0.0, (target - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def record_key(persistent_id: str | None, landing_page: str) -> str:
    source = persistent_id or landing_page
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", source).strip("._-")
    readable = readable[:90] or "dataset"
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:10]
    return f"{readable}--{digest}"


def normalize_identifier(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("@id") or value.get("url") or value.get("name")
    if isinstance(value, list):
        for item in value:
            normalized = normalize_identifier(item)
            if normalized:
                return normalized
        return ""
    text = normalize_space(value)
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("https://doi.org/") or lowered.startswith("http://doi.org/"):
        return "doi:" + text.split("doi.org/", 1)[1]
    if lowered.startswith("doi.org/"):
        return "doi:" + text.split("/", 1)[1]
    if lowered.startswith("doi:"):
        return "doi:" + text[4:]
    return text


def doi_from_identifier(identifier: str) -> str:
    return identifier[4:] if identifier.lower().startswith("doi:") else ""


def persistent_id_from_url(url: str) -> str:
    query = parse_qs(urlsplit(url).query)
    for key in ("persistentId", "persistentid"):
        if query.get(key):
            return normalize_identifier(unquote(query[key][0]))
    return ""


def extract_file_id(url: str) -> str:
    match = FILE_ID_RE.search(urlsplit(url).path)
    if match:
        return match.group(1)
    query = parse_qs(urlsplit(url).query)
    for key in ("fileId", "fileid", "id"):
        if query.get(key) and str(query[key][0]).isdigit():
            return str(query[key][0])
    return ""


def parse_size(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, dict):
        for key in ("value", "contentSize", "size"):
            if key in value:
                parsed = parse_size(value[key])
                if parsed is not None:
                    return parsed
        return None
    text = normalize_space(value).replace(",", "")
    if text.isdigit():
        return int(text)
    match = HUMAN_SIZE_RE.match(text)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "b").lower()
    factors = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "pb": 1000**5,
        "eb": 1000**6,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
        "pib": 1024**5,
        "eib": 1024**6,
    }
    return int(number * factors.get(unit, 1))


def listify(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text_value(value: Any) -> str:
    if isinstance(value, str):
        return normalize_space(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        for item in value:
            result = text_value(item)
            if result:
                return result
        return ""
    if isinstance(value, dict):
        for key in ("name", "value", "@value", "url", "@id"):
            result = text_value(value.get(key))
            if result:
                return result
    return ""


def all_text_values(value: Any) -> list[str]:
    values: list[str] = []
    for item in listify(value):
        if isinstance(item, dict):
            candidate = text_value(item)
        else:
            candidate = normalize_space(item)
        if candidate and candidate not in values:
            values.append(candidate)
    return values


def json_type_names(value: Any) -> set[str]:
    names: set[str] = set()
    for item in listify(value):
        text = normalize_space(item).lower()
        if text:
            names.add(text.split(":")[-1])
    return names


def iter_json_nodes(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_json_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_json_nodes(child)


def parse_json_ld(soup: BeautifulSoup) -> list[Any]:
    documents: list[Any] = []
    for script in soup.find_all("script"):
        script_type = normalize_space(script.get("type")).lower()
        if script_type != "application/ld+json":
            continue
        raw = script.string if script.string is not None else script.get_text()
        raw = html_module.unescape(raw or "").strip().rstrip(";")
        if not raw:
            continue
        try:
            documents.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            LOGGER.debug("ignoring invalid JSON-LD block: %s", exc)
    return documents


def choose_dataset_node(documents: Sequence[Any]) -> dict[str, Any]:
    best: dict[str, Any] = {}
    best_score = -1
    for document in documents:
        for node in iter_json_nodes(document):
            types = json_type_names(node.get("@type"))
            score = 0
            if "dataset" in types:
                score += 20
            if "distribution" in node:
                score += 8
            if node.get("name") or node.get("headline"):
                score += 3
            if node.get("identifier") or node.get("@id"):
                score += 2
            if node.get("description"):
                score += 1
            if score > best_score:
                best = node
                best_score = score
    return best if best_score >= 20 else {}


def meta_values(soup: BeautifulSoup, *names: str) -> list[str]:
    wanted = {name.lower() for name in names}
    results: list[str] = []
    for meta in soup.find_all("meta"):
        key = normalize_space(meta.get("name") or meta.get("property")).lower()
        if key not in wanted:
            continue
        value = normalize_space(meta.get("content"))
        if value and value not in results:
            results.append(value)
    return results


def first_nonempty(*values: Any) -> str:
    for value in values:
        candidate = text_value(value)
        if candidate:
            return candidate
    return ""


def author_name(value: Any) -> str:
    if isinstance(value, str):
        return normalize_space(value)
    if not isinstance(value, dict):
        return ""
    if value.get("name"):
        return text_value(value["name"])
    given = text_value(value.get("givenName"))
    family = text_value(value.get("familyName"))
    return normalize_space(" ".join(part for part in (given, family) if part))


def extract_checksum(node: dict[str, Any]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    direct = {
        "md5": node.get("md5"),
        "sha1": node.get("sha1") or node.get("sha-1"),
        "sha256": node.get("sha256") or node.get("sha-256"),
        "sha512": node.get("sha512") or node.get("sha-512"),
    }
    for algorithm, value in direct.items():
        text = text_value(value).lower()
        if text:
            checksums[algorithm] = text
    for item in listify(node.get("checksum") or node.get("digest")):
        if isinstance(item, dict):
            algorithm = text_value(item.get("algorithm") or item.get("name") or item.get("type")).lower()
            value = text_value(item.get("value") or item.get("checksumValue") or item.get("digestValue")).lower()
            algorithm = re.sub(r"[^a-z0-9]", "", algorithm)
            if algorithm in {"md5", "sha1", "sha256", "sha512"} and value:
                checksums[algorithm] = value
    return checksums


def candidate_download_url(node: dict[str, Any], page_url: str) -> str:
    for key in ("contentUrl", "downloadUrl"):
        value = text_value(node.get(key))
        if value:
            return normalize_url(urljoin(page_url, value))
    return ""


def distribution_nodes(dataset_node: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    def add(value: Any) -> None:
        for item in listify(value):
            if isinstance(item, dict) and id(item) not in seen_ids:
                seen_ids.add(id(item))
                nodes.append(item)

    add(dataset_node.get("distribution"))
    # Croissant documents may place FileObject nodes in @graph and reference
    # them from distribution. Include any explicit downloadable object.
    for node in iter_json_nodes(dataset_node):
        types = json_type_names(node.get("@type"))
        if ("datadownload" in types or "fileobject" in types) and (
            node.get("contentUrl") or node.get("downloadUrl")
        ):
            add(node)
    return nodes


def filename_from_url(url: str, fallback: str) -> str:
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    return safe_segment(name, fallback)


def normalize_file_node(node: dict[str, Any], page_url: str, index: int) -> dict[str, Any] | None:
    download_url = candidate_download_url(node, page_url)
    if not download_url:
        return None
    file_id = extract_file_id(download_url)
    name = first_nonempty(node.get("name"), node.get("path"), node.get("filename"))
    if not name:
        name = filename_from_url(download_url, f"file_{file_id or index}")
    name = normalize_space(name)
    mime_type = first_nonempty(node.get("encodingFormat"), node.get("fileFormat"), node.get("mimeType"))
    if isinstance(node.get("encodingFormat"), dict):
        mime_type = first_nonempty(
            node["encodingFormat"].get("name"),
            node["encodingFormat"].get("@id"),
            node["encodingFormat"].get("url"),
        )
    size = parse_size(node.get("contentSize") or node.get("fileSize") or node.get("size"))
    return {
        "file_ref": file_id or hashlib.sha256(download_url.encode("utf-8")).hexdigest()[:12],
        "file_id": file_id,
        "name": name,
        "mime_type": mime_type,
        "size_bytes": size,
        "checksums": extract_checksum(node),
        "download_url": download_url,
        "landing_page": "",
        "source": "embedded_json_ld",
        "download_status": "not_requested",
        "local_path": "",
    }


def html_download_files(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for element in soup.find_all(["a", "link"]):
        href = normalize_space(element.get("href"))
        if not href:
            continue
        absolute = normalize_url(urljoin(page_url, href))
        split = urlsplit(absolute)
        query = parse_qs(split.query)
        if query.get("imageThumb") or query.get("imageThumb".lower()):
            continue
        if not FILE_ID_RE.search(split.path):
            continue
        file_id = extract_file_id(absolute)
        label = normalize_space(element.get_text(" ", strip=True))
        filename = filename_from_url(absolute, f"file_{file_id or len(results) + 1}")
        results.append(
            {
                "file_ref": file_id or hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:12],
                "file_id": file_id,
                "name": label or filename,
                "mime_type": "",
                "size_bytes": None,
                "checksums": {},
                "download_url": absolute,
                "landing_page": "",
                "source": "html_download_link",
                "download_status": "not_requested",
                "local_path": "",
            }
        )
    return results


def html_file_landing_pages(soup: BeautifulSoup, page_url: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    selectors = [".fileNameOriginal a[href]", "a[href*='file.xhtml']"]
    seen: set[str] = set()
    for selector in selectors:
        for anchor in soup.select(selector):
            href = normalize_space(anchor.get("href"))
            if not href:
                continue
            absolute = normalize_url(urljoin(page_url, href))
            if absolute in seen:
                continue
            seen.add(absolute)
            query = parse_qs(urlsplit(absolute).query)
            file_id = ""
            for key in ("fileId", "fileid"):
                if query.get(key):
                    file_id = str(query[key][0])
            results.append(
                {
                    "file_ref": file_id or hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:12],
                    "file_id": file_id,
                    "name": normalize_space(anchor.get_text(" ", strip=True)) or f"file_{file_id or len(results) + 1}",
                    "mime_type": "",
                    "size_bytes": None,
                    "checksums": {},
                    "download_url": "",
                    "landing_page": absolute,
                    "source": "html_file_row",
                    "download_status": "not_downloadable_from_page_data",
                    "local_path": "",
                }
            )
    return results


def deduplicate_files(files: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for file_item in files:
        key = file_item.get("download_url") or file_item.get("landing_page") or (
            f"id:{file_item.get('file_id')}" if file_item.get("file_id") else f"name:{file_item.get('name')}"
        )
        if key not in merged:
            merged[key] = file_item
            continue
        current = merged[key]
        for field in ("file_id", "name", "mime_type", "size_bytes", "download_url", "landing_page"):
            if not current.get(field) and file_item.get(field):
                current[field] = file_item[field]
        if file_item.get("checksums"):
            current.setdefault("checksums", {}).update(file_item["checksums"])
        if current.get("source") != "embedded_json_ld" and file_item.get("source") == "embedded_json_ld":
            current["source"] = "embedded_json_ld"
    return list(merged.values())


def external_url(value: Any, page_url: str) -> str:
    if isinstance(value, dict):
        for key in ("url", "@id", "sameAs"):
            result = external_url(value.get(key), page_url)
            if result:
                return result
        return ""
    for item in listify(value):
        # Dataverse states citation as a list of CreativeWork objects, so the
        # URL is one level below the list, not a bare string in it.
        if isinstance(item, dict):
            result = external_url(item, page_url)
            if result:
                return result
            continue
        text = normalize_space(item)
        if text.startswith(("http://", "https://")):
            absolute = normalize_url(urljoin(page_url, text))
            if not same_origin(absolute, page_url):
                return absolute
    return ""


def parse_dataset_html(body: bytes, page_url: str, discovery: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[Any]]:
    if is_bot_challenge(body):
        raise BotChallengeError(f"browser verification page returned for {page_url}")
    soup = BeautifulSoup(body, "html.parser")
    documents = parse_json_ld(soup)
    dataset = choose_dataset_node(documents)

    pid_candidates = meta_values(soup, "DC.identifier", "citation_doi")
    pid_candidates.extend(
        [
            persistent_id_from_url(page_url),
            normalize_identifier(dataset.get("identifier")),
            normalize_identifier(dataset.get("@id")),
        ]
    )
    persistent_id = next((normalize_identifier(value) for value in pid_candidates if normalize_identifier(value)), "")
    doi = doi_from_identifier(persistent_id)

    title = first_nonempty(
        dataset.get("name"),
        dataset.get("headline"),
        *(meta_values(soup, "DC.title", "citation_title", "og:title")),
        (discovery or {}).get("title"),
    )
    description = first_nonempty(
        dataset.get("description"),
        *(meta_values(soup, "DC.description", "description", "og:description")),
    )

    authors: list[str] = []
    for value in listify(dataset.get("creator")) + listify(dataset.get("author")):
        name = author_name(value)
        if name and name not in authors:
            authors.append(name)
    for name in meta_values(soup, "DC.creator", "citation_author", "article:author"):
        if name not in authors:
            authors.append(name)

    keywords = all_text_values(dataset.get("keywords"))
    for subject in meta_values(soup, "DC.subject", "citation_keywords"):
        for item in re.split(r"\s*[;,]\s*", subject):
            if item and item not in keywords:
                keywords.append(item)

    files: list[dict[str, Any]] = []
    for index, node in enumerate(distribution_nodes(dataset), start=1):
        normalized = normalize_file_node(node, page_url, index)
        if normalized:
            files.append(normalized)
    files.extend(html_download_files(soup, page_url))
    if not files:
        files.extend(html_file_landing_pages(soup, page_url))
    files = deduplicate_files(files)

    paper_url = ""
    for field in ("citation", "isBasedOn", "sameAs", "subjectOf"):
        paper_url = external_url(dataset.get(field), page_url)
        if paper_url:
            break

    license_value = dataset.get("license")
    license_text = text_value(license_value)
    publisher = text_value(dataset.get("publisher"))
    included_catalog = dataset.get("includedInDataCatalog")
    collection_name = text_value(included_catalog)

    key = record_key(persistent_id or None, page_url)
    return (
        {
            "record_key": key,
            "persistent_id": persistent_id,
            "doi": doi,
            "title": title,
            "landing_page": normalize_url(page_url),
            "description": description,
            "authors": authors,
            "keywords": keywords,
            "published_at": first_nonempty(dataset.get("datePublished"), *meta_values(soup, "DC.date", "article:published_time")),
            "modified_at": text_value(dataset.get("dateModified")),
            "version": text_value(dataset.get("version")),
            "license": license_text,
            "publisher": publisher,
            "collection": collection_name,
            "paper_url": paper_url,
            "files": files,
            "methods": {
                "discovery": {
                    "label": "html_collection_result_card",
                    "source_page": (discovery or {}).get("source_page", ""),
                },
                "record": {
                    "label": "html_dataset_page_embedded_json_ld",
                    "fallback": "ordinary_html_meta_and_links",
                },
                "download": {
                    "label": "exact_download_url_exposed_by_html",
                    "url_construction": False,
                },
            },
            "raw": {
                "dataset_html": "",
                "structured_data": "",
            },
        },
        documents,
    )


def dataset_link_from_anchor(anchor: Tag, page_url: str) -> str:
    href = normalize_space(anchor.get("href"))
    if not href:
        return ""
    absolute = normalize_url(urljoin(page_url, href))
    if not DATASET_PATH_RE.search(urlsplit(absolute).path):
        return ""
    return absolute


def alias_from_dataverse_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    marker = "/dataverse/"
    if marker not in path:
        return ""
    alias = unquote(path.split(marker, 1)[1]).split("/", 1)[0]
    return normalize_space(alias)


def parse_collection_html(body: bytes, page_url: str, current_alias: str) -> CollectionPage:
    if is_bot_challenge(body):
        raise BotChallengeError(f"browser verification page returned for {page_url}")
    soup = BeautifulSoup(body, "html.parser")
    datasets: OrderedDict[str, dict[str, Any]] = OrderedDict()

    cards = soup.select(".datasetResult")
    if cards:
        for card in cards:
            candidates = [a for a in card.find_all("a", href=True) if dataset_link_from_anchor(a, page_url)]
            if not candidates:
                continue
            anchor = candidates[0]
            url = dataset_link_from_anchor(anchor, page_url)
            pid = persistent_id_from_url(url)
            key = pid or url
            datasets.setdefault(
                key,
                {
                    "persistent_id": pid,
                    "landing_page": url,
                    "title": normalize_space(anchor.get_text(" ", strip=True)),
                    "source_page": normalize_url(page_url),
                },
            )
    else:
        for anchor in soup.find_all("a", href=True):
            url = dataset_link_from_anchor(anchor, page_url)
            if not url:
                continue
            pid = persistent_id_from_url(url)
            key = pid or url
            datasets.setdefault(
                key,
                {
                    "persistent_id": pid,
                    "landing_page": url,
                    "title": normalize_space(anchor.get_text(" ", strip=True)),
                    "source_page": normalize_url(page_url),
                },
            )

    subcollections: OrderedDict[str, None] = OrderedDict()
    # Only result cards represent child Dataverses. Scanning every page link
    # would accidentally treat breadcrumbs, navigation links, and the parent
    # collection as descendants and could escape the requested subtree.
    for card in soup.select(".dataverseResult"):
        for anchor in card.find_all("a", href=True):
            absolute = normalize_url(urljoin(page_url, normalize_space(anchor.get("href"))))
            alias = alias_from_dataverse_url(absolute)
            if alias and alias != current_alias:
                subcollections.setdefault(alias, None)
                break

    max_page = 1
    for anchor in soup.find_all("a", href=True):
        query = parse_qs(urlsplit(urljoin(page_url, anchor.get("href"))).query)
        for value in query.get("page", []):
            try:
                max_page = max(max_page, int(value.replace(",", "")))
            except (ValueError, AttributeError):
                pass

    reported_total: int | None = None
    count_node = soup.select_one(".results-count")
    if count_node:
        match = re.search(r"\bof\s+([0-9,]+)\b", normalize_space(count_node.get_text(" ", strip=True)), re.IGNORECASE)
        if match:
            reported_total = int(match.group(1).replace(",", ""))
    if reported_total and datasets:
        # Dataverse currently renders ten result cards per collection page.
        # This inference is only a fallback when paginator links are absent.
        visible_cards = len(cards) or len(datasets)
        if visible_cards > 0:
            inferred = (reported_total + visible_cards - 1) // visible_cards
            max_page = max(max_page, inferred)

    return CollectionPage(tuple(datasets.values()), tuple(subcollections.keys()), max_page, reported_total)


def build_collection_url(base_url: str, alias: str, page: int) -> str:
    path = f"/dataverse/{quote(alias, safe='')}"
    query = urlencode(
        {
            "q": "",
            "types": "dataverses:datasets",
            "sort": "dateSort",
            "order": "desc",
            "page": str(page),
        }
    )
    return f"{base_url.rstrip('/')}{path}?{query}"


class HtmlClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        if settings.cookie_file:
            jar = http.cookiejar.MozillaCookieJar(str(settings.cookie_file))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except (OSError, http.cookiejar.LoadError) as exc:
                raise ScraperError(f"could not load Netscape cookie file {settings.cookie_file}: {exc}") from exc
            self.session.cookies.update(jar)
        self._last_request = 0.0
        # Adaptive pacing: starts at the configured delay, grows when the server
        # pushes back, and decays back toward the base after sustained success.
        self._delay = max(settings.delay_seconds, 0.0)
        self._recent_requests: deque[float] = deque()
        self._consecutive_rate_limits = 0
        self._successes_since_backoff = 0

    def __enter__(self) -> "HtmlClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.session.close()

    def _throttle(self) -> None:
        """Hold back until both pacing rules allow another request.

        Two independent limits apply. The per-request delay spaces calls out,
        and a rolling one-minute budget caps bursts even when the delay is
        small, so a long crawl cannot creep above the configured rate.
        """

        while True:
            now = time.monotonic()
            waits = []

            elapsed = now - self._last_request
            if self._last_request and elapsed < self._delay:
                waits.append(self._delay - elapsed)

            budget = self.settings.requests_per_minute
            if budget > 0:
                while self._recent_requests and now - self._recent_requests[0] >= 60.0:
                    self._recent_requests.popleft()
                if len(self._recent_requests) >= budget:
                    waits.append(60.0 - (now - self._recent_requests[0]))

            wait = max(waits) if waits else 0.0
            if wait <= 0:
                break
            if wait > 1.0:
                LOGGER.debug("pacing: waiting %.1fs before the next request", wait)
            time.sleep(wait)

        moment = time.monotonic()
        self._last_request = moment
        self._recent_requests.append(moment)

    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        """Tell repository-wide backpressure apart from a per-file refusal.

        Dataverse answers a restricted file with a JSON error body; the
        rate-limit 403 is a plain nginx page. Only the latter should slow the
        whole crawl down.
        """

        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        content_type = response.headers.get("Content-Type", "").lower()
        if "json" in content_type:
            return False
        return True

    def _note_rate_limit(self, response: requests.Response) -> float:
        """Widen the pacing after backpressure and report how long to wait."""

        self._consecutive_rate_limits += 1
        self._successes_since_backoff = 0
        previous = self._delay
        self._delay = min(MAX_ADAPTIVE_DELAY_SECONDS, max(self._delay * 2.0, 1.0))
        retry_after = parse_retry_after(response.headers.get("Retry-After"))
        cooldown = RATE_LIMIT_COOLDOWN_SECONDS * self._consecutive_rate_limits
        wait = max(retry_after or 0.0, cooldown)
        LOGGER.warning(
            "HTTP %d looks like rate limiting; slowing from %.1fs to %.1fs between requests "
            "and pausing %.0fs (%d in a row)",
            response.status_code,
            previous,
            self._delay,
            wait,
            self._consecutive_rate_limits,
        )
        return wait

    def _note_success(self) -> None:
        """Ease the pacing back down once the server is answering normally."""

        self._consecutive_rate_limits = 0
        if self._delay <= self.settings.delay_seconds:
            return
        self._successes_since_backoff += 1
        if self._successes_since_backoff >= DELAY_DECAY_AFTER_SUCCESSES:
            self._successes_since_backoff = 0
            self._delay = max(self.settings.delay_seconds, self._delay * 0.75)
            LOGGER.info("pacing recovered; delay now %.1fs between requests", self._delay)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        stream: bool = False,
        acceptable: Iterable[int] = (200,),
    ) -> requests.Response:
        allowed = set(acceptable)
        last_error: Exception | None = None
        attempt = 0
        # Rate-limit pauses deliberately do not consume the retry budget: being
        # asked to slow down is not the same as a failing request.
        while attempt <= self.settings.max_retries:
            self._throttle()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    stream=stream,
                    timeout=self.settings.timeout_seconds,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self.settings.max_retries:
                    break
                delay = min(60.0, 1.0 * (2**attempt) + random.uniform(0, 0.25))
                LOGGER.warning("request failed; retrying in %.1fs: %s", delay, exc)
                attempt += 1
                time.sleep(delay)
                continue
            if response.status_code in RATE_LIMIT_STATUS and self._is_rate_limited(response):
                wait = self._note_rate_limit(response)
                response.close()
                if self._consecutive_rate_limits >= RATE_LIMIT_CIRCUIT_BREAK:
                    raise RateLimitError(
                        f"stopped after {self._consecutive_rate_limits} consecutive rate-limit responses "
                        f"from {urlsplit(url).netloc}. Everything fetched so far is cached, so a later run "
                        f"resumes from here. Retry in a few minutes, and consider a longer --delay or a "
                        f"lower --max-requests-per-minute (currently {self.settings.requests_per_minute})."
                    )
                time.sleep(wait)
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < self.settings.max_retries:
                delay = parse_retry_after(response.headers.get("Retry-After"))
                if delay is None:
                    delay = min(60.0, 1.0 * (2**attempt) + random.uniform(0, 0.25))
                LOGGER.warning("HTTP %d; retrying in %.1fs: %s", response.status_code, delay, response.url)
                response.close()
                attempt += 1
                time.sleep(delay)
                continue
            if response.status_code in allowed:
                self._note_success()
                return response

            body = ""
            try:
                body = response.text[:2000]
            except Exception:
                pass
            status = response.status_code
            final_url = str(response.url)
            response.close()
            raise ScraperError(f"unexpected HTTP {status} for {final_url}: {normalize_space(body)[:300]}")
        raise ScraperError(f"request failed after retries: {url}: {last_error}")

    def fetch_html(self, url: str, destination: Path, refresh: bool = False) -> FetchResult:
        sidecar = destination.with_suffix(destination.suffix + ".http.json")
        if destination.exists() and not refresh:
            body = destination.read_bytes()
            metadata = read_json(sidecar, {}) or {}
            result = FetchResult(
                requested_url=url,
                final_url=str(metadata.get("final_url") or url),
                status_code=int(metadata.get("status_code") or 200),
                headers=dict(metadata.get("headers") or {}),
                body=body,
                from_cache=True,
            )
            if is_bot_challenge(body):
                raise BotChallengeError(f"cached browser verification page at {destination}; rerun with --refresh")
            return result

        response = self.request("GET", url)
        try:
            body = bytes(response.content)
            content_type = response.headers.get("Content-Type", "")
            if is_bot_challenge(body):
                raise BotChallengeError(
                    "Harvard Dataverse returned a browser-verification page. "
                    "The scraper does not bypass anti-bot controls. Run from a normal browser-accessible network "
                    "or pass a Netscape-format browser cookie file with --cookie-file."
                )
            if "html" not in content_type.lower() and b"<html" not in body[:4096].lower():
                raise ScraperError(f"expected HTML from {response.url}, got {content_type or 'unknown content type'}")
            atomic_write_bytes(destination, body)
            selected_headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "content-length", "etag", "last-modified", "retry-after", "link"}
            }
            atomic_write_json(
                sidecar,
                {
                    "fetched_at": utc_now(),
                    "requested_url": url,
                    "final_url": str(response.url),
                    "status_code": response.status_code,
                    "headers": selected_headers,
                    "body_bytes": len(body),
                    "body_sha256": sha256_bytes(body),
                },
            )
            return FetchResult(url, str(response.url), response.status_code, selected_headers, body, False)
        finally:
            response.close()


    def fetch_json(self, url: str, destination: Path | None, refresh: bool = False) -> Any:
        """Fetch a JSON document, optionally caching it beside the record."""

        if destination is not None and destination.exists() and not refresh:
            cached = read_json(destination, None)
            if cached is not None:
                return cached

        response = self.request("GET", url)
        try:
            body = bytes(response.content)
            if is_bot_challenge(body):
                raise BotChallengeError(
                    "Harvard Dataverse returned a browser-verification page for an API request. "
                    "The scraper does not bypass anti-bot controls."
                )
            try:
                document = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ScraperError(f"expected JSON from {response.url}: {exc}") from exc
        finally:
            response.close()

        if destination is not None:
            atomic_write_json(destination, document)
        return document


def build_search_url(base_url: str, collection: str, start: int, per_page: int) -> str:
    query = urlencode(
        {
            "q": "*",
            "subtree": collection,
            "type": "dataset",
            "per_page": per_page,
            "start": start,
            "sort": "date",
            "order": "desc",
        }
    )
    return f"{base_url.rstrip('/')}{SEARCH_PATH}?{query}"


def build_export_url(base_url: str, persistent_id: str) -> str:
    query = urlencode({"exporter": CROISSANT_EXPORTER, "persistentId": persistent_id})
    return f"{base_url.rstrip('/')}{EXPORT_PATH}?{query}"


def search_items(document: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Read one Search response page."""

    if not isinstance(document, dict):
        raise ScraperError("search response was not a JSON object")
    if normalize_space(document.get("status")).upper() not in {"OK", ""}:
        message = normalize_space(document.get("message")) or "unknown error"
        raise ScraperError(f"search endpoint reported an error: {message}")
    data = document.get("data")
    if not isinstance(data, dict):
        raise ScraperError("search response had no data object")
    items = data.get("items")
    if not isinstance(items, list):
        raise ScraperError("search response had no items array")
    total = data.get("total_count")
    return [item for item in items if isinstance(item, dict)], int(total) if isinstance(total, int) else None


def discovery_from_search_item(item: dict[str, Any], base_url: str, source_page: str) -> dict[str, Any] | None:
    persistent_id = normalize_identifier(item.get("global_id"))
    landing_page = normalize_space(item.get("url"))
    if not landing_page and persistent_id:
        landing_page = f"{base_url.rstrip('/')}/dataset.xhtml?persistentId={persistent_id}"
    if not persistent_id and not landing_page:
        return None
    return {
        "persistent_id": persistent_id,
        "landing_page": normalize_url(landing_page),
        "title": normalize_space(item.get("name")),
        "published_at": normalize_space(item.get("published_at")),
        "collection": normalize_space(item.get("name_of_dataverse")),
        "source_page": source_page,
    }


def croissant_file_nodes(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the FileObject entries of a Croissant export, in page order."""

    nodes: list[dict[str, Any]] = []
    for node in listify(document.get("distribution")):
        if not isinstance(node, dict):
            continue
        # json_type_names() strips the "cr:"/"sc:" prefix and lowercases.
        if "fileobject" in json_type_names(node.get("@type")):
            nodes.append(node)
    return nodes


def parse_dataset_croissant(
    document: Any,
    discovery: dict[str, Any],
    base_url: str,
) -> tuple[dict[str, Any], list[Any]]:
    """Build a normalized record from one Croissant metadata export."""

    if not isinstance(document, dict):
        raise ScraperError("croissant export was not a JSON object")

    persistent_id = normalize_identifier(discovery.get("persistent_id")) or normalize_identifier(
        document.get("@id")
    )
    landing_page = normalize_url(
        normalize_space(discovery.get("landing_page"))
        or normalize_space(document.get("url"))
        or f"{base_url.rstrip('/')}/dataset.xhtml?persistentId={persistent_id}"
    )
    doi = doi_from_identifier(persistent_id)

    authors: list[str] = []
    for value in listify(document.get("creator")) + listify(document.get("author")):
        name = author_name(value)
        if name and name not in authors:
            authors.append(name)

    files: list[dict[str, Any]] = []
    for index, node in enumerate(croissant_file_nodes(document), start=1):
        normalized = normalize_file_node(node, landing_page, index)
        if normalized:
            files.append(normalized)
    files = deduplicate_files(files)

    paper_url = ""
    for field in ("citation", "isBasedOn", "sameAs", "subjectOf"):
        paper_url = external_url(document.get(field), landing_page)
        if paper_url:
            break

    key = record_key(persistent_id or None, landing_page)
    record = {
        "record_key": key,
        "persistent_id": persistent_id,
        "doi": doi,
        "title": first_nonempty(document.get("name"), discovery.get("title")),
        "landing_page": landing_page,
        "description": text_value(document.get("description")),
        "authors": authors,
        "keywords": all_text_values(document.get("keywords")),
        "published_at": first_nonempty(document.get("datePublished"), discovery.get("published_at")),
        "modified_at": text_value(document.get("dateModified")),
        "version": text_value(document.get("version")),
        "license": text_value(document.get("license")),
        "publisher": text_value(document.get("publisher")),
        "collection": first_nonempty(
            text_value(document.get("includedInDataCatalog")), discovery.get("collection")
        ),
        "paper_url": paper_url,
        "citation_text": text_value(document.get("citeAs")),
        "files": files,
        "methods": {
            "discovery": {
                "label": "dataverse_search_endpoint",
                "source_page": discovery.get("source_page", ""),
            },
            "record": {
                "label": "dataverse_croissant_metadata_export",
                "fallback": "",
            },
            "download": {
                "label": "exact_content_url_from_metadata_export",
                "url_construction": False,
            },
        },
        "raw": {
            "dataset_html": "",
            "structured_data": "",
        },
    }
    return record, [document]


class PanHtmlScraper:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.output_root.mkdir(parents=True, exist_ok=True)
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.logs_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint = read_json(settings.checkpoint_path, {}) or {}
        self.checkpoint.setdefault("schema_version", "1.0.0")
        self.checkpoint.setdefault("started_at", utc_now())
        self.checkpoint.setdefault("collections_visited", [])
        self.checkpoint.setdefault("datasets_discovered", [])
        self.checkpoint.setdefault("datasets_completed", [])
        self.checkpoint.setdefault("files_completed", [])

    def record_error(self, stage: str, message: str, *, url: str = "", record: str = "") -> None:
        append_jsonl(
            self.settings.errors_path,
            {
                "time": utc_now(),
                "stage": stage,
                "record": record,
                "url": url,
                "message": message,
            },
        )

    def save_checkpoint(self) -> None:
        self.checkpoint["updated_at"] = utc_now()
        atomic_write_json(self.settings.checkpoint_path, self.checkpoint)

    def discover(self, client: HtmlClient) -> list[dict[str, Any]]:
        if self.settings.source == "api":
            return self.discover_via_api(client)
        return self.discover_via_html(client)

    def discover_via_api(self, client: HtmlClient) -> list[dict[str, Any]]:
        """Page through the Search endpoint for the configured collection."""

        discovered: OrderedDict[str, dict[str, Any]] = OrderedDict()
        start = 0
        reported_total: int | None = None
        page_index = 0
        LOGGER.info("collection: %s (search endpoint)", self.settings.collection)

        while True:
            page_index += 1
            url = build_search_url(
                self.settings.base_url, self.settings.collection, start, SEARCH_PAGE_SIZE
            )
            path = self.settings.search_pages_dir / f"page_{page_index:04d}.json"
            document = client.fetch_json(url, path, self.settings.refresh)
            items, total = search_items(document)
            if reported_total is None:
                reported_total = total
            before = len(discovered)
            for item in items:
                entry = discovery_from_search_item(item, self.settings.base_url, url)
                if entry is None:
                    continue
                key = entry["persistent_id"] or entry["landing_page"]
                discovered.setdefault(key, entry)
                if self.settings.max_records is not None and len(discovered) >= self.settings.max_records:
                    break
            LOGGER.info(
                "  page %d: items=%d new=%d total=%d/%s",
                page_index,
                len(items),
                len(discovered) - before,
                len(discovered),
                reported_total if reported_total is not None else "?",
            )
            if self.settings.max_records is not None and len(discovered) >= self.settings.max_records:
                break
            if not items:
                break
            start += len(items)
            if reported_total is not None and start >= reported_total:
                break
            if page_index >= self.settings.max_pages:
                LOGGER.warning("stopped at --max-pages=%d before the reported total", self.settings.max_pages)
                break

        if reported_total is not None and self.settings.max_records is None and len(discovered) < reported_total:
            LOGGER.warning(
                "search reported %d datasets but only %d unique entries were collected",
                reported_total,
                len(discovered),
            )

        records = list(discovered.values())
        if self.settings.max_records is not None:
            records = records[: self.settings.max_records]
        self.checkpoint["collections_visited"] = [self.settings.collection]
        self.checkpoint["datasets_discovered"] = [
            item.get("persistent_id") or item["landing_page"] for item in records
        ]
        self.save_checkpoint()
        return records

    def discover_via_html(self, client: HtmlClient) -> list[dict[str, Any]]:
        discovered: OrderedDict[str, dict[str, Any]] = OrderedDict()
        queue: deque[str] = deque([self.settings.collection])
        visited: set[str] = set()

        while queue:
            alias = queue.popleft()
            if alias in visited:
                continue
            visited.add(alias)
            LOGGER.info("collection: %s", alias)
            collection_dir = self.settings.collection_pages_dir / safe_segment(alias, "collection")
            page = 1
            max_page = 1
            empty_pages = 0
            while page <= max_page and page <= self.settings.max_pages:
                url = build_collection_url(self.settings.base_url, alias, page)
                path = collection_dir / f"page_{page:04d}.html"
                result = client.fetch_html(url, path, self.settings.refresh)
                parsed = parse_collection_html(result.body, result.final_url, alias)
                max_page = max(max_page, parsed.max_page)
                before = len(discovered)
                for item in parsed.datasets:
                    key = item.get("persistent_id") or item["landing_page"]
                    discovered.setdefault(str(key), dict(item))
                    if self.settings.max_records is not None and len(discovered) >= self.settings.max_records:
                        break
                for child in parsed.subcollections:
                    if child not in visited and child not in queue:
                        queue.append(child)
                new_count = len(discovered) - before
                LOGGER.info(
                    "  page %d/%d: datasets=%d new=%d subcollections=%d total=%d",
                    page,
                    max_page,
                    len(parsed.datasets),
                    new_count,
                    len(parsed.subcollections),
                    len(discovered),
                )
                if not parsed.datasets and not parsed.subcollections:
                    empty_pages += 1
                else:
                    empty_pages = 0
                if empty_pages >= 1:
                    break
                if self.settings.max_records is not None and len(discovered) >= self.settings.max_records:
                    break
                page += 1
            if alias not in self.checkpoint["collections_visited"]:
                self.checkpoint["collections_visited"].append(alias)
                self.save_checkpoint()
            if self.settings.max_records is not None and len(discovered) >= self.settings.max_records:
                break

        records = list(discovered.values())
        if self.settings.max_records is not None:
            records = records[: self.settings.max_records]
        self.checkpoint["datasets_discovered"] = [item.get("persistent_id") or item["landing_page"] for item in records]
        self.save_checkpoint()
        return records

    def record_dir(self, record: dict[str, Any]) -> Path:
        return self.settings.data_dir / record["record_key"]

    def write_record(self, record: dict[str, Any]) -> None:
        path = self.record_dir(record) / "record.json"
        atomic_write_json(path, record)

    def process_dataset(self, client: HtmlClient, discovery: dict[str, Any]) -> dict[str, Any] | None:
        if self.settings.source == "api":
            return self.process_dataset_via_api(client, discovery)
        return self.process_dataset_via_html(client, discovery)

    def process_dataset_via_api(self, client: HtmlClient, discovery: dict[str, Any]) -> dict[str, Any] | None:
        persistent_id = normalize_identifier(discovery.get("persistent_id"))
        provisional_key = record_key(persistent_id or None, discovery.get("landing_page", ""))
        record_directory = self.settings.data_dir / provisional_key
        metadata_path = record_directory / "croissant.json"
        try:
            if not persistent_id:
                raise ScraperError(f"search result had no persistent identifier: {discovery.get('landing_page')}")
            url = build_export_url(self.settings.base_url, persistent_id)
            document = client.fetch_json(url, metadata_path, self.settings.refresh)
            record, documents = parse_dataset_croissant(document, discovery, self.settings.base_url)

            final_dir = self.settings.data_dir / record["record_key"]
            if final_dir != record_directory and metadata_path.exists():
                final_dir.mkdir(parents=True, exist_ok=True)
                target = final_dir / "croissant.json"
                if not target.exists():
                    shutil.move(str(metadata_path), str(target))
                shutil.rmtree(record_directory, ignore_errors=True)
                metadata_path = target

            structured_path = final_dir / "structured_data.json"
            atomic_write_json(structured_path, documents)
            record["raw"]["metadata_export"] = metadata_path.relative_to(self.settings.output_root).as_posix()
            record["raw"]["structured_data"] = structured_path.relative_to(self.settings.output_root).as_posix()
            record["raw"]["metadata_export_url"] = url
            self.write_record(record)
            key = record.get("persistent_id") or record["landing_page"]
            if key not in self.checkpoint["datasets_completed"]:
                self.checkpoint["datasets_completed"].append(key)
                self.save_checkpoint()
            return record
        except BotChallengeError:
            raise
        except Exception as exc:
            self.record_error("dataset", str(exc), url=discovery.get("landing_page", ""), record=provisional_key)
            LOGGER.error("dataset failed: %s: %s", discovery.get("persistent_id") or discovery.get("landing_page"), exc)
            return None

    def process_dataset_via_html(self, client: HtmlClient, discovery: dict[str, Any]) -> dict[str, Any] | None:
        provisional_key = record_key(discovery.get("persistent_id") or None, discovery["landing_page"])
        provisional_dir = self.settings.data_dir / provisional_key
        html_path = provisional_dir / "dataset.html"
        try:
            result = client.fetch_html(discovery["landing_page"], html_path, self.settings.refresh)
            record, documents = parse_dataset_html(result.body, result.final_url, discovery)
            final_dir = self.settings.data_dir / record["record_key"]
            if final_dir != provisional_dir:
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                if final_dir.exists():
                    for child in provisional_dir.iterdir():
                        target = final_dir / child.name
                        if not target.exists():
                            shutil.move(str(child), str(target))
                    shutil.rmtree(provisional_dir, ignore_errors=True)
                else:
                    provisional_dir.rename(final_dir)
                html_path = final_dir / "dataset.html"
            structured_path = final_dir / "structured_data.json"
            atomic_write_json(structured_path, documents)
            record["raw"]["dataset_html"] = html_path.relative_to(self.settings.output_root).as_posix()
            record["raw"]["structured_data"] = structured_path.relative_to(self.settings.output_root).as_posix()
            self.write_record(record)
            key = record.get("persistent_id") or record["landing_page"]
            if key not in self.checkpoint["datasets_completed"]:
                self.checkpoint["datasets_completed"].append(key)
                self.save_checkpoint()
            return record
        except BotChallengeError:
            # A verification page usually affects the whole session, not just
            # one record. Stop instead of silently producing an incomplete
            # catalog that looks successful.
            raise
        except Exception as exc:
            self.record_error("dataset", str(exc), url=discovery["landing_page"], record=provisional_key)
            LOGGER.error("dataset failed: %s: %s", discovery["landing_page"], exc)
            return None

    def selected_for_download(self, file_item: dict[str, Any]) -> bool:
        if not file_item.get("download_url"):
            return False
        name = str(file_item.get("name") or "")
        suffix = Path(name).suffix.lower()
        if self.settings.include_extensions and suffix not in self.settings.include_extensions:
            return False
        if suffix in self.settings.exclude_extensions:
            return False
        size = file_item.get("size_bytes")
        if self.settings.max_file_bytes and isinstance(size, int) and size > self.settings.max_file_bytes:
            return False
        return True

    def download_path(self, record: dict[str, Any], file_item: dict[str, Any]) -> Path:
        ref = safe_segment(str(file_item.get("file_ref") or "file"), "file", 80)
        name = safe_segment(Path(str(file_item.get("name") or "file")).name, f"file_{ref}")
        return self.record_dir(record) / "files" / ref / name

    def enough_disk_space(self, destination: Path) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.settings.min_free_bytes <= 0:
            return True
        free = shutil.disk_usage(destination.parent).free
        return free >= self.settings.min_free_bytes

    def verify_checksum(self, path: Path, checksums: dict[str, str]) -> tuple[bool, str]:
        for algorithm in ("sha256", "sha512", "sha1", "md5"):
            expected = normalize_space(checksums.get(algorithm)).lower()
            if not expected:
                continue
            digest = hashlib.new(algorithm)
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            actual = digest.hexdigest().lower()
            return actual == expected, f"{algorithm}:{actual}"
        return True, f"sha256:{sha256_file(path)}"

    def download_one(self, client: HtmlClient, record: dict[str, Any], file_item: dict[str, Any]) -> None:
        url = str(file_item.get("download_url") or "")
        if not url:
            file_item["download_status"] = "no_download_url"
            return
        if not self.settings.allow_external_downloads and not same_origin(url, self.settings.base_url):
            file_item["download_status"] = "skipped_external_host"
            return
        if not self.selected_for_download(file_item):
            file_item["download_status"] = "skipped_by_filter"
            return

        destination = self.download_path(record, file_item)
        file_item["local_path"] = destination.relative_to(self.settings.output_root).as_posix()
        expected_size = file_item.get("size_bytes") if isinstance(file_item.get("size_bytes"), int) else None
        checksums = file_item.get("checksums") if isinstance(file_item.get("checksums"), dict) else {}

        if destination.exists():
            size_ok = expected_size is None or destination.stat().st_size == expected_size
            checksum_ok, digest = self.verify_checksum(destination, checksums) if size_ok else (False, "")
            if size_ok and checksum_ok:
                file_item["download_status"] = "complete"
                file_item["downloaded_bytes"] = destination.stat().st_size
                file_item["verified_digest"] = digest
                return

        if self.settings.dry_run:
            file_item["download_status"] = "dry_run_selected"
            return
        if not self.enough_disk_space(destination):
            file_item["download_status"] = "skipped_low_disk_space"
            return

        part = destination.with_suffix(destination.suffix + ".part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if part.exists() and not self.settings.resume:
            part.unlink()
        offset = part.stat().st_size if part.exists() else 0
        headers: dict[str, str] = {}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"

        response = client.request("GET", url, headers=headers, stream=True, acceptable=(200, 206, 416))
        try:
            final_url = str(response.url)
            if not redirect_target_allowed(final_url, self.settings.base_url, self.settings.allow_external_downloads):
                file_item["download_status"] = "skipped_external_host"
                file_item["redirected_to"] = final_url
                return
            if not same_origin(final_url, url):
                file_item["redirected_to"] = final_url
            if response.status_code == 416 and expected_size is not None and offset == expected_size:
                checksum_ok, digest = self.verify_checksum(part, checksums)
                if not checksum_ok:
                    raise ScraperError(f"checksum mismatch for completed partial file {url}: {digest}")
                os.replace(part, destination)
                file_item["verified_digest"] = digest
            elif response.status_code == 416:
                part.unlink(missing_ok=True)
                raise ScraperError(f"server rejected resume range for {url}")
            else:
                content_length = response.headers.get("Content-Length")
                incoming = int(content_length) if content_length and content_length.isdigit() else None
                total_estimate = offset + incoming if response.status_code == 206 and incoming is not None else incoming
                if self.settings.max_file_bytes and total_estimate and total_estimate > self.settings.max_file_bytes:
                    file_item["download_status"] = "skipped_too_large"
                    return

                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                expected_mime = str(file_item.get("mime_type") or "").lower()
                mode = "ab" if response.status_code == 206 and offset > 0 else "wb"
                bytes_written = offset if mode == "ab" else 0
                first_chunk = True
                with part.open(mode) as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        if first_chunk:
                            first_chunk = False
                            if content_type in {"text/html", "application/xhtml+xml"} and expected_mime not in {
                                "text/html",
                                "application/xhtml+xml",
                            } and is_bot_challenge(chunk):
                                raise BotChallengeError(f"browser verification blocked download {url}")
                            if content_type == "application/json" and expected_mime != "application/json":
                                snippet = chunk[:4096].decode("utf-8", "ignore").lower()
                                if '"status":"error"' in snippet.replace(" ", "") or '"status": "error"' in snippet:
                                    raise ScraperError(f"Dataverse returned an access error for {url}")
                        bytes_written += len(chunk)
                        if self.settings.max_file_bytes and bytes_written > self.settings.max_file_bytes:
                            raise ScraperError(f"download exceeded configured byte limit: {url}")
                        handle.write(chunk)
                        if not self.enough_disk_space(destination):
                            raise ScraperError("minimum free-disk threshold reached during download")
                if expected_size is not None and part.stat().st_size != expected_size:
                    raise ScraperError(
                        f"size mismatch for {url}: expected {expected_size}, got {part.stat().st_size}"
                    )
                checksum_ok, digest = self.verify_checksum(part, checksums)
                if not checksum_ok:
                    raise ScraperError(f"checksum mismatch for {url}: {digest}")
                os.replace(part, destination)
                file_item["verified_digest"] = digest

            file_item["download_status"] = "complete"
            file_item["downloaded_bytes"] = destination.stat().st_size
            file_item["downloaded_at"] = utc_now()
            checkpoint_key = f"{record['record_key']}:{file_item.get('file_ref')}"
            if checkpoint_key not in self.checkpoint["files_completed"]:
                self.checkpoint["files_completed"].append(checkpoint_key)
                self.save_checkpoint()
        finally:
            response.close()

    def download_record(self, client: HtmlClient, record: dict[str, Any]) -> None:
        for file_item in record.get("files", []):
            try:
                self.download_one(client, record, file_item)
            except BotChallengeError:
                raise
            except Exception as exc:
                file_item["download_status"] = "error"
                file_item["download_error"] = str(exc)
                self.record_error(
                    "download",
                    str(exc),
                    url=str(file_item.get("download_url") or ""),
                    record=record["record_key"],
                )
                LOGGER.error("download failed: %s: %s", file_item.get("name"), exc)
            self.write_record(record)

    def write_catalog(self, records: Sequence[dict[str, Any]]) -> None:
        total_files = sum(len(record.get("files", [])) for record in records)
        downloadable = sum(
            1 for record in records for item in record.get("files", []) if item.get("download_url")
        )
        declared_bytes = sum(
            item["size_bytes"]
            for record in records
            for item in record.get("files", [])
            if isinstance(item.get("size_bytes"), int)
        )
        completed = sum(
            1
            for record in records
            for item in record.get("files", [])
            if item.get("download_status") == "complete"
        )
        catalog = {
            "schema_version": "1.0.0",
            "generated_at": utc_now(),
            "source": {
                "name": "Harvard Dataverse Political Analysis (PAN)",
                "collection_alias": self.settings.collection,
                "collection_url": f"{self.settings.base_url.rstrip('/')}/dataverse/{self.settings.collection}",
                "method": (
                    "dataverse_search_and_croissant_export"
                    if self.settings.source == "api"
                    else "html_collection_and_dataset_pages"
                ),
                "metadata_api_used": self.settings.source == "api",
                "note": (
                    "File URLs are used exactly as the metadata export supplies them; they are never constructed."
                    if self.settings.source == "api"
                    else "File URLs are followed exactly as embedded in dataset HTML; they are never constructed."
                ),
            },
            "summary": {
                "records": len(records),
                "files": total_files,
                "downloadable_files": downloadable,
                "completed_downloads": completed,
                "declared_bytes": declared_bytes,
            },
            "records": list(records),
        }
        atomic_write_json(self.settings.catalog_path, catalog)

        extension_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        for record in records:
            for item in record.get("files", []):
                suffix = Path(str(item.get("name") or "")).suffix.lower() or "[no extension]"
                extension_counts[suffix] += 1
                status_counts[str(item.get("download_status") or "unknown")] += 1
        atomic_write_json(
            self.settings.summary_path,
            {
                "generated_at": utc_now(),
                "records": len(records),
                "files": total_files,
                "downloadable_files": downloadable,
                "declared_bytes": declared_bytes,
                "extensions": dict(sorted(extension_counts.items())),
                "download_statuses": dict(sorted(status_counts.items())),
            },
        )
        atomic_write_json(
            self.settings.missing_papers_path,
            [
                {
                    "record_key": record["record_key"],
                    "persistent_id": record.get("persistent_id", ""),
                    "title": record.get("title", ""),
                    "landing_page": record.get("landing_page", ""),
                }
                for record in records
                if not record.get("paper_url")
            ],
        )

    def run(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with HtmlClient(self.settings) as client:
            discoveries = self.discover(client)
            LOGGER.info("discovered %d dataset pages", len(discoveries))
            for index, discovery in enumerate(discoveries, start=1):
                LOGGER.info("dataset %d/%d: %s", index, len(discoveries), discovery.get("title") or discovery["landing_page"])
                record = self.process_dataset(client, discovery)
                if record is None:
                    continue
                if self.settings.download_files:
                    self.download_record(client, record)
                records.append(record)
                self.write_catalog(records)
        self.write_catalog(records)
        self.checkpoint["finished_at"] = utc_now()
        self.save_checkpoint()
        return records


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inventory and download Harvard PAN datasets from the public Dataverse endpoints or from HTML pages."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inventory-only", action="store_true", help="collect metadata and build catalog.json (default)")
    mode.add_argument("--download-files", action="store_true", help="also download the files the metadata exposes")
    parser.add_argument(
        "--source",
        choices=("api", "html"),
        default="api",
        help=(
            "metadata source: 'api' uses the Search endpoint and Croissant export (default); "
            "'html' parses collection and dataset pages, which Harvard now gates behind a "
            "browser check and serves without file lists"
        ),
    )
    parser.add_argument("--smoke-test", action="store_true", help="inventory at most five records under smoke-output/")
    parser.add_argument("--resume", action="store_true", help="resume existing .part downloads with HTTP Range")
    parser.add_argument("--refresh", action="store_true", help="refresh cached search pages, metadata exports, and HTML")
    parser.add_argument("--dry-run", action="store_true", help="select downloads but do not transfer file bytes")
    parser.add_argument("--base-url", default="https://dataverse.harvard.edu")
    parser.add_argument("--collection", default="pan")
    parser.add_argument("--output-dir", type=Path, default=Path("."), help="directory containing catalog.json and data/")
    parser.add_argument("--max-records", type=positive_int)
    parser.add_argument("--max-pages", type=positive_int, default=10000)
    parser.add_argument(
        "--delay",
        type=nonnegative_float,
        default=1.5,
        help="minimum seconds between HTTP requests (default: 1.5)",
    )
    parser.add_argument(
        "--max-requests-per-minute",
        type=int,
        default=DEFAULT_REQUESTS_PER_MINUTE,
        help=(
            f"rolling ceiling on request rate (default: {DEFAULT_REQUESTS_PER_MINUTE}; "
            "0 disables the ceiling and leaves only --delay)"
        ),
    )
    parser.add_argument("--timeout", type=positive_int, default=120)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--max-file-mb", type=nonnegative_float, default=0.0)
    parser.add_argument("--min-free-gb", type=nonnegative_float, default=0.0)
    parser.add_argument("--include-ext", action="append", help="comma-separated extension allowlist; repeatable")
    parser.add_argument("--exclude-ext", action="append", help="comma-separated extension blocklist; repeatable")
    parser.add_argument(
        "--allow-external-downloads",
        action="store_true",
        help="allow exposed URLs on hosts other than the repository and its object storage",
    )
    parser.add_argument("--cookie-file", type=Path, help="optional Netscape-format browser cookie file")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    output_root = args.output_dir.resolve()
    max_records = args.max_records
    download_files = bool(args.download_files)
    if args.smoke_test:
        output_root = (Path.cwd() / "smoke-output").resolve()
        max_records = min(max_records or 5, 5)
        download_files = False
    contact = normalize_space(os.getenv("PAN_CONTACT_EMAIL"))
    contact_fragment = f"; contact={contact}" if contact else ""
    user_agent = f"PAN-HTML-Scraper/{VERSION} (+https://dataverse.harvard.edu/dataverse/pan{contact_fragment})"
    retries = max(0, int(args.retries))
    requests_per_minute = max(0, int(args.max_requests_per_minute))
    if requests_per_minute == 0:
        LOGGER.warning(
            "--max-requests-per-minute 0 removes the rate ceiling; only --delay=%.1fs paces this run",
            float(args.delay),
        )
    elif requests_per_minute > DEFAULT_REQUESTS_PER_MINUTE:
        LOGGER.warning(
            "--max-requests-per-minute %d is above the tested-safe %d; expect rate-limit pauses",
            requests_per_minute,
            DEFAULT_REQUESTS_PER_MINUTE,
        )
    if float(args.delay) < 1.0:
        LOGGER.warning(
            "--delay %.2fs is below 1s; sustained bursts at that rate have been refused before",
            float(args.delay),
        )
    return Settings(
        base_url=args.base_url.rstrip("/"),
        collection=normalize_space(args.collection) or "pan",
        source=args.source,
        output_root=output_root,
        delay_seconds=float(args.delay),
        requests_per_minute=requests_per_minute,
        timeout_seconds=float(args.timeout),
        max_retries=retries,
        max_pages=int(args.max_pages),
        max_records=max_records,
        refresh=bool(args.refresh),
        download_files=download_files,
        resume=bool(args.resume),
        dry_run=bool(args.dry_run),
        max_file_bytes=int(float(args.max_file_mb) * 1024 * 1024),
        min_free_bytes=int(float(args.min_free_gb) * 1024 * 1024 * 1024),
        include_extensions=parse_extension_set(args.include_ext),
        exclude_extensions=parse_extension_set(args.exclude_ext),
        allow_external_downloads=bool(args.allow_external_downloads),
        cookie_file=args.cookie_file.resolve() if args.cookie_file else None,
        user_agent=user_agent,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    settings = settings_from_args(args)
    try:
        records = PanHtmlScraper(settings).run()
    except KeyboardInterrupt:
        LOGGER.error("interrupted")
        return 130
    except BotChallengeError as exc:
        LOGGER.error("%s", exc)
        return 3
    except ScraperError as exc:
        LOGGER.error("%s", exc)
        return 2
    LOGGER.info("complete: %d records; catalog: %s", len(records), settings.catalog_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
