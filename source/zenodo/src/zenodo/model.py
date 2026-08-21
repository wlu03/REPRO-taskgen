from __future__ import annotations

import copy
import re
from typing import Any, Iterable
from urllib.parse import quote, urljoin

from .util import html_to_text, human_bytes, localized_text, safe_component, safe_filename, stable_file_id, utc_now


SCHEMA_VERSION = "2.0.0"


def search_hits(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    hits = payload.get("hits", {})
    if isinstance(hits, dict):
        values = hits.get("hits", [])
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []
    if isinstance(hits, list):
        return [item for item in hits if isinstance(item, dict)]
    return []


def search_total(payload: Any) -> int | None:
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return None
    hits = payload.get("hits")
    total = hits.get("total") if isinstance(hits, dict) else None
    if isinstance(total, dict):
        total = total.get("value")
    try:
        return int(total) if total is not None else None
    except (TypeError, ValueError):
        return None


def search_next(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    links = payload.get("links")
    return str(links.get("next") or "") if isinstance(links, dict) else ""


def _record_id(record: dict[str, Any]) -> str:
    return str(record.get("id") or record.get("record_id") or record.get("recid") or "")


def _pid_identifier(container: Any, key: str = "doi") -> str:
    if not isinstance(container, dict):
        return ""
    value = container.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("identifier") or value.get("id") or "")
    return ""


def _relation_id(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("id") or localized_text(value.get("title")) or "")
    return ""


def _resource_type(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"id": value, "title": value}
    if not isinstance(value, dict):
        return {"id": "", "title": ""}
    return {
        "id": str(value.get("id") or value.get("type") or ""),
        "title": localized_text(value.get("title")) or str(value.get("id") or ""),
    }


def _person(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"name": value, "type": "", "given_name": "", "family_name": "", "affiliations": [], "identifiers": []}
    if not isinstance(value, dict):
        return {"name": "", "type": "", "given_name": "", "family_name": "", "affiliations": [], "identifiers": []}
    person = value.get("person_or_org") if isinstance(value.get("person_or_org"), dict) else value
    affiliations = value.get("affiliations", person.get("affiliations", []))
    normalized_affiliations = []
    if isinstance(affiliations, list):
        for affiliation in affiliations:
            if isinstance(affiliation, str):
                normalized_affiliations.append({"name": affiliation, "id": ""})
            elif isinstance(affiliation, dict):
                normalized_affiliations.append({"name": str(affiliation.get("name") or ""), "id": str(affiliation.get("id") or "")})
    identifiers = []
    for key in ("identifiers",):
        if isinstance(person.get(key), list):
            identifiers.extend(copy.deepcopy(person[key]))
    if person.get("orcid"):
        identifiers.append({"scheme": "orcid", "identifier": str(person["orcid"])})
    return {
        "name": str(person.get("name") or value.get("name") or ""),
        "type": str(person.get("type") or value.get("type") or ""),
        "given_name": str(person.get("given_name") or ""),
        "family_name": str(person.get("family_name") or ""),
        "affiliations": normalized_affiliations,
        "identifiers": identifiers,
    }


def _people(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    return [_person(value) for value in values]


def _vocabulary_list(values: Any) -> list[dict[str, str]]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    output = []
    for value in values:
        if isinstance(value, str):
            output.append({"id": value, "title": value})
        elif isinstance(value, dict):
            output.append(
                {
                    "id": str(value.get("id") or ""),
                    "title": localized_text(value.get("title")) or str(value.get("id") or ""),
                }
            )
    return output


def _identifier_url(identifier: Any, scheme: Any = "") -> str:
    text = str(identifier or "").strip()
    if re.match(r"^https://", text, flags=re.IGNORECASE):
        return text
    if re.match(r"^http://", text, flags=re.IGNORECASE):
        return text
    normalized_scheme = str(scheme or "").lower()
    if normalized_scheme == "doi" or re.match(r"^10\.\d{4,9}/\S+$", text):
        return f"https://doi.org/{quote(text, safe='/():;._-')}"
    return ""


def _related_identifiers(metadata: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    source = metadata.get("related_identifiers", [])
    if not isinstance(source, list):
        source = []
    normalized: list[dict[str, Any]] = []
    external: list[dict[str, Any]] = []
    papers: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in source:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("identifier") or "")
        relation = _relation_id(item.get("relation"))
        resource_type = _relation_id(item.get("resource_type"))
        scheme = str(item.get("scheme") or "")
        normalized_item = {
            "identifier": identifier,
            "scheme": scheme,
            "relation": relation,
            "resource_type": resource_type,
        }
        normalized.append(normalized_item)
        url = _identifier_url(identifier, scheme)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        link = {
            "url": url,
            "label": resource_type or relation or scheme or "related identifier",
            "relation": relation,
            "resource_type": resource_type,
            "source": "metadata.related_identifiers",
        }
        external.append(link)
        relation_lower = relation.lower().replace("_", "").replace("-", "")
        type_lower = resource_type.lower()
        if type_lower.startswith("publication") or relation_lower in {
            "issupplementto",
            "isdescribedby",
            "isdocumentedby",
            "isreviewedby",
        }:
            papers.append(copy.deepcopy(link))
    return normalized, external, papers


def _file_entries(record: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    files = record.get("files")
    if isinstance(files, dict):
        entries = files.get("entries")
        if isinstance(entries, dict):
            for key, value in entries.items():
                if isinstance(value, dict):
                    yield str(key), value
            return
        if isinstance(entries, list):
            for value in entries:
                if isinstance(value, dict):
                    yield str(value.get("key") or value.get("filename") or value.get("name") or ""), value
            return
    if isinstance(files, list):
        for value in files:
            if isinstance(value, dict):
                yield str(value.get("key") or value.get("filename") or value.get("name") or value.get("id") or ""), value


def _hosted_files(
    record: dict[str, Any],
    record_id: str,
    record_dir: str,
    files_public: bool | None,
    base_url: str,
) -> list[dict[str, Any]]:
    values = []
    for provided_key, item in _file_entries(record):
        key = str(item.get("key") or item.get("filename") or item.get("name") or provided_key)
        links = item.get("links") if isinstance(item.get("links"), dict) else {}
        current_file_shape = bool(item.get("file_id") or item.get("version_id"))
        download_url = str(links.get("content") or links.get("download") or "")
        if not download_url and not current_file_shape:
            download_url = str(links.get("self") or "")
        if download_url:
            download_url = urljoin(f"{base_url}/", download_url)
        api_url = str(links.get("self") or "")
        if api_url:
            api_url = urljoin(f"{base_url}/", api_url)
        size = item.get("size", item.get("filesize"))
        try:
            size_bytes = int(size) if size is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        file_id = str(item.get("file_id") or item.get("id") or stable_file_id(key))
        filename = safe_filename(key)
        local_path = f"{record_dir}/files/{safe_component(file_id, fallback_prefix='file')}/{filename}"
        file_access = item.get("access") if isinstance(item.get("access"), dict) else {}
        hidden = bool(file_access.get("hidden") or item.get("hidden", False))
        restricted = files_public is False or hidden
        values.append(
            {
                "file_id": file_id,
                "file_version_id": str(item.get("version_id") or ""),
                "key": key,
                "filename": filename,
                "size_bytes": size_bytes,
                "size_human": human_bytes(size_bytes),
                "checksum": str(item.get("checksum") or ""),
                "mimetype": str(item.get("mimetype") or item.get("type") or ""),
                "storage_class": str(item.get("storage_class") or ""),
                "hidden": hidden,
                "access": copy.deepcopy(file_access),
                "source_status": str(item.get("status") or ""),
                "download_url": download_url,
                "api_url": api_url,
                "downloadable": bool(download_url) and not restricted,
                "restricted": restricted,
                "local_path": local_path,
                "status": "not_requested",
                "downloaded_bytes": 0,
                "downloaded_at": "",
                "error": "",
            }
        )
    return values


def _rights(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values = metadata.get("rights")
    if isinstance(values, list):
        output = []
        for value in values:
            if isinstance(value, str):
                output.append({"id": value, "title": value, "link": "", "description": ""})
            elif isinstance(value, dict):
                props = value.get("props") if isinstance(value.get("props"), dict) else {}
                output.append(
                    {
                        "id": str(value.get("id") or ""),
                        "title": localized_text(value.get("title")) or str(value.get("id") or ""),
                        "link": str(value.get("link") or value.get("url") or props.get("url") or ""),
                        "description": localized_text(value.get("description")),
                    }
                )
        return output
    license_value = metadata.get("license")
    if isinstance(license_value, dict):
        return [{"id": str(license_value.get("id") or ""), "title": str(license_value.get("title") or ""), "link": str(license_value.get("url") or ""), "description": ""}]
    if license_value:
        return [{"id": str(license_value), "title": str(license_value), "link": "", "description": ""}]
    return []


def community_summary(raw: dict[str, Any], slug: str, base_url: str) -> dict[str, Any]:
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    return {
        "id": str(raw.get("id") or slug),
        "slug": str(raw.get("slug") or slug),
        "title": localized_text(metadata.get("title")) or localized_text(raw.get("title")) or slug,
        "description": html_to_text(metadata.get("description") or raw.get("description")),
        "url": str((raw.get("links") or {}).get("self_html") or f"{base_url}/communities/{quote(slug)}") if isinstance(raw.get("links"), dict) else f"{base_url}/communities/{quote(slug)}",
    }


def normalize_record(
    record: dict[str, Any],
    *,
    community: dict[str, Any],
    base_url: str,
    raw_response_path: str,
    files_response_path: str,
    discovery_response_path: str,
    collection: dict[str, Any] | None = None,
    source_status: str = "record_api",
    files_source_status: str = "files_api",
) -> dict[str, Any]:
    record_id = _record_id(record)
    safe_record_id = safe_component(record_id, fallback_prefix="record")
    record_dir = f"data/{safe_record_id}"
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    pids = record.get("pids") if isinstance(record.get("pids"), dict) else {}
    parent = record.get("parent") if isinstance(record.get("parent"), dict) else {}
    parent_pids = parent.get("pids") if isinstance(parent.get("pids"), dict) else {}
    doi = _pid_identifier(pids) or str(record.get("doi") or metadata.get("doi") or "")
    concept_id = str(parent.get("id") or record.get("conceptrecid") or record.get("concept_id") or "")
    concept_doi = (
        _pid_identifier(pids, "concept-doi")
        or _pid_identifier(parent_pids, "concept-doi")
        or _pid_identifier(parent_pids)
        or str(record.get("conceptdoi") or "")
    )
    access = record.get("access") if isinstance(record.get("access"), dict) else {}
    legacy_access = str(record.get("access_right") or metadata.get("access_right") or "")
    file_access = str(access.get("files") or "").lower()
    if file_access:
        files_public: bool | None = file_access == "public"
    elif legacy_access:
        files_public = legacy_access in {"open", "public"}
    else:
        files_public = None
    related, external, paper_links = _related_identifiers(metadata)
    links = record.get("links") if isinstance(record.get("links"), dict) else {}
    hosted_files = _hosted_files(record, record_id, record_dir, files_public, base_url)
    title = str(metadata.get("title") or record.get("title") or "")
    description_html = str(metadata.get("description") or "")
    subjects = metadata.get("subjects") if isinstance(metadata.get("subjects"), list) else []
    keywords = metadata.get("keywords") if isinstance(metadata.get("keywords"), list) else []
    references = metadata.get("references") if isinstance(metadata.get("references"), list) else []
    resource_type = _resource_type(metadata.get("resource_type") or record.get("resource_type"))
    statistics = record.get("stats") if isinstance(record.get("stats"), dict) else record.get("statistics") if isinstance(record.get("statistics"), dict) else {}
    versions = record.get("versions") if isinstance(record.get("versions"), dict) else {}
    custom_fields = record.get("custom_fields") if isinstance(record.get("custom_fields"), dict) else {}
    journal = metadata.get("journal") if isinstance(metadata.get("journal"), dict) else custom_fields.get("journal:journal") if isinstance(custom_fields.get("journal:journal"), dict) else {}
    languages = _vocabulary_list(metadata.get("languages") or metadata.get("language"))
    programming_languages = _vocabulary_list(custom_fields.get("code:programmingLanguage"))
    normalized = {
        "schema_version": SCHEMA_VERSION,
        "source_repository": "Zenodo",
        "collection": copy.deepcopy(collection) if isinstance(collection, dict) else {
            "key": str(community.get("slug") or ""),
            "slug": str(community.get("slug") or ""),
            "title": str(community.get("title") or ""),
            "abbreviation": "",
            "aliases": [],
            "records_url": "",
        },
        "community": copy.deepcopy(community),
        "identifiers": {
            "record_id": record_id,
            "concept_record_id": concept_id,
            "doi": doi,
            "concept_doi": concept_doi,
        },
        "title": title,
        "description_html": description_html,
        "description_text": html_to_text(description_html),
        "publication_date": str(metadata.get("publication_date") or ""),
        "created": str(record.get("created") or ""),
        "updated": str(record.get("updated") or record.get("modified") or ""),
        "dates": {
            "published": str(metadata.get("publication_date") or ""),
            "updated": str(record.get("updated") or record.get("modified") or ""),
            "retrieved": utc_now(),
        },
        "revision": record.get("revision"),
        "version": str(metadata.get("version") or record.get("version") or ""),
        "version_index": versions.get("index"),
        "is_latest_version": versions.get("is_latest"),
        "resource_type": resource_type,
        "access": {
            "record": str(access.get("record") or legacy_access or ""),
            "files": str(access.get("files") or legacy_access or ""),
            "status": str(access.get("status") or legacy_access or ""),
            "embargo": copy.deepcopy(access.get("embargo")) if isinstance(access.get("embargo"), dict) else None,
        },
        "licenses": _rights(metadata),
        "creators": _people(metadata.get("creators")),
        "contributors": _people(metadata.get("contributors")),
        "keywords": [str(value) for value in keywords],
        "subjects": copy.deepcopy(subjects),
        "language": languages[0]["id"] if languages else "",
        "languages": languages,
        "publisher": str(metadata.get("publisher") or "Zenodo"),
        "journal": copy.deepcopy(journal),
        "programming_languages": programming_languages,
        "custom_fields": copy.deepcopy(custom_fields),
        "paper_url": paper_links[0]["url"] if paper_links else "",
        "paper_links": paper_links,
        "related_identifiers": related,
        "external_links": external,
        "references": copy.deepcopy(references),
        "hosted_files": hosted_files,
        "links": {
            "record": str(links.get("self_html") or links.get("html") or f"{base_url}/records/{quote(record_id)}"),
            "api": str(links.get("self") or f"{base_url}/api/records/{quote(record_id)}"),
            "doi": f"https://doi.org/{doi}" if doi else "",
            "latest": str(links.get("latest") or ""),
            "latest_html": str(links.get("latest_html") or ""),
            "versions": str(links.get("versions") or ""),
        },
        "statistics": copy.deepcopy(statistics),
        "methods": [
            {
                "stage": "discover_records",
                "method": "zenodo_community_records_api",
                "response_path": discovery_response_path,
            },
            {
                "stage": "fetch_record",
                "method": "zenodo_record_api" if source_status == "record_api" else "zenodo_search_hit_fallback",
                "request_url": f"{base_url}/api/records/{quote(record_id, safe='')}",
                "response_path": raw_response_path if source_status == "record_api" else discovery_response_path,
            },
            {
                "stage": "discover_files",
                "method": "zenodo_record_files_api" if files_source_status == "files_api" else "zenodo_record_embedded_files_fallback",
                "request_url": f"{base_url}/api/records/{quote(record_id, safe='')}/files",
                "response_path": files_response_path if files_source_status == "files_api" else raw_response_path if source_status == "record_api" else discovery_response_path,
            },
            {
                "stage": "download_files",
                "method": "zenodo_api_exact_content_link",
                "enabled": False,
            },
        ],
        "local_paths": {
            "record": f"{record_dir}/record.json",
            "api_response": raw_response_path if source_status == "record_api" else "",
            "files_response": files_response_path if files_source_status == "files_api" else "",
            "files": f"{record_dir}/files",
        },
        "source_status": source_status,
        "files_source_status": files_source_status,
    }
    return normalized


def merge_download_state(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return current
    old_files = {
        str(item.get("key")): item
        for item in previous.get("hosted_files", [])
        if isinstance(item, dict) and item.get("key") is not None
    }
    for item in current.get("hosted_files", []):
        old = old_files.get(str(item.get("key")))
        if not old:
            continue
        if old.get("size_bytes") != item.get("size_bytes") or str(old.get("checksum") or "") != str(item.get("checksum") or ""):
            continue
        for field in ("status", "downloaded_bytes", "downloaded_at", "error"):
            if field in old:
                item[field] = old[field]
    return current


def catalog_summary(records: list[dict[str, Any]], failed_step_count: int) -> dict[str, Any]:
    files = [item for record in records for item in record.get("hosted_files", []) if isinstance(item, dict)]
    known_sizes = [item.get("size_bytes") for item in files if isinstance(item.get("size_bytes"), int)]
    estimated = sum(item.get("size_bytes") for item in files if item.get("downloadable") and isinstance(item.get("size_bytes"), int))
    paper_links = [item for record in records for item in record.get("paper_links", []) if isinstance(item, dict)]
    external = [item for record in records for item in record.get("external_links", []) if isinstance(item, dict)]
    downloaded = [item for item in files if item.get("status") in {"downloaded", "existing"}]
    return {
        "record_count": len(records),
        "record_api_success_count": sum(record.get("source_status") == "record_api" for record in records),
        "record_fallback_count": sum(record.get("source_status") != "record_api" for record in records),
        "open_record_count": sum(str(record.get("access", {}).get("status", "")).lower() in {"open", "public"} for record in records),
        "restricted_record_count": sum(str(record.get("access", {}).get("files", "")).lower() not in {"", "open", "public", "embargoed"} for record in records),
        "records_with_paper_link": sum(bool(record.get("paper_url")) for record in records),
        "paper_link_count": len(paper_links),
        "records_with_hosted_files": sum(bool(record.get("hosted_files")) for record in records),
        "hosted_file_count": len(files),
        "known_hosted_file_bytes": sum(known_sizes),
        "downloadable_file_count": sum(bool(item.get("downloadable")) for item in files),
        "restricted_file_count": sum(bool(item.get("restricted")) for item in files),
        "external_link_count": len(external),
        "estimated_download_bytes": estimated,
        "estimated_download_size_human": human_bytes(estimated),
        "files_with_unknown_size": len(files) - len(known_sizes),
        "downloaded_or_existing_file_count": len(downloaded),
        "downloaded_or_existing_bytes": sum(int(item.get("downloaded_bytes") or 0) for item in downloaded),
        "skipped_file_count": sum(str(item.get("status") or "").startswith("skipped_") for item in files),
        "failed_download_count": sum(item.get("status") == "failed" for item in files),
        "latest_version_count": sum(record.get("is_latest_version") is True for record in records),
        "older_version_count": sum(record.get("is_latest_version") is False for record in records),
        "access_rights": {
            "open": sum(str(record.get("access", {}).get("status", "")).lower() in {"open", "public"} for record in records),
            "embargoed": sum(str(record.get("access", {}).get("status", "")).lower() == "embargoed" for record in records),
            "restricted": sum(str(record.get("access", {}).get("status", "")).lower() == "restricted" for record in records),
            "closed": sum(str(record.get("access", {}).get("status", "")).lower() == "closed" for record in records),
            "unknown": sum(not str(record.get("access", {}).get("status", "")) for record in records),
        },
        "failed_step_count": failed_step_count,
    }
