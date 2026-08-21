from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

from .http import HttpClient, HttpError
from .models import ReplicationPackage, ResourceRecord
from .utils import extract_doi, extract_dois, normalize_whitespace, parse_int

JOURNALDATA_HOSTS = {"journaldata.zbw.eu", "www.journaldata.zbw.eu"}
DOI_RESOLUTION_HOSTS = JOURNALDATA_HOSTS | {"doi.org", "www.doi.org", "dx.doi.org"}
CKAN_API_BASE = "https://journaldata.zbw.eu/api/3/action"


@dataclass
class CkanResolution:
    package: ReplicationPackage
    landing_metadata: dict[str, Any] | None = None
    landing_html: bytes | None = None
    package_response: dict[str, Any] | None = None
    search_response: dict[str, Any] | None = None


def dataset_slug_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        index = parts.index("dataset")
    except ValueError:
        return None
    if index + 1 >= len(parts):
        return None
    slug = parts[index + 1].strip()
    return slug or None


def _extras_to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    result: dict[str, Any] = {}
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            if key is not None:
                result[str(key)] = item.get("value")
    return result


def _contains_exact_doi(value: Any, doi: str) -> bool:
    if isinstance(value, str):
        return doi in extract_dois(value)
    if isinstance(value, dict):
        return any(_contains_exact_doi(item, doi) for item in value.values())
    if isinstance(value, list):
        return any(_contains_exact_doi(item, doi) for item in value)
    return False


def _resource_id(resource: dict[str, Any]) -> str:
    value = normalize_whitespace(str(resource.get("id") or ""))
    if value:
        return value
    url = str(resource.get("url") or "")
    return "url_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def normalize_resource(resource: dict[str, Any]) -> ResourceRecord:
    url = str(resource.get("url") or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    url_type = normalize_whitespace(str(resource.get("url_type") or ""))
    repository_hosted = host in JOURNALDATA_HOSTS
    looks_like_upload = url_type.lower() == "upload" or "/download" in parsed.path.lower()
    downloadable = bool(url and parsed.scheme in {"http", "https"} and repository_hosted and looks_like_upload)

    if not url:
        reason = "missing_url"
    elif parsed.scheme not in {"http", "https"}:
        reason = "unsupported_url_scheme"
    elif not repository_hosted:
        reason = "external_resource"
    elif not looks_like_upload:
        reason = "repository_link_is_not_a_hosted_upload"
    else:
        reason = "repository_hosted_upload"

    return ResourceRecord(
        resource_id=_resource_id(resource),
        name=normalize_whitespace(str(resource.get("name") or "")) or "unnamed resource",
        url=url,
        description=normalize_whitespace(str(resource.get("description") or "")),
        format=normalize_whitespace(str(resource.get("format") or "")),
        mimetype=normalize_whitespace(str(resource.get("mimetype") or "")),
        mimetype_inner=normalize_whitespace(str(resource.get("mimetype_inner") or "")),
        url_type=url_type,
        size_bytes=parse_int(resource.get("size")),
        created=resource.get("created"),
        last_modified=resource.get("last_modified") or resource.get("cache_last_updated"),
        hash=resource.get("hash"),
        hosted_by_repository=repository_hosted,
        downloadable=downloadable,
        download_reason=reason,
    )


def normalize_package_result(
    result: dict[str, Any],
    *,
    availability_text: str,
    link_url: str | None,
    doi: str | None,
    resolved_url: str | None,
    redirect_chain: list[dict[str, Any]] | None = None,
) -> ReplicationPackage:
    extras = _extras_to_dict(result.get("extras"))
    resources = [normalize_resource(item) for item in result.get("resources", []) if isinstance(item, dict)]
    organization = result.get("organization") if isinstance(result.get("organization"), dict) else None
    tags = [
        normalize_whitespace(str(item.get("name") or ""))
        for item in result.get("tags", [])
        if isinstance(item, dict) and normalize_whitespace(str(item.get("name") or ""))
    ]
    discovered_doi = doi
    if not discovered_doi:
        for candidate in (
            result.get("doi"),
            result.get("identifier"),
            extras.get("DOI"),
            extras.get("doi"),
            extras.get("Identifier"),
            extras.get("identifier"),
        ):
            discovered_doi = extract_doi(str(candidate or ""))
            if discovered_doi:
                break

    return ReplicationPackage(
        availability_text=availability_text,
        link_url=link_url,
        doi=discovered_doi,
        resolved_url=resolved_url,
        redirect_chain=redirect_chain or [],
        repository="ZBW Journal Data Archive",
        dataset_slug=normalize_whitespace(str(result.get("name") or "")) or dataset_slug_from_url(resolved_url),
        dataset_id=normalize_whitespace(str(result.get("id") or "")) or None,
        title=normalize_whitespace(str(result.get("title") or "")) or None,
        notes=normalize_whitespace(str(result.get("notes") or "")) or None,
        author=normalize_whitespace(str(result.get("author") or "")) or None,
        maintainer=normalize_whitespace(str(result.get("maintainer") or "")) or None,
        version=normalize_whitespace(str(result.get("version") or "")) or None,
        license_id=normalize_whitespace(str(result.get("license_id") or "")) or None,
        license_title=normalize_whitespace(str(result.get("license_title") or "")) or None,
        metadata_created=result.get("metadata_created"),
        metadata_modified=result.get("metadata_modified"),
        organization=organization,
        tags=tags,
        extras=extras,
        resources=resources,
        inventory_status="complete",
    )


class JournalDataClient:
    def __init__(self, http: HttpClient, api_base: str = CKAN_API_BASE) -> None:
        self.http = http
        self.api_base = api_base.rstrip("/")
        self._package_cache: dict[str, dict[str, Any]] = {}

    def _action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        endpoint = f"{self.api_base}/{action}"
        payload, _ = self.http.get_json(endpoint, allowed_hosts=JOURNALDATA_HOSTS, params=params)
        if payload.get("success") is not True:
            raise HttpError(f"CKAN {action} failed: {json.dumps(payload.get('error'), ensure_ascii=False)}")
        return payload

    def _package_show(self, slug: str) -> dict[str, Any]:
        if slug in self._package_cache:
            return self._package_cache[slug]
        payload = self._action("package_show", {"id": slug})
        self._package_cache[slug] = payload
        return payload

    def _search_by_doi(self, doi: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        payload = self._action("package_search", {"q": doi, "rows": 20})
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        matches = [
            item
            for item in result.get("results", [])
            if isinstance(item, dict) and _contains_exact_doi(item, doi)
        ]
        if len(matches) == 1:
            return matches[0], payload
        return None, payload

    def resolve(self, package: ReplicationPackage) -> CkanResolution:
        if not package.link_url:
            package.inventory_status = "no_link"
            return CkanResolution(package=package)

        landing_metadata, landing_html = self.http.resolve_page(
            package.link_url,
            allowed_hosts=DOI_RESOLUTION_HOSTS,
        )
        resolved_url = str(landing_metadata.get("resolved_url") or package.link_url)
        final_host = (urlparse(resolved_url).hostname or "").lower()
        package.resolved_url = resolved_url
        package.redirect_chain = list(landing_metadata.get("redirect_chain") or [])

        if final_host not in JOURNALDATA_HOSTS:
            package.repository = final_host or None
            package.inventory_status = "external_repository"
            return CkanResolution(package=package, landing_metadata=landing_metadata, landing_html=landing_html)

        slug = dataset_slug_from_url(resolved_url)
        package_response: dict[str, Any] | None = None
        search_response: dict[str, Any] | None = None
        result: dict[str, Any] | None = None

        package_show_error: str | None = None
        if slug:
            try:
                package_response = self._package_show(slug)
                candidate = package_response.get("result")
                if isinstance(candidate, dict):
                    result = candidate
            except HttpError as exc:
                package_show_error = str(exc)

        if result is None and package.doi:
            result, search_response = self._search_by_doi(package.doi)
            if result is not None:
                slug = normalize_whitespace(str(result.get("name") or "")) or slug
                package_response = {"success": True, "result": result, "source": "package_search_exact_doi_match"}

        if result is None:
            package.inventory_status = "package_not_found"
            package.dataset_slug = slug
            package.error = "The Journal Data landing page resolved, but no CKAN package could be identified."
            if package_show_error:
                package.error += f" package_show error: {package_show_error}"
            return CkanResolution(
                package=package,
                landing_metadata=landing_metadata,
                landing_html=landing_html,
                package_response=package_response,
                search_response=search_response,
            )

        normalized = normalize_package_result(
            result,
            availability_text=package.availability_text,
            link_url=package.link_url,
            doi=package.doi,
            resolved_url=resolved_url,
            redirect_chain=package.redirect_chain,
        )
        if not normalized.dataset_slug:
            normalized.dataset_slug = slug
        return CkanResolution(
            package=normalized,
            landing_metadata=landing_metadata,
            landing_html=landing_html,
            package_response=package_response,
            search_response=search_response,
        )
