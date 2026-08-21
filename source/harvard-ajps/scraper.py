#!/usr/bin/env python3
"""Inventory and optionally download public AJPS verification datasets.

The AJPS policy points authors to the AJPS collection in Harvard Dataverse.
This harvester uses only documented, public Dataverse APIs.  It deliberately
does not scrape JavaScript pages, follow arbitrary metadata links, authenticate,
extract archives, or execute deposited code.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, BinaryIO, Iterable, Mapping


SCHEMA_VERSION = "1.0"
DEFAULT_BASE_URL = "https://dataverse.harvard.edu"
DEFAULT_COLLECTION_ALIAS = "ajps"
DEFAULT_POLICY_URL = "https://ajps.org/ajps-verification-policy/"
DEFAULT_USER_AGENT = (
    "ajps-verification-scraper/1.0 "
    "(+https://ajps.org/ajps-verification-policy/; public metadata harvester)"
)
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}
HASH_ALGORITHMS = {
    "MD5": "md5",
    "SHA-1": "sha1",
    "SHA1": "sha1",
    "SHA-256": "sha256",
    "SHA256": "sha256",
    "SHA-512": "sha512",
    "SHA512": "sha512",
}
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class HarvesterError(RuntimeError):
    """A recoverable error associated with one harvest stage."""


@dataclass(frozen=True)
class JsonResponse:
    payload: Any
    body: bytes
    requested_url: str
    final_url: str
    status: int
    headers: Mapping[str, str]


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_url(base_url: str, path: str, params: Mapping[str, Any] | None = None) -> str:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if params:
        cleaned = {key: value for key, value in params.items() if value is not None}
        url += "?" + urllib.parse.urlencode(cleaned, doseq=True)
    return url


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path: Path, value: Any) -> None:
    body = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    atomic_write_bytes(path, body)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def safe_filename(value: Any, fallback: str = "download") -> str:
    """Return a portable basename; never preserve a supplied path."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\\", "/").replace("\x00", "")
    text = text.rsplit("/", 1)[-1]
    text = "".join(character for character in text if ord(character) >= 32)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text or text in {".", ".."}:
        text = fallback
    stem = text.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        text = "_" + text
    if len(text) > 180:
        suffix = Path(text).suffix[:20]
        text = text[: 180 - len(suffix)].rstrip(" .") + suffix
    return text


def storage_key(persistent_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", persistent_id)
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", normalized).strip("._-")
    if not normalized:
        normalized = "dataset"
    digest = hashlib.sha256(persistent_id.encode("utf-8")).hexdigest()[:8]
    return f"{normalized[:168]}--{digest}"


def coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def unique_strings(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)
    return output


def metadata_field(dataset_version: Mapping[str, Any], type_name: str) -> Any:
    blocks = dataset_version.get("metadataBlocks") or {}
    if not isinstance(blocks, Mapping):
        return None
    for block in blocks.values():
        if not isinstance(block, Mapping):
            continue
        for field in block.get("fields") or []:
            if isinstance(field, Mapping) and field.get("typeName") == type_name:
                return field.get("value")
    return None


def compound_value(compound: Any, type_name: str) -> Any:
    if not isinstance(compound, Mapping):
        return None
    field = compound.get(type_name)
    return field.get("value") if isinstance(field, Mapping) else None


def extract_authors(dataset_version: Mapping[str, Any]) -> list[dict[str, Any]]:
    authors: list[dict[str, Any]] = []
    for item in coerce_list(metadata_field(dataset_version, "author")):
        if not isinstance(item, Mapping):
            continue
        name = compound_value(item, "authorName")
        authors.append(
            {
                "name": name,
                "affiliation": compound_value(item, "authorAffiliation"),
                "identifier_scheme": compound_value(item, "authorIdentifierScheme"),
                "identifier": compound_value(item, "authorIdentifier"),
            }
        )
    return [author for author in authors if author["name"]]


def extract_contacts(dataset_version: Mapping[str, Any]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for item in coerce_list(metadata_field(dataset_version, "datasetContact")):
        if not isinstance(item, Mapping):
            continue
        contact = {
            "name": compound_value(item, "datasetContactName"),
            "affiliation": compound_value(item, "datasetContactAffiliation"),
            "email": compound_value(item, "datasetContactEmail"),
        }
        if any(contact.values()):
            contacts.append(contact)
    return contacts


def extract_descriptions(dataset_version: Mapping[str, Any]) -> list[dict[str, Any]]:
    descriptions: list[dict[str, Any]] = []
    for item in coerce_list(metadata_field(dataset_version, "dsDescription")):
        if not isinstance(item, Mapping):
            continue
        value = compound_value(item, "dsDescriptionValue")
        if value:
            descriptions.append(
                {
                    "text": value,
                    "date": compound_value(item, "dsDescriptionDate"),
                }
            )
    return descriptions


def extract_keywords(dataset_version: Mapping[str, Any]) -> list[dict[str, Any]]:
    keywords: list[dict[str, Any]] = []
    for item in coerce_list(metadata_field(dataset_version, "keyword")):
        if not isinstance(item, Mapping):
            continue
        value = compound_value(item, "keywordValue")
        if value:
            keywords.append(
                {
                    "value": value,
                    "vocabulary": compound_value(item, "keywordVocabulary"),
                    "vocabulary_uri": compound_value(item, "keywordVocabularyURI"),
                }
            )
    return keywords


def extract_publications(dataset_version: Mapping[str, Any]) -> list[dict[str, Any]]:
    publications: list[dict[str, Any]] = []
    for item in coerce_list(metadata_field(dataset_version, "publication")):
        if not isinstance(item, Mapping):
            continue
        publication = {
            "citation": compound_value(item, "publicationCitation"),
            "identifier_type": compound_value(item, "publicationIDType"),
            "identifier": compound_value(item, "publicationIDNumber"),
            "url": compound_value(item, "publicationURL"),
        }
        if any(publication.values()):
            publications.append(publication)
    return publications


def extract_urls(value: Any) -> list[str]:
    output: list[str] = []
    if isinstance(value, str):
        for match in URL_RE.findall(value):
            cleaned = match.rstrip(".,;:)]}")
            parsed = urllib.parse.urlparse(cleaned)
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and parsed.username is None
                and parsed.password is None
            ):
                output.append(cleaned)
    elif isinstance(value, Mapping):
        for nested in value.values():
            output.extend(extract_urls(nested))
    elif isinstance(value, list):
        for nested in value:
            output.extend(extract_urls(nested))
    return unique_strings(output)


def version_from_search_item(item: Mapping[str, Any]) -> str:
    major = item.get("majorVersion")
    minor = item.get("minorVersion")
    if major is not None and minor is not None:
        return f"{major}.{minor}"
    version = item.get("version")
    if isinstance(version, str) and re.fullmatch(r"\d+(?:\.\d+)?", version.strip()):
        return version.strip() if "." in version else version.strip() + ".0"
    return ":latest-published"


def persistent_id_from_search_item(item: Mapping[str, Any]) -> str | None:
    for key in ("global_id", "globalId", "persistent_id", "persistentId"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    url = item.get("url")
    if isinstance(url, str):
        values = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get(
            "persistentId"
        )
        if values:
            return values[0]
    return None


def normalize_checksum(data_file: Mapping[str, Any], download_format: str) -> dict[str, Any] | None:
    checksum: Any = None
    scope = "archival"
    if download_format == "original":
        checksum = data_file.get("originalFileChecksum")
        if checksum:
            scope = "original"
        else:
            checksum = data_file.get("checksum")
            scope = "original_or_stored_file"
    else:
        # For ingested tables Dataverse commonly reports the checksum of the
        # deposited original, not the generated archival TSV.
        if not data_file.get("originalFileName"):
            checksum = data_file.get("checksum")
    if not isinstance(checksum, Mapping):
        return None
    checksum_type = checksum.get("type")
    checksum_value = checksum.get("value")
    if not checksum_type or not checksum_value:
        return None
    return {"type": str(checksum_type), "value": str(checksum_value), "scope": scope}


def embargo_is_active(value: Any, *, today: dt.date | None = None) -> bool:
    if not value:
        return False
    if not isinstance(value, Mapping):
        return True
    date_value = value.get("dateAvailable") or value.get("date_available")
    if not date_value:
        return True
    try:
        available = dt.date.fromisoformat(str(date_value)[:10])
    except ValueError:
        return True
    return available > (today or dt.datetime.now(dt.timezone.utc).date())


def normalize_file(
    file_metadata: Mapping[str, Any],
    *,
    base_url: str,
    record_key: str,
    download_format: str,
) -> dict[str, Any]:
    data_file = file_metadata.get("dataFile") or {}
    if not isinstance(data_file, Mapping):
        data_file = {}
    file_id = data_file.get("id")
    if file_id is None:
        raise HarvesterError("Dataset file is missing dataFile.id")
    try:
        file_id_int = int(file_id)
    except (TypeError, ValueError) as error:
        raise HarvesterError(f"Invalid dataFile.id: {file_id!r}") from error

    if download_format == "original":
        label = (
            data_file.get("originalFileName")
            or file_metadata.get("label")
            or data_file.get("filename")
        )
    else:
        label = data_file.get("filename") or file_metadata.get("label")
    filename = safe_filename(label, fallback=f"file_{file_id_int}")
    restricted = bool(file_metadata.get("restricted", data_file.get("restricted", False)))
    embargo = file_metadata.get("embargo") or data_file.get("embargo")
    active_embargo = embargo_is_active(embargo)
    query = {"format": "original"} if download_format == "original" else None
    download_url = build_url(base_url, f"api/access/datafile/{file_id_int}", query)
    persistent_id = data_file.get("persistentId")
    landing_url = (
        build_url(base_url, "file.xhtml", {"persistentId": persistent_id})
        if persistent_id
        else build_url(base_url, "file.xhtml", {"fileId": file_id_int})
    )
    if download_format == "original" and data_file.get("originalFileName"):
        # The archival TSV size is not a safe proxy for a deposited original.
        size = data_file.get("originalFileSize")
    else:
        size = data_file.get("filesize")
    try:
        size_bytes = int(size) if size is not None else None
    except (TypeError, ValueError):
        size_bytes = None

    local_path = f"data/{record_key}/files/{file_id_int}/{filename}"
    return {
        "file_id": file_id_int,
        "persistent_id": persistent_id,
        "filename": filename,
        "dataverse_label": file_metadata.get("label"),
        "stored_filename": data_file.get("filename"),
        "original_filename": data_file.get("originalFileName"),
        "directory_label": file_metadata.get("directoryLabel"),
        "description": file_metadata.get("description") or data_file.get("description"),
        "content_type": data_file.get("contentType"),
        "original_content_type": data_file.get("originalFileFormat"),
        "size_bytes": size_bytes,
        "checksum": normalize_checksum(data_file, download_format),
        "archival_representation": {
            "filename": data_file.get("filename") or file_metadata.get("label"),
            "content_type": data_file.get("contentType"),
            "size_bytes": data_file.get("filesize"),
        },
        "original_representation": {
            "filename": (
                data_file.get("originalFileName")
                or file_metadata.get("label")
                or data_file.get("filename")
            ),
            "content_type": data_file.get("originalFileFormat") or data_file.get("contentType"),
            "size_bytes": (
                data_file.get("originalFileSize")
                if data_file.get("originalFileName")
                else data_file.get("filesize")
            ),
        },
        "categories": coerce_list(file_metadata.get("categories")),
        "tabular_tags": coerce_list(data_file.get("tabularTags")),
        "access": {
            "restricted": restricted,
            "embargo": embargo,
            "embargo_active": active_embargo,
            "status": (
                "restricted"
                if restricted
                else "embargoed"
                if active_embargo
                else "public_metadata"
            ),
            "downloadable_without_auth": False if restricted or active_embargo else None,
        },
        "urls": {"landing_page": landing_url, "download": download_url},
        "download": {
            "requested_format": download_format,
            "status": (
                "skipped_restricted"
                if restricted
                else "skipped_embargo"
                if active_embargo
                else "not_requested"
            ),
            "local_path": local_path,
            "bytes_written": None,
            "checksum_verified": None,
            "attempted_at": None,
            "error": None,
        },
    }


def normalize_record(
    api_response: Mapping[str, Any],
    search_item: Mapping[str, Any],
    *,
    base_url: str,
    collection_alias: str,
    policy_url: str,
    discovery_url: str,
    metadata_url: str,
    download_format: str,
    expected_version: str | None = None,
    harvested_at: str | None = None,
) -> dict[str, Any]:
    if api_response.get("status") not in {None, "OK"}:
        raise HarvesterError(f"Dataverse returned status {api_response.get('status')!r}")
    version = api_response.get("data")
    if not isinstance(version, Mapping):
        raise HarvesterError("Dataset API response is missing a data object")
    persistent_id = version.get("datasetPersistentId") or persistent_id_from_search_item(
        search_item
    )
    if not isinstance(persistent_id, str) or not persistent_id:
        raise HarvesterError("Dataset has no persistent identifier")
    record_key = storage_key(persistent_id)

    major = version.get("versionNumber")
    minor = version.get("versionMinorNumber")
    version_number = f"{major}.{minor}" if major is not None and minor is not None else None
    if (
        expected_version
        and not expected_version.startswith(":")
        and version_number != expected_version
    ):
        raise HarvesterError(
            f"Pinned version mismatch: requested {expected_version}, received {version_number}"
        )
    title = metadata_field(version, "title") or search_item.get("name")
    publications = extract_publications(version)
    related_values = {
        "related_publications": unique_strings(
            coerce_list(metadata_field(version, "relatedPublication"))
        ),
        "related_datasets": unique_strings(
            coerce_list(metadata_field(version, "relatedDataset"))
        ),
        "other_references": unique_strings(
            coerce_list(metadata_field(version, "otherReferences"))
        ),
    }
    # Publication URLs have their own typed home under related_publications;
    # external_links is reserved for other explicit related-material URLs.
    external_urls = extract_urls(related_values)
    dataverse_host = urllib.parse.urlparse(base_url).netloc.lower()
    external_links = [
        {"url": url, "followed": False}
        for url in external_urls
        if urllib.parse.urlparse(url).netloc.lower() != dataverse_host
    ]

    hosted_files: list[dict[str, Any]] = []
    file_errors: list[str] = []
    for file_metadata in version.get("files") or []:
        if not isinstance(file_metadata, Mapping):
            file_errors.append("Ignored non-object file metadata entry")
            continue
        try:
            hosted_files.append(
                normalize_file(
                    file_metadata,
                    base_url=base_url,
                    record_key=record_key,
                    download_format=download_format,
                )
            )
        except HarvesterError as error:
            file_errors.append(str(error))

    hosted_files.sort(key=lambda item: item["file_id"])

    dataset_landing_url = build_url(
        base_url, "dataset.xhtml", {"persistentId": persistent_id}
    )
    license_value = version.get("license")
    if not license_value and version.get("termsOfUse"):
        license_value = {"name": None, "terms_of_use": version.get("termsOfUse")}
    first_publication = publications[0] if publications else None
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": persistent_id,
        "persistent_id": persistent_id,
        "dataset_id": version.get("datasetId"),
        "storage_key": record_key,
        "title": title,
        "citation": version.get("citation") or search_item.get("citation"),
        "description": extract_descriptions(version),
        "authors": extract_authors(version),
        "contacts": extract_contacts(version),
        "subjects": unique_strings(coerce_list(metadata_field(version, "subject"))),
        "keywords": extract_keywords(version),
        "languages": unique_strings(coerce_list(metadata_field(version, "language"))),
        "depositor": version.get("depositor"),
        "publication_date": (
            version.get("publicationDate")
            or version.get("releaseTime")
            or search_item.get("published_at")
        ),
        "dates": {
            "published": (
                version.get("publicationDate")
                or version.get("releaseTime")
                or search_item.get("published_at")
                or ""
            ),
            "updated": version.get("lastUpdateTime") or "",
            "retrieved": utc_now(),
        },
        "version": {
            "version_id": version.get("id"),
            "number": version_number,
            "state": version.get("versionState"),
            "create_time": version.get("createTime"),
            "last_update_time": version.get("lastUpdateTime"),
            "release_time": version.get("releaseTime"),
        },
        "license": license_value,
        "terms_of_access": version.get("termsOfAccess"),
        "file_access_request": version.get("fileAccessRequest"),
        "verification_status": None,
        "paper": first_publication,
        "related_publications": publications,
        "related_materials": related_values,
        "hosted_files": hosted_files,
        "external_links": external_links,
        "urls": {
            "policy": policy_url,
            "collection": build_url(base_url, f"dataverse/{collection_alias}"),
            "dataset": dataset_landing_url,
            "metadata_api": metadata_url,
        },
        "methods": [
            {
                "stage": "discovery",
                "label": "dataverse_search_api_subtree",
                "request": {"method": "GET", "url": discovery_url},
            },
            {
                "stage": "metadata_and_file_inventory",
                "label": "dataverse_native_api_pinned_published_version",
                "request": {"method": "GET", "url": metadata_url},
            },
            {
                "stage": "file_download",
                "label": "dataverse_data_access_api_file_id",
                "request_template": build_url(base_url, "api/access/datafile/{file_id}"),
                "selected_format": download_format,
            },
        ],
        "provenance": {
            "source_collection_alias": collection_alias,
            "search_item": copy.deepcopy(dict(search_item)),
            "harvested_at": harvested_at or utc_now(),
        },
        "harvest_status": "partial" if file_errors else "complete",
        "normalization_warnings": file_errors,
    }


def failed_record(
    persistent_id: str,
    search_item: Mapping[str, Any],
    error: Exception,
    *,
    base_url: str,
    collection_alias: str,
    policy_url: str,
    discovery_url: str,
    metadata_url: str,
) -> dict[str, Any]:
    discovered_version = version_from_search_item(search_item)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": persistent_id,
        "persistent_id": persistent_id,
        "dataset_id": search_item.get("entity_id"),
        "storage_key": storage_key(persistent_id),
        "title": search_item.get("name"),
        "citation": search_item.get("citation"),
        "description": [],
        "authors": [],
        "contacts": [],
        "subjects": unique_strings(coerce_list(search_item.get("subjects"))),
        "keywords": [],
        "languages": [],
        "depositor": None,
        "publication_date": search_item.get("published_at"),
        "dates": {
            "published": search_item.get("published_at") or "",
            "updated": search_item.get("updatedAt") or "",
            "retrieved": utc_now(),
        },
        "version": {
            "version_id": search_item.get("versionId"),
            "number": None if discovered_version.startswith(":") else discovered_version,
            "state": search_item.get("versionState"),
            "create_time": search_item.get("createdAt"),
            "last_update_time": search_item.get("updatedAt"),
            "release_time": search_item.get("published_at"),
        },
        "license": None,
        "terms_of_access": None,
        "file_access_request": None,
        "verification_status": None,
        "paper": None,
        "related_publications": [],
        "related_materials": {
            "related_publications": [],
            "related_datasets": [],
            "other_references": [],
        },
        "hosted_files": [],
        "external_links": [],
        "urls": {
            "policy": policy_url,
            "collection": build_url(base_url, f"dataverse/{collection_alias}"),
            "dataset": build_url(
                base_url, "dataset.xhtml", {"persistentId": persistent_id}
            ),
            "metadata_api": metadata_url,
        },
        "methods": [
            {
                "stage": "discovery",
                "label": "dataverse_search_api_subtree",
                "request": {"method": "GET", "url": discovery_url},
            },
            {
                "stage": "metadata_and_file_inventory",
                "label": "dataverse_native_api_pinned_published_version",
                "request": {"method": "GET", "url": metadata_url},
            },
        ],
        "provenance": {
            "source_collection_alias": collection_alias,
            "search_item": copy.deepcopy(dict(search_item)),
            "harvested_at": utc_now(),
        },
        "harvest_status": "metadata_failed",
        "normalization_warnings": [],
        "error": f"{type(error).__name__}: {error}",
    }


def file_hash(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: Path, file_record: Mapping[str, Any]) -> tuple[bool, bool | None, str | None]:
    expected_size = file_record.get("size_bytes")
    if expected_size is not None:
        try:
            expected_size_int = int(expected_size)
        except (TypeError, ValueError):
            expected_size_int = -1
        if expected_size_int >= 0 and path.stat().st_size != expected_size_int:
            return (
                False,
                None,
                f"size mismatch: expected {expected_size_int}, received {path.stat().st_size}",
            )
    checksum = file_record.get("checksum")
    if isinstance(checksum, Mapping):
        algorithm = HASH_ALGORITHMS.get(str(checksum.get("type", "")).upper())
        expected = str(checksum.get("value", "")).lower()
        if algorithm and expected:
            actual = file_hash(path, algorithm).lower()
            if actual != expected:
                return False, False, f"checksum mismatch: expected {expected}, received {actual}"
            return True, True, None
    return True, None, None


def has_verification_evidence(file_record: Mapping[str, Any]) -> bool:
    if file_record.get("size_bytes") is not None:
        return True
    checksum = file_record.get("checksum")
    return bool(
        isinstance(checksum, Mapping)
        and HASH_ALGORITHMS.get(str(checksum.get("type", "")).upper())
        and checksum.get("value")
    )


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return max(0.0, (parsed - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout: float,
        retries: int,
        delay: float,
        user_agent: str,
    ) -> None:
        parsed = urllib.parse.urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("--base-url must be an absolute HTTPS URL")
        self.base_url = base_url.rstrip("/")
        self.base_host = parsed.netloc.lower()
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.user_agent = user_agent
        self._last_request_started: float | None = None

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.netloc.lower() != self.base_host:
            raise HarvesterError(f"Refusing request outside configured Dataverse host: {url}")

    def _throttle(self) -> None:
        if self._last_request_started is not None and self.delay > 0:
            remaining = self.delay - (time.monotonic() - self._last_request_started)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_started = time.monotonic()

    def open(self, url: str, *, headers: Mapping[str, str] | None = None) -> BinaryIO:
        self._validate_url(url)
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/octet-stream, application/json;q=0.9, */*;q=0.8",
        }
        if headers:
            request_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._throttle()
            request = urllib.request.Request(url, headers=request_headers, method="GET")
            try:
                return urllib.request.urlopen(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in RETRYABLE_HTTP_CODES or attempt >= self.retries:
                    error.close()
                    raise
                retry_after = parse_retry_after(
                    error.headers.get("Retry-After") if error.headers is not None else None
                )
                error.close()
                time.sleep(min(120.0, retry_after if retry_after is not None else 2**attempt))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = error
                if attempt >= self.retries:
                    raise
                time.sleep(min(30.0, 2**attempt))
        raise HarvesterError(f"Request failed: {last_error}")

    def get_json(self, url: str) -> JsonResponse:
        response = self.open(url, headers={"Accept": "application/json"})
        with response:
            body = response.read()
            final_url = response.geturl()
            status = getattr(response, "status", response.getcode())
            headers = dict(response.headers.items())
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HarvesterError(f"Expected JSON from {url}: {error}") from error
        return JsonResponse(payload, body, url, final_url, status, headers)


def update_download_result(
    file_record: dict[str, Any],
    *,
    status: str,
    attempted_at: str | None = None,
    bytes_written: int | None = None,
    checksum_verified: bool | None = None,
    error: str | None = None,
) -> None:
    file_record["download"].update(
        {
            "status": status,
            "attempted_at": attempted_at,
            "bytes_written": bytes_written,
            "checksum_verified": checksum_verified,
            "error": error,
        }
    )


def download_file(
    client: HttpClient,
    file_record: dict[str, Any],
    *,
    output_dir: Path,
    resume: bool,
    max_bytes: int | None,
    min_free_bytes: int,
) -> None:
    attempted_at = utc_now()
    if file_record["access"]["restricted"]:
        update_download_result(file_record, status="skipped_restricted")
        return
    if file_record["access"].get("embargo_active"):
        update_download_result(file_record, status="skipped_embargo")
        return

    expected_size = file_record.get("size_bytes")
    if max_bytes is not None and expected_size is not None and expected_size > max_bytes:
        update_download_result(
            file_record,
            status="skipped_max_size",
            attempted_at=attempted_at,
            error=f"metadata size {expected_size} exceeds limit {max_bytes}",
        )
        return

    relative_path = Path(file_record["download"]["local_path"])
    destination = (output_dir / relative_path.relative_to("data")).resolve()
    if not destination.is_relative_to(output_dir.resolve()):
        raise HarvesterError(f"Refusing unsafe output path: {relative_path}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_name(destination.name + ".part")

    if destination.exists():
        valid, verified, reason = verify_file(destination, file_record)
        if valid and has_verification_evidence(file_record):
            file_record["access"]["downloadable_without_auth"] = True
            update_download_result(
                file_record,
                status="already_present",
                attempted_at=attempted_at,
                bytes_written=destination.stat().st_size,
                checksum_verified=verified,
            )
            return
        # A corrupt file inside this scraper's output is replaced only after a
        # complete, verified .part download succeeds.
        reason = reason or "existing file could not be verified"
    else:
        reason = None

    part_size = part.stat().st_size if resume and part.exists() else 0
    if part.exists() and not resume:
        part.unlink()
        part_size = 0
    if max_bytes is not None and part_size > max_bytes:
        update_download_result(
            file_record,
            status="skipped_max_size",
            attempted_at=attempted_at,
            bytes_written=part_size,
            error=f"partial file already exceeds limit {max_bytes}",
        )
        return

    free_bytes = shutil.disk_usage(destination.parent).free
    additional_expected = max(0, expected_size - part_size) if expected_size else 0
    if free_bytes - additional_expected < min_free_bytes:
        update_download_result(
            file_record,
            status="skipped_free_space",
            attempted_at=attempted_at,
            error=(
                f"free space {free_bytes} minus expected write {additional_expected} "
                f"would fall below reserve {min_free_bytes}"
            ),
        )
        return

    headers = {"Range": f"bytes={part_size}-"} if part_size else {}
    try:
        try:
            response = client.open(file_record["urls"]["download"], headers=headers)
        except urllib.error.HTTPError as error:
            if error.code == 416 and part_size:
                error.close()
                valid, verified, verification_error = verify_file(part, file_record)
                if valid and has_verification_evidence(file_record):
                    os.replace(part, destination)
                    file_record["access"]["downloadable_without_auth"] = True
                    update_download_result(
                        file_record,
                        status="downloaded",
                        attempted_at=attempted_at,
                        bytes_written=destination.stat().st_size,
                        checksum_verified=verified,
                    )
                    return
                raise HarvesterError(
                    "server rejected resume range and partial file is incomplete: "
                    f"{verification_error or 'verification unavailable'}"
                ) from error
            raise
        with response:
            status_code = getattr(response, "status", response.getcode())
            append = bool(part_size and status_code == 206)
            if append:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {part_size}-"):
                    raise HarvesterError(
                        f"invalid Content-Range for resume: {content_range!r}"
                    )
            if part_size and not append:
                part_size = 0
            mode = "ab" if append else "wb"
            bytes_written = part_size
            received_this_response = 0
            response_length = response.headers.get("Content-Length")
            try:
                expected_response_bytes = int(response_length) if response_length else None
            except ValueError:
                expected_response_bytes = None
            with part.open(mode) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    bytes_written += len(chunk)
                    received_this_response += len(chunk)
                    if max_bytes is not None and bytes_written > max_bytes:
                        raise HarvesterError(
                            f"stream exceeded --max-file-mb limit ({max_bytes} bytes)"
                        )
                    if shutil.disk_usage(destination.parent).free - len(chunk) < min_free_bytes:
                        raise HarvesterError("download stopped to preserve free-space reserve")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if (
                expected_response_bytes is not None
                and received_this_response != expected_response_bytes
            ):
                raise HarvesterError(
                    "truncated response: "
                    f"expected {expected_response_bytes} bytes, received {received_this_response}"
                )

        valid, verified, verification_error = verify_file(part, file_record)
        if not valid:
            raise HarvesterError(verification_error or "download verification failed")
        os.replace(part, destination)
        file_record["access"]["downloadable_without_auth"] = True
        update_download_result(
            file_record,
            status="downloaded",
            attempted_at=attempted_at,
            bytes_written=destination.stat().st_size,
            checksum_verified=verified,
            error=None,
        )
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError) and error.code in {401, 403}:
            file_record["access"]["downloadable_without_auth"] = False
        update_download_result(
            file_record,
            status="failed",
            attempted_at=attempted_at,
            bytes_written=part.stat().st_size if part.exists() else 0,
            error=f"{type(error).__name__}: {error}",
        )


def summarize_records(
    records: list[Mapping[str, Any]],
    *,
    source_total: int | None,
    discovered_count: int,
    limited: bool,
    errors_this_run: int,
) -> dict[str, Any]:
    files = [
        file_record
        for record in records
        for file_record in record.get("hosted_files", [])
        if isinstance(file_record, Mapping)
    ]
    restricted_files = [
        file_record
        for file_record in files
        if (file_record.get("access") or {}).get("restricted", False)
    ]
    embargoed_files = [
        file_record
        for file_record in files
        if not (file_record.get("access") or {}).get("restricted", False)
        and (file_record.get("access") or {}).get("embargo_active", False)
    ]
    public_files = [
        file_record
        for file_record in files
        if not (file_record.get("access") or {}).get("restricted", False)
        and not (file_record.get("access") or {}).get("embargo_active", False)
    ]
    statuses: dict[str, int] = {}
    for file_record in files:
        status = str((file_record.get("download") or {}).get("status", "unknown"))
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "source_total_records": source_total,
        "records_discovered": discovered_count,
        "records_in_catalog": len(records),
        "records_complete": sum(
            record.get("harvest_status") == "complete" for record in records
        ),
        "records_failed": sum(
            record.get("harvest_status") != "complete" for record in records
        ),
        "limited_by_max_records": limited,
        "records_with_paper_links": sum(
            bool((record.get("paper") or {}).get("url")) for record in records
        ),
        "external_link_count": sum(len(record.get("external_links", [])) for record in records),
        "hosted_file_count": len(files),
        "public_file_count": len(public_files),
        "restricted_file_count": len(restricted_files),
        "embargoed_file_count": len(embargoed_files),
        "estimated_public_download_bytes": sum(
            int(file_record.get("size_bytes") or 0) for file_record in public_files
        ),
        "download_status_counts": dict(sorted(statuses.items())),
        "errors_this_run": errors_this_run,
    }


class AJPSHarvester:
    def __init__(self, args: argparse.Namespace, client: HttpClient) -> None:
        self.args = args
        self.client = client
        self.output_dir = args.output_dir.resolve()
        self.data_dir = self.output_dir / "data"
        self.raw_search_dir = self.data_dir / "raw" / "search"
        self.state_dir = self.output_dir / "state"
        self.logs_dir = self.output_dir / "logs"
        self.checkpoint_path = self.state_dir / "checkpoint.json"
        self.discovered_path = self.state_dir / "discovered.json"
        self.catalog_path = self.output_dir / "catalog.json"
        self.errors_path = self.logs_dir / "errors.jsonl"
        self.run_id = f"{utc_now()}-{uuid.uuid4().hex[:8]}"
        self.errors_this_run = 0
        self.checkpoint = self._load_or_create_checkpoint()

    def _load_or_create_checkpoint(self) -> dict[str, Any]:
        if self.args.resume and self.checkpoint_path.exists():
            checkpoint = read_json(self.checkpoint_path)
            if checkpoint.get("collection_alias") != self.args.collection_alias:
                raise HarvesterError("Checkpoint collection alias does not match this run")
            if checkpoint.get("base_url") != self.args.base_url:
                raise HarvesterError("Checkpoint Dataverse base URL does not match this run")
            checkpoint["resumed_at"] = utc_now()
            checkpoint["run_id"] = self.run_id
            return checkpoint
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "base_url": self.args.base_url,
            "collection_alias": self.args.collection_alias,
            "mode": "download_files" if self.args.download_files else "inventory_only",
            "started_at": utc_now(),
            "updated_at": utc_now(),
            "discovery": {"status": "pending", "record_count": 0, "source_total": None},
            "records": {},
        }

    def _save_checkpoint(self) -> None:
        self.checkpoint["updated_at"] = utc_now()
        atomic_write_json(self.checkpoint_path, self.checkpoint)

    def _log_error(
        self,
        stage: str,
        error: Exception | str,
        *,
        persistent_id: str | None = None,
        file_id: int | None = None,
        url: str | None = None,
    ) -> None:
        self.errors_this_run += 1
        append_jsonl(
            self.errors_path,
            {
                "timestamp": utc_now(),
                "run_id": self.run_id,
                "stage": stage,
                "persistent_id": persistent_id,
                "file_id": file_id,
                "url": url,
                "error": str(error),
                "error_type": type(error).__name__ if isinstance(error, Exception) else None,
            },
        )

    def _search_url(self, start: int) -> str:
        return build_url(
            self.args.base_url,
            "api/search",
            {
                "q": "*",
                "type": "dataset",
                "subtree": self.args.collection_alias,
                "fq": "publicationStatus:Published",
                "per_page": self.args.per_page,
                "start": start,
                "sort": "date",
                "order": "asc",
                "show_entity_ids": "true",
                "show_api_urls": "true",
                "show_collections": "true",
            },
        )

    def discover(self) -> tuple[list[dict[str, Any]], int | None]:
        if self.args.resume and not self.args.refresh and self.discovered_path.exists():
            saved = read_json(self.discovered_path)
            records = saved.get("records") or []
            same_scope = (
                saved.get("base_url") == self.args.base_url
                and saved.get("collection_alias") == self.args.collection_alias
                and saved.get("requested_max_records") == self.args.max_records
            )
            if same_scope and isinstance(records, list):
                return records, saved.get("source_total_records")

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        start = 0
        source_total: int | None = None
        page_number = 1
        written_search_pages: set[str] = set()
        while True:
            url = self._search_url(start)
            response = self.client.get_json(url)
            raw_path = self.raw_search_dir / f"page_{page_number:06d}.json"
            atomic_write_bytes(raw_path, response.body)
            written_search_pages.add(raw_path.name)
            payload = response.payload
            if not isinstance(payload, Mapping) or payload.get("status") not in {None, "OK"}:
                raise HarvesterError(f"Search API returned an error envelope at start={start}")
            data = payload.get("data")
            if not isinstance(data, Mapping):
                raise HarvesterError(f"Search API response has no data object at start={start}")
            items = data.get("items") or []
            if not isinstance(items, list):
                raise HarvesterError(f"Search API items are not a list at start={start}")
            total_value = data.get("total_count")
            try:
                source_total = int(total_value) if total_value is not None else source_total
            except (TypeError, ValueError):
                pass

            for item in items:
                if not isinstance(item, Mapping):
                    continue
                persistent_id = persistent_id_from_search_item(item)
                if not persistent_id or persistent_id in seen:
                    continue
                seen.add(persistent_id)
                records.append(
                    {
                        "persistent_id": persistent_id,
                        "version": version_from_search_item(item),
                        "discovery_url": url,
                        "search_item": dict(item),
                    }
                )
                if self.args.max_records and len(records) >= self.args.max_records:
                    break

            if self.args.max_records and len(records) >= self.args.max_records:
                break
            if not items:
                break
            next_start = start + len(items)
            if next_start <= start:
                raise HarvesterError("Search API pagination made no progress")
            start = next_start
            page_number += 1
            if source_total is not None and start >= source_total:
                break

        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "base_url": self.args.base_url,
            "collection_alias": self.args.collection_alias,
            "requested_max_records": self.args.max_records,
            "source_total_records": source_total,
            "limited_by_max_records": bool(
                self.args.max_records and source_total and len(records) < source_total
            ),
            "records": records,
        }
        atomic_write_json(self.discovered_path, snapshot)
        for old_page in self.raw_search_dir.glob("page_*.json"):
            if old_page.name not in written_search_pages:
                old_page.unlink()
        self.checkpoint["discovery"] = {
            "status": "complete",
            "record_count": len(records),
            "source_total": source_total,
            "completed_at": utc_now(),
        }
        self._save_checkpoint()
        return records, source_total

    def _metadata_url(self, persistent_id: str, version: str) -> str:
        safe_version = urllib.parse.quote(version, safe=":.")
        return build_url(
            self.args.base_url,
            f"api/datasets/:persistentId/versions/{safe_version}",
            {"persistentId": persistent_id, "excludeFiles": "false"},
        )

    def _record_paths(self, persistent_id: str) -> tuple[Path, Path]:
        directory = self.data_dir / storage_key(persistent_id)
        return directory / "record.json", directory / "api_response.json"

    def _cached_record(
        self, persistent_id: str, expected_version: str
    ) -> dict[str, Any] | None:
        record_path, _ = self._record_paths(persistent_id)
        if not (self.args.resume and not self.args.refresh and record_path.exists()):
            return None
        record = read_json(record_path)
        if (
            isinstance(record, dict)
            and record.get("schema_version") == SCHEMA_VERSION
            and record.get("persistent_id") == persistent_id
            and record.get("harvest_status") == "complete"
            and record.get("provenance", {}).get("source_collection_alias")
            == self.args.collection_alias
        ):
            cached_version = (record.get("version") or {}).get("number")
            if not expected_version.startswith(":") and cached_version != expected_version:
                return None
            metadata_api = (record.get("urls") or {}).get("metadata_api")
            if not isinstance(metadata_api, str):
                return None
            parsed_api = urllib.parse.urlparse(metadata_api)
            parsed_base = urllib.parse.urlparse(self.args.base_url)
            if (
                parsed_api.scheme != parsed_base.scheme
                or parsed_api.netloc.lower() != parsed_base.netloc.lower()
            ):
                return None
            selected_formats = {
                method.get("selected_format")
                for method in record.get("methods", [])
                if isinstance(method, Mapping) and method.get("stage") == "file_download"
            }
            if not selected_formats or self.args.download_format in selected_formats:
                return record
        return None

    def _write_record(self, record: Mapping[str, Any]) -> None:
        record_path, _ = self._record_paths(str(record["persistent_id"]))
        atomic_write_json(record_path, record)

    def _catalog(
        self,
        records: list[Mapping[str, Any]],
        *,
        source_total: int | None,
        discovered_count: int,
    ) -> dict[str, Any]:
        limited = bool(self.args.max_records and source_total and discovered_count < source_total)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": utc_now(),
            "source": {
                "name": "American Journal of Political Science verification materials",
                "policy_url": self.args.policy_url,
                "collection_alias": self.args.collection_alias,
                "collection_url": build_url(
                    self.args.base_url, f"dataverse/{self.args.collection_alias}"
                ),
                "api_base_url": self.args.base_url,
                "version_policy": "published version pinned at discovery time",
            },
            "summary": summarize_records(
                records,
                source_total=source_total,
                discovered_count=discovered_count,
                limited=limited,
                errors_this_run=self.errors_this_run,
            ),
            "records": sorted(
                records, key=lambda record: str(record.get("persistent_id") or "")
            ),
        }

    def run(self) -> int:
        self.raw_search_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            discovered, source_total = self.discover()
        except Exception as error:
            self._log_error("discovery", error)
            self.checkpoint["status"] = "discovery_failed"
            self.checkpoint["discovery"] = {
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "failed_at": utc_now(),
            }
            self._save_checkpoint()
            atomic_write_json(
                self.catalog_path,
                self._catalog([], source_total=None, discovered_count=0),
            )
            return 1
        normalized_records: list[dict[str, Any]] = []
        max_bytes = (
            int(self.args.max_file_mb * 1024 * 1024)
            if self.args.max_file_mb is not None
            else None
        )
        min_free_bytes = int(self.args.min_free_gb * 1024 * 1024 * 1024)

        for index, discovered_record in enumerate(discovered, start=1):
            persistent_id = discovered_record["persistent_id"]
            version = discovered_record["version"]
            search_item = discovered_record["search_item"]
            metadata_url = self._metadata_url(persistent_id, version)
            print(f"[{index}/{len(discovered)}] {persistent_id} ({version})", flush=True)
            record = self._cached_record(persistent_id, version)
            try:
                if record is None:
                    response = self.client.get_json(metadata_url)
                    record_path, api_path = self._record_paths(persistent_id)
                    atomic_write_bytes(api_path, response.body)
                    record = normalize_record(
                        response.payload,
                        search_item,
                        base_url=self.args.base_url,
                        collection_alias=self.args.collection_alias,
                        policy_url=self.args.policy_url,
                        discovery_url=discovered_record["discovery_url"],
                        metadata_url=metadata_url,
                        download_format=self.args.download_format,
                        expected_version=version,
                    )
                    for warning in record.get("normalization_warnings", []):
                        self._log_error(
                            "file_inventory",
                            warning,
                            persistent_id=persistent_id,
                            url=metadata_url,
                        )
                    atomic_write_json(record_path, record)

                if self.args.download_files:
                    for file_record in record.get("hosted_files", []):
                        download_file(
                            self.client,
                            file_record,
                            output_dir=self.data_dir,
                            resume=self.args.resume,
                            max_bytes=max_bytes,
                            min_free_bytes=min_free_bytes,
                        )
                        status = file_record["download"]["status"]
                        if status == "failed":
                            self._log_error(
                                "download",
                                file_record["download"].get("error") or "download failed",
                                persistent_id=persistent_id,
                                file_id=file_record.get("file_id"),
                                url=(file_record.get("urls") or {}).get("download"),
                            )
                        self._write_record(record)

                normalized_records.append(record)
                self.checkpoint["records"][persistent_id] = {
                    "metadata": record.get("harvest_status"),
                    "version": record.get("version", {}).get("number"),
                    "file_statuses": {
                        str(file_record.get("file_id")): file_record.get("download", {}).get(
                            "status"
                        )
                        for file_record in record.get("hosted_files", [])
                    },
                    "updated_at": utc_now(),
                }
            except Exception as error:
                self._log_error(
                    "metadata",
                    error,
                    persistent_id=persistent_id,
                    url=metadata_url,
                )
                record = failed_record(
                    persistent_id,
                    search_item,
                    error,
                    base_url=self.args.base_url,
                    collection_alias=self.args.collection_alias,
                    policy_url=self.args.policy_url,
                    discovery_url=discovered_record["discovery_url"],
                    metadata_url=metadata_url,
                )
                normalized_records.append(record)
                self._write_record(record)
                self.checkpoint["records"][persistent_id] = {
                    "metadata": "failed",
                    "error": str(error),
                    "updated_at": utc_now(),
                }

            self._save_checkpoint()
            atomic_write_json(
                self.catalog_path,
                self._catalog(
                    normalized_records,
                    source_total=source_total,
                    discovered_count=len(discovered),
                ),
            )

        self.checkpoint["completed_at"] = utc_now()
        self.checkpoint["status"] = "complete_with_errors" if self.errors_this_run else "complete"
        self._save_checkpoint()
        catalog = self._catalog(
            normalized_records,
            source_total=source_total,
            discovered_count=len(discovered),
        )
        atomic_write_json(self.catalog_path, catalog)
        print(json.dumps(catalog["summary"], indent=2, sort_keys=True), flush=True)
        return 1 if self.errors_this_run else 0


def nonnegative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory published datasets in the AJPS Harvard Dataverse collection "
            "and optionally download public files."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inventory-only",
        action="store_true",
        help="Collect metadata and file manifests only (the default).",
    )
    mode.add_argument(
        "--download-files",
        action="store_true",
        help="Also download unrestricted repository files.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Inventory three records into smoke-output/ without downloading files.",
    )
    parser.add_argument(
        "--output-dir",
        "--output",
        "--output-root",
        type=Path,
        default=None,
        help="Output directory (default: output/, or smoke-output/ for --smoke-test).",
    )
    parser.add_argument("--resume", action="store_true", help="Reuse completed metadata and .part files.")
    parser.add_argument("--refresh", action="store_true", help="Refresh discovery and metadata API responses.")
    parser.add_argument("--max-records", type=positive_int, default=None)
    parser.add_argument("--per-page", type=positive_int, default=100)
    parser.add_argument("--delay", type=nonnegative_float, default=0.5)
    parser.add_argument("--timeout", type=nonnegative_float, default=60.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-file-mb", type=nonnegative_float, default=None)
    parser.add_argument("--min-free-gb", type=nonnegative_float, default=2.0)
    parser.add_argument(
        "--download-format",
        choices=("original", "archival"),
        default="original",
        help="For ingested tables, save the author's original file or Dataverse's archival TSV.",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--collection-alias", default=DEFAULT_COLLECTION_ALIAS)
    parser.add_argument("--policy-url", default=DEFAULT_POLICY_URL)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    args = parser.parse_args(argv)
    if args.per_page > 1000:
        parser.error("--per-page cannot exceed the Dataverse API maximum of 1000")
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.smoke_test:
        if args.download_files:
            parser.error("--smoke-test cannot be combined with --download-files")
        args.inventory_only = True
        args.max_records = min(args.max_records or 3, 3)
    args.output_dir = args.output_dir or Path("smoke-output" if args.smoke_test else "output")
    args.base_url = args.base_url.rstrip("/")
    return args


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("Python 3.10 or newer is required.", file=sys.stderr)
        return 2
    try:
        args = parse_args(argv)
        client = HttpClient(
            base_url=args.base_url,
            timeout=args.timeout,
            retries=args.retries,
            delay=args.delay,
            user_agent=args.user_agent,
        )
        return AJPSHarvester(args, client).run()
    except KeyboardInterrupt:
        print("Interrupted. Re-run with --resume to continue.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"fatal: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
