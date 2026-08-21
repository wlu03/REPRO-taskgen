#!/usr/bin/env python3
"""Inventory AEA studies at ICPSR and optionally download project ZIPs.

Discovery uses ICPSR's public search service.  The program does not bypass
Cloudflare, login, terms acceptance, or study-specific access restrictions.
"""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import math
import os
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from itertools import chain
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urljoin, urlsplit

import requests


SEARCH_ENDPOINT = "https://search.icpsr.umich.edu/search/search/studies"
BIBLIOGRAPHY_ENDPOINT = (
    "https://bibliography.icpsr.umich.edu/bibliography/api/1.0/citations"
)
AEA_CATALOG_URL = "https://www.icpsr.umich.edu/sites/aea/search/studies"
AEA_ARCHIVE = "aea"
DEFAULT_PAGE_SIZE = 500
CATALOG_FLUSH_EVERY = 25
USER_AGENT = (
    "Mozilla/5.0 (compatible; ICPSRAEAScraper/1.0; "
    "+https://www.icpsr.umich.edu/sites/aea/home)"
)
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
SEARCH_MARKERS = (
    "SearchPage, {searchResults : ",
    "searchResults : ",
)
PROJECT_PATH_RE = re.compile(
    r"^/openicpsr/project/(?P<study_id>[0-9]+)/version/"
    r"(?P<version>V[0-9]+)/view/?$",
    flags=re.IGNORECASE,
)
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:A-Z0-9]+", flags=re.IGNORECASE)
PACKAGE_DOI_RE = re.compile(
    r"^10\.3886/(?:E|ICPSR)[0-9]+V[0-9]+$", flags=re.IGNORECASE
)
ALLOWED_DOWNLOAD_SUFFIXES = (
    ".icpsr.umich.edu",
    ".openicpsr.org",
)
# ICPSR redirects packages to object storage.  Accept only S3 and CloudFront
# endpoint shapes rather than every host under amazonaws.com.
ALLOWED_DOWNLOAD_CDN_RE = re.compile(
    r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)*\.amazonaws\.com"
    r"|[a-z0-9][a-z0-9-]*\.cloudfront\.net"
)
LOGIN_PATH_SEGMENTS = {
    "login", "signin", "sign-in", "sso", "oauth", "oauth2", "cas", "saml",
}


class AeaIcpsrAccessBlocked(RuntimeError):
    """The site returned an automated-access challenge."""


class AeaIcpsrAuthenticationRequired(RuntimeError):
    """The requested package requires an authorized ICPSR session."""


class AeaIcpsrTermsRequired(RuntimeError):
    """The requested package requires interactive terms acceptance."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing symlinked log file: {path}")
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def safe_component(value: str, fallback: str) -> str:
    cleaned = SAFE_COMPONENT_RE.sub("_", value.strip()).strip("._")
    return (cleaned or fallback)[:180]


def ensure_direct_child_directory(parent: Path, child: Path) -> None:
    if child.parent != parent:
        raise ValueError(f"directory is not a direct child of {parent}: {child}")
    if child.is_symlink():
        raise ValueError(f"refusing symlinked output directory: {child}")
    child.mkdir(exist_ok=True)
    if child.resolve().parent != parent.resolve():
        raise ValueError(f"output directory escaped its parent: {child}")


def is_cloudflare_challenge(response: requests.Response, body: str = "") -> bool:
    return (
        response.headers.get("cf-mitigated", "").lower() == "challenge"
        or "Just a moment..." in body
        or "challenge-platform" in body
    )


def is_allowed_download_host(host: str) -> bool:
    if any(
        host == suffix[1:] or host.endswith(suffix)
        for suffix in ALLOWED_DOWNLOAD_SUFFIXES
    ):
        return True
    return bool(ALLOWED_DOWNLOAD_CDN_RE.fullmatch(host))


def is_login_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith(("login.", "signin.", "sso.")):
        return True
    return any(
        segment in LOGIN_PATH_SEGMENTS or segment.endswith("login")
        for segment in parts.path.lower().split("/")
        if segment
    )


def is_terms_url(url: str) -> bool:
    return "/download/terms" in urlsplit(url).path.lower()


def validate_https_url(url: str, role: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or not host:
        raise ValueError(f"refusing non-HTTPS or hostless URL: {url}")
    if parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError(f"refusing URL with credentials or nonstandard port: {url}")

    if role == "search":
        if host != "search.icpsr.umich.edu" or parts.path != "/search/search/studies":
            raise ValueError(f"refusing unexpected search URL: {url}")
    elif role == "bibliography":
        if (
            host != "bibliography.icpsr.umich.edu"
            or parts.path != "/bibliography/api/1.0/citations"
        ):
            raise ValueError(f"refusing unexpected bibliography URL: {url}")
    elif role == "download":
        if not is_allowed_download_host(host):
            raise ValueError(f"refusing unexpected download host: {url}")
    else:
        raise ValueError(f"unknown URL role: {role}")
    return url


def redact_url_query(url: str) -> str:
    """Remove query credentials before a redirected URL is written to JSON."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl()


class AeaIcpsrClient:
    """Sequential requests with retries, rate limiting, and redirect checks."""

    def __init__(
        self,
        delay: float,
        cookie_file: Path | None = None,
        timeout: tuple[float, float] = (15.0, 180.0),
        max_retries: int = 5,
    ) -> None:
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.last_request_started = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json,application/zip;q=0.9,*/*;q=0.8",
            }
        )
        if cookie_file is not None:
            jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
            jar.load(ignore_discard=True, ignore_expires=False)
            self.session.cookies.update(jar)

    def close(self) -> None:
        self.session.close()

    def _rate_limit(self) -> None:
        remaining = self.delay - (time.monotonic() - self.last_request_started)
        if remaining > 0:
            time.sleep(remaining)
        self.last_request_started = time.monotonic()

    @staticmethod
    def _retry_after(response: requests.Response, attempt: int) -> float:
        header = response.headers.get("Retry-After", "").strip()
        if header:
            try:
                return min(120.0, max(0.0, float(header)))
            except ValueError:
                try:
                    moment = parsedate_to_datetime(header)
                    if moment.tzinfo is None:
                        moment = moment.replace(tzinfo=timezone.utc)
                    return min(
                        120.0,
                        max(0.0, (moment - datetime.now(timezone.utc)).total_seconds()),
                    )
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(60.0, float(2**attempt))

    def request(
        self,
        method: str,
        url: str,
        *,
        role: str,
        stream: bool = False,
    ) -> requests.Response:
        validate_https_url(url, role)
        for attempt in range(self.max_retries + 1):
            self._rate_limit()
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    stream=stream,
                    allow_redirects=False,
                )
            except requests.RequestException:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(60.0, float(2**attempt)))
                continue
            if response.status_code not in RETRYABLE_STATUSES:
                return response
            if attempt >= self.max_retries:
                return response
            wait_for = self._retry_after(response, attempt)
            response.close()
            time.sleep(wait_for)
        raise AssertionError("request retry loop ended unexpectedly")

    def get_search_html(self, url: str) -> str:
        response = self.request("GET", url, role="search")
        try:
            body = response.text
            if is_cloudflare_challenge(response, body):
                raise AeaIcpsrAccessBlocked(f"Cloudflare challenge at {url}")
            response.raise_for_status()
            return body
        finally:
            response.close()

    def get_json(self, url: str, *, role: str) -> Any:
        response = self.request("GET", url, role=role)
        try:
            body = response.text
            if is_cloudflare_challenge(response, body):
                raise AeaIcpsrAccessBlocked(f"Cloudflare challenge at {url}")
            if response.status_code in (401, 403):
                raise AeaIcpsrAuthenticationRequired(
                    f"HTTP {response.status_code} from {url}"
                )
            response.raise_for_status()
            try:
                return response.json()
            except requests.exceptions.JSONDecodeError as exc:
                raise ValueError(f"endpoint did not return valid JSON: {url}") from exc
        finally:
            response.close()

    def open_download(self, url: str) -> tuple[requests.Response, str]:
        current = validate_https_url(url, "download")
        for _ in range(10):
            if is_login_url(current):
                raise AeaIcpsrAuthenticationRequired(f"login required for {url}")
            if is_terms_url(current):
                raise AeaIcpsrTermsRequired(f"terms acceptance required for {url}")
            response = self.request("GET", current, role="download", stream=True)
            if response.status_code in REDIRECT_STATUSES:
                location = response.headers.get("Location", "")
                response.close()
                if not location:
                    raise RuntimeError(f"redirect without Location from {current}")
                current = urljoin(current, location)
                if is_login_url(current):
                    raise AeaIcpsrAuthenticationRequired(f"login redirect from {url}")
                if is_terms_url(current):
                    raise AeaIcpsrTermsRequired(f"terms redirect from {url}")
                validate_https_url(current, "download")
                continue
            if response.status_code in (401, 403):
                snippet = response.raw.read(8192, decode_content=True).decode(
                    "utf-8", errors="replace"
                )
                if is_cloudflare_challenge(response, snippet):
                    response.close()
                    raise AeaIcpsrAccessBlocked(f"Cloudflare challenge at {current}")
                response.close()
                raise AeaIcpsrAuthenticationRequired(
                    f"HTTP {response.status_code} from {current}"
                )
            try:
                response.raise_for_status()
            except Exception:
                response.close()
                raise
            return response, current
        raise RuntimeError(f"too many redirects from {url}")


class AeaIcpsrFragmentParser(HTMLParser):
    """Extract readable text and links from a small HTML fragment or page."""

    BREAK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        if tag in self.BREAK_TAGS:
            self.text_parts.append("\n")
        if tag == "a":
            attributes = {name.lower(): value or "" for name, value in attrs}
            self._link_href = unescape(attributes.get("href", "")).strip()
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._link_href is not None:
            text = " ".join("".join(self._link_text).split())
            self.links.append({"url": self._link_href, "text": text})
            self._link_href = None
            self._link_text = []
        if tag in self.BREAK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)
        if self._link_href is not None:
            self._link_text.append(data)

    def result(self) -> tuple[str, list[dict[str, str]]]:
        lines = [" ".join(line.split()) for line in "".join(self.text_parts).splitlines()]
        text = "\n".join(line for line in lines if line)
        return text, self.links


def parse_html(value: str, base_url: str = "") -> tuple[str, list[dict[str, str]]]:
    parser = AeaIcpsrFragmentParser()
    parser.feed(value)
    parser.close()
    text, links = parser.result()
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for link in links:
        url = urljoin(base_url, link["url"]) if base_url else link["url"]
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        if url in seen:
            continue
        seen.add(url)
        normalized.append({"url": url, "text": link["text"]})
    return text, normalized


def parse_summary(record: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    raw = record.get("SUMMARY")
    fragments = raw if isinstance(raw, list) else ([raw] if raw else [])
    text_parts: list[str] = []
    links: list[dict[str, str]] = []
    seen: set[str] = set()
    for fragment in fragments:
        text, found = parse_html(str(fragment), str(record.get("URL") or ""))
        if text:
            text_parts.append(text)
        for link in found:
            if link["url"] not in seen:
                seen.add(link["url"])
                links.append(link)
    return "\n".join(text_parts), links


def doi_from_url(url: str) -> str:
    decoded = unescape(url)
    match = DOI_RE.search(decoded)
    if not match:
        return ""
    return match.group(0).rstrip(".,;)]}")


def parse_embedded_search_results(html: str) -> dict[str, Any]:
    for marker in SEARCH_MARKERS:
        position = html.find(marker)
        if position < 0:
            continue
        start = position + len(marker)
        try:
            value, _ = json.JSONDecoder().raw_decode(html[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            response = value.get("response")
            if isinstance(response, dict) and isinstance(response.get("docs"), list):
                return value
    raise ValueError("ICPSR search page did not contain embedded search JSON")


def build_search_url(start: int, rows: int, study_id: str | None = None) -> str:
    parameters: list[tuple[str, str | int]] = [
        ("ARCHIVE", AEA_ARCHIVE),
        ("PUBLISH_STATUS", "PUBLISHED"),
        ("start", start),
        ("rows", rows),
        ("sort", "TITLE_SORT asc"),
    ]
    if study_id is not None:
        parameters.append(("ID", study_id))
    return f"{SEARCH_ENDPOINT}?{urlencode(parameters)}"


def fetch_search_page(
    client: AeaIcpsrClient,
    start: int,
    rows: int,
    cache_dir: Path,
    resume: bool,
    study_id: str | None,
) -> dict[str, Any]:
    suffix = (
        f"id_{study_id}" if study_id else f"start_{start:07d}_rows_{rows:04d}"
    )
    cache_path = cache_dir / f"{suffix}.json"
    if resume and cache_path.exists():
        cached = load_json(cache_path)
        if isinstance(cached, dict) and isinstance(cached.get("search_results"), dict):
            return cached["search_results"]

    url = build_search_url(start, rows, study_id)
    html = client.get_search_html(url)
    results = parse_embedded_search_results(html)
    atomic_write_json(
        cache_path,
        {"source_url": url, "fetched_at": utc_now(), "search_results": results},
    )
    return results


def discover_records(
    client: AeaIcpsrClient,
    cache_dir: Path,
    page_size: int,
    max_records: int | None,
    study_id: str | None,
    resume: bool,
) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = 0
    reported_total: int | None = None

    while True:
        remaining = None if max_records is None else max_records - len(records)
        if remaining is not None and remaining <= 0:
            break
        rows = page_size if remaining is None else min(page_size, remaining)
        page = fetch_search_page(
            client, start, rows, cache_dir, resume, study_id
        )
        response = page.get("response")
        if not isinstance(response, dict):
            raise ValueError("embedded search JSON has no response object")
        docs = response.get("docs")
        if not isinstance(docs, list):
            raise ValueError("embedded search JSON has no docs array")
        total = response.get("numFound")
        if not isinstance(total, int) or total < 0:
            raise ValueError("embedded search JSON has an invalid numFound")
        if reported_total is None:
            reported_total = total
        else:
            reported_total = max(reported_total, total)

        for doc in docs:
            if not isinstance(doc, dict):
                continue
            identifier = str(doc.get("ID") or doc.get("STUDYQ") or "").strip()
            if not identifier.isdigit() or identifier in seen:
                continue
            archives = doc.get("ARCHIVE")
            if isinstance(archives, list) and not any(
                str(item).lower() == AEA_ARCHIVE for item in archives
            ):
                continue
            seen.add(identifier)
            records.append(doc)
            if max_records is not None and len(records) >= max_records:
                break

        if study_id is not None:
            break
        if not docs:
            break
        start += len(docs)
        if start >= total:
            break

    if study_id is not None and not records:
        raise ValueError(f"AEA study {study_id} was not found")
    if study_id is None and max_records is None and len(records) != reported_total:
        raise RuntimeError(
            "catalog changed or discovery returned duplicate/invalid records: "
            f"ICPSR reported {reported_total}, but {len(records)} unique records "
            "were discovered; rerun instead of accepting a partial catalog"
        )
    return records, int(reported_total or 0)


def parse_project_url(url: str, expected_study_id: str) -> dict[str, str] | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() != "https" or host not in {
        "www.openicpsr.org",
        "openicpsr.org",
    }:
        return None
    match = PROJECT_PATH_RE.fullmatch(parts.path)
    if not match:
        return None
    study_id = match.group("study_id")
    version = match.group("version").upper()
    if study_id != expected_study_id:
        return None
    package_url = (
        f"https://www.openicpsr.org/openicpsr/project/{study_id}/version/"
        f"{version}/download/project?"
        + urlencode(
            {"dirPath": f"/openicpsr/{study_id}/fcr:versions/{version}"}
        )
    )
    return {
        "study_id": study_id,
        "version": version,
        "project_url": url,
        "package_url": package_url,
        "filename": f"{study_id}-{version}.zip",
    }


def default_download_state() -> dict[str, Any]:
    return {"status": "not_requested"}


def preserve_download_state(
    resource: dict[str, Any], previous_record: dict[str, Any]
) -> None:
    previous_resources = previous_record.get("resources")
    if not isinstance(previous_resources, list):
        return
    for previous in previous_resources:
        if not isinstance(previous, dict):
            continue
        same_id = previous.get("resource_id") == resource.get("resource_id")
        same_url = previous.get("url") == resource.get("url")
        if same_id and same_url and isinstance(previous.get("download"), dict):
            resource["download"] = previous["download"]
            return


def build_bibliography_url(study_id: str) -> str:
    return f"{BIBLIOGRAPHY_ENDPOINT}?" + urlencode(
        {"path": f"/openicpsr/{study_id}"}
    )


def citation_authors(citation: dict[str, Any]) -> str:
    raw_authors = citation.get("author")
    if not isinstance(raw_authors, list):
        return ""
    names: list[str] = []
    for author in raw_authors:
        if isinstance(author, dict):
            literal = str(author.get("literal") or "").strip()
            given = str(author.get("given") or "").strip()
            family = str(author.get("family") or "").strip()
            name = literal or " ".join(part for part in (given, family) if part)
        else:
            name = str(author).strip()
        if name:
            names.append(name)
    return "; ".join(names)


def citation_text(citation: dict[str, Any]) -> str:
    clobs = citation.get("clobs")
    if isinstance(clobs, dict) and clobs.get("htmlCitation"):
        text, _ = parse_html(str(clobs["htmlCitation"]))
        return " ".join(text.split())
    return ""


def citation_paper_candidate(
    citation: dict[str, Any], index: int
) -> tuple[int, dict[str, str]] | None:
    citation_type = str(citation.get("type") or "").lower()
    if "dataset" in citation_type or citation_type in {"data", "software"}:
        return None

    doi = str(citation.get("DOI") or citation.get("doi") or "").strip()
    doi = doi_from_url(doi) or doi.rstrip(".,;)]}")
    if doi and PACKAGE_DOI_RE.fullmatch(doi):
        return None
    document_like = bool(
        re.search(
            r"article|journal|paper|book|chapter|conference|report|thesis|"
            r"manuscript|proceedings",
            citation_type,
        )
    )
    if doi and doi.lower().startswith("10.1257/"):
        score = 200 - index
        url = f"https://doi.org/{doi}"
    elif doi and document_like:
        score = 150 - index
        url = f"https://doi.org/{doi}"
    else:
        raw_url = str(citation.get("URL") or citation.get("url") or "").strip()
        parts = urlsplit(raw_url)
        host = (parts.hostname or "").lower()
        url_doi = doi_from_url(raw_url)
        if url_doi and PACKAGE_DOI_RE.fullmatch(url_doi):
            return None
        if (
            not document_like
            or parts.scheme.lower() not in {"http", "https"}
            or not host
            or host.endswith("icpsr.umich.edu")
            or host.endswith("openicpsr.org")
        ):
            return None
        score = 100 - index
        url = raw_url
    return score, {
        "title": str(citation.get("title") or ""),
        "authors": citation_authors(citation),
        "citation": citation_text(citation),
        "url": url,
    }


def choose_related_publication(
    citations: list[dict[str, Any]],
) -> dict[str, str] | None:
    candidates: list[tuple[int, dict[str, str]]] = []
    for index, citation in enumerate(citations):
        if not isinstance(citation, dict):
            continue
        candidate = citation_paper_candidate(citation, index)
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def fetch_paper_metadata(
    client: AeaIcpsrClient,
    study_id: str,
    record_dir: Path,
    fetch_requested: bool,
    resume: bool,
    previous_record: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[dict[str, Any]]]:
    related_url = build_bibliography_url(study_id)
    fetch_info: dict[str, Any] = {
        "requested": fetch_requested,
        "status": "not_requested",
        "url": related_url,
    }
    errors: list[str] = []
    chosen: dict[str, str] | None = None
    related_publications: list[dict[str, Any]] = []
    fetch_succeeded = False
    previous_paper = previous_record.get("paper")
    previous_is_verified = isinstance(previous_paper, dict) and (
        previous_paper.get("link_status") in {"present", "absent"}
    )
    previous_related = previous_record.get("related_publications")
    previous_related_is_valid = isinstance(previous_related, list) and all(
        isinstance(item, dict) for item in previous_related
    )

    if fetch_requested:
        json_path = record_dir / "related_publications.json"
        try:
            if resume and json_path.exists():
                raw = load_json(json_path)
                fetched_at = ""
            else:
                raw = client.get_json(related_url, role="bibliography")
                atomic_write_json(json_path, raw)
                fetched_at = utc_now()
            if not isinstance(raw, list) or not all(
                isinstance(item, dict) for item in raw
            ):
                raise ValueError("bibliography endpoint did not return a citation array")
            related_publications = raw
            bibliography_candidate = choose_related_publication(related_publications)
            if bibliography_candidate is not None:
                chosen = bibliography_candidate
            fetch_succeeded = True
            fetch_info = {
                "requested": True,
                "status": "complete",
                "url": related_url,
                "citations_found": len(related_publications),
            }
            if fetched_at:
                fetch_info["fetched_at"] = fetched_at
        except AeaIcpsrAuthenticationRequired as exc:
            fetch_info["status"] = "auth_required"
            errors.append(f"paper_links: AeaIcpsrAuthenticationRequired: {exc}")
        except AeaIcpsrAccessBlocked as exc:
            fetch_info["status"] = "access_blocked"
            errors.append(f"paper_links: AeaIcpsrAccessBlocked: {exc}")
        except Exception as exc:
            fetch_info["status"] = "failed"
            errors.append(f"paper_links: {type(exc).__name__}: {exc}")

    if fetch_succeeded and chosen is not None:
        paper = {
            "title": chosen.get("title", ""),
            "authors": chosen.get("authors", ""),
            "citation": chosen.get("citation", ""),
            "url": chosen.get("url", ""),
            "url_source": "bibliography_api",
            "link_status": "present",
            "outputs": [],
        }
    elif fetch_succeeded:
        paper = {
            "title": "",
            "authors": "",
            "citation": "",
            "url": "",
            "url_source": "",
            "link_status": "absent",
            "outputs": [],
        }
    elif previous_is_verified:
        paper = dict(previous_paper)
        if previous_related_is_valid:
            related_publications = list(previous_related)
        if fetch_requested:
            fetch_info["preserved_previous_verified_value"] = True
    elif fetch_requested:
        paper = {
            "title": "",
            "authors": "",
            "citation": "",
            "url": "",
            "url_source": "",
            "link_status": "fetch_error",
            "outputs": [],
        }
    else:
        paper = {
            "title": "",
            "authors": "",
            "citation": "",
            "url": "",
            "url_source": "",
            "link_status": "not_checked",
            "outputs": [],
        }
    return paper, fetch_info, errors, related_publications


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_content_length(response: requests.Response) -> int | None:
    """Content-Length counts encoded bytes, but iter_content yields decoded
    bytes, so the header is unusable when a transfer encoding is applied."""
    encoding = response.headers.get("Content-Encoding", "").strip().lower()
    if encoding and encoding != "identity":
        return None
    value = response.headers.get("Content-Length", "").strip()
    if not value:
        return None
    try:
        size = int(value)
    except ValueError:
        return None
    return size if size >= 0 else None


def looks_like_html(content_type: str, first_chunk: bytes) -> bool:
    lowered_type = content_type.lower()
    prefix = first_chunk[:512].lstrip().lower()
    return (
        "text/html" in lowered_type
        or prefix.startswith(b"<!doctype html")
        or prefix.startswith(b"<html")
    )


def classify_html_download(first_chunk: bytes) -> str:
    text = first_chunk.decode("utf-8", errors="replace").lower()
    if "cloudflare" in text or "challenge-platform" in text or "just a moment" in text:
        return "access_blocked"
    if "terms" in text and ("accept" in text or "agree" in text):
        return "terms_required"
    return "auth_required"


def verify_existing_download(
    path: Path, prior: dict[str, Any]
) -> dict[str, Any] | None:
    if not path.is_file() or path.is_symlink():
        return None
    expected_size = prior.get("bytes")
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        return None
    if not zipfile.is_zipfile(path):
        return None
    checksum = sha256_file(path)
    expected_checksum = str(prior.get("sha256") or "")
    if expected_checksum and checksum != expected_checksum:
        return None
    result = dict(prior)
    result.update(
        {
            "status": "complete",
            "bytes": path.stat().st_size,
            "sha256": checksum,
            "verified_existing_at": utc_now(),
        }
    )
    return result


def download_package(
    client: AeaIcpsrClient,
    resource: dict[str, Any],
    record_dir: Path,
    max_bytes: int | None,
    min_free_bytes: int,
) -> dict[str, Any]:
    files_dir = record_dir / "files"
    ensure_direct_child_directory(record_dir, files_dir)
    filename = safe_component(
        str(resource.get("filename") or ""),
        f"{resource.get('resource_id', 'project')}.zip",
    )
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    final_path = files_dir / filename
    if final_path.parent != files_dir or final_path.is_symlink():
        raise ValueError(f"refusing unsafe package path: {final_path}")

    prior = resource.get("download")
    if isinstance(prior, dict) and prior.get("status") == "complete":
        verified = verify_existing_download(final_path, prior)
        if verified is not None:
            return verified

    try:
        response, final_url = client.open_download(str(resource["url"]))
    except AeaIcpsrAuthenticationRequired as exc:
        return {"status": "auth_required", "error": str(exc), "checked_at": utc_now()}
    except AeaIcpsrTermsRequired as exc:
        return {"status": "terms_required", "error": str(exc), "checked_at": utc_now()}
    except AeaIcpsrAccessBlocked as exc:
        return {"status": "access_blocked", "error": str(exc), "checked_at": utc_now()}

    temporary = final_path.with_name(f".{final_path.name}.part")
    try:
        content_length = parse_content_length(response)
        if max_bytes is not None and content_length is not None and content_length > max_bytes:
            return {
                "status": "skipped_too_large",
                "reported_bytes": content_length,
                "limit_bytes": max_bytes,
                "checked_at": utc_now(),
            }
        free = shutil.disk_usage(files_dir).free
        if free < min_free_bytes or (
            content_length is not None and free - content_length < min_free_bytes
        ):
            return {
                "status": "skipped_low_space",
                "reported_bytes": content_length,
                "free_bytes": free,
                "reserve_bytes": min_free_bytes,
                "checked_at": utc_now(),
            }

        iterator = response.iter_content(chunk_size=1024 * 1024)
        first_chunk = next(iterator, b"")
        if looks_like_html(response.headers.get("Content-Type", ""), first_chunk):
            return {
                "status": classify_html_download(first_chunk),
                "error": "download endpoint returned HTML instead of a ZIP archive",
                "checked_at": utc_now(),
            }
        if not first_chunk:
            return {
                "status": "failed",
                "error": "download endpoint returned an empty body",
                "checked_at": utc_now(),
            }

        if temporary.exists():
            if temporary.is_symlink() or not temporary.is_file():
                raise ValueError(f"refusing unsafe partial file: {temporary}")
            temporary.unlink()
        digest = hashlib.sha256()
        written = 0
        with temporary.open("xb") as handle:
            for chunk in chain((first_chunk,), iterator):
                if not chunk:
                    continue
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise RuntimeError("download exceeded --max-file-mb")
                if shutil.disk_usage(files_dir).free - len(chunk) < min_free_bytes:
                    raise RuntimeError("download would violate --min-free-gb reserve")
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if content_length is not None and written != content_length:
            raise RuntimeError(
                f"download length mismatch: expected {content_length}, received {written}"
            )
        if not zipfile.is_zipfile(temporary):
            raise RuntimeError("downloaded project is not a valid ZIP archive")
        os.replace(temporary, final_path)
        return {
            "status": "complete",
            "bytes": written,
            "sha256": digest.hexdigest(),
            "path": str(Path("files") / filename),
            "final_url": redact_url_query(final_url),
            "completed_at": utc_now(),
        }
    except Exception:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise
    finally:
        response.close()


def finalize_downloaded_package(
    temporary: Path,
    final_path: Path,
    filename: str,
    max_bytes: int | None,
) -> dict[str, Any]:
    """Validate, hash, and atomically place a completed package file."""
    if not temporary.is_file() or temporary.is_symlink():
        raise RuntimeError("browser did not produce a regular file")
    written = temporary.stat().st_size
    if written == 0:
        raise RuntimeError("browser produced an empty file")
    if max_bytes is not None and written > max_bytes:
        raise RuntimeError("download exceeded --max-file-mb")
    if not zipfile.is_zipfile(temporary):
        raise RuntimeError("downloaded project is not a valid ZIP archive")
    checksum = sha256_file(temporary)
    os.replace(temporary, final_path)
    return {
        "status": "complete",
        "bytes": written,
        "sha256": checksum,
        "path": str(Path("files") / filename),
        "transport": "browser",
        "completed_at": utc_now(),
    }


def download_package_via_browser(
    session: Any,
    resource: dict[str, Any],
    record_dir: Path,
    max_bytes: int | None,
    min_free_bytes: int,
    project_url: str,
) -> dict[str, Any]:
    """Download one project ZIP through an operator-cleared browser session."""
    files_dir = record_dir / "files"
    ensure_direct_child_directory(record_dir, files_dir)
    filename = safe_component(
        str(resource.get("filename") or ""),
        f"{resource.get('resource_id', 'project')}.zip",
    )
    if not filename.lower().endswith(".zip"):
        filename += ".zip"
    final_path = files_dir / filename
    if final_path.parent != files_dir or final_path.is_symlink():
        raise ValueError(f"refusing unsafe package path: {final_path}")

    prior = resource.get("download")
    if isinstance(prior, dict) and prior.get("status") == "complete":
        verified = verify_existing_download(final_path, prior)
        if verified is not None:
            return verified

    free = shutil.disk_usage(files_dir).free
    if free < min_free_bytes:
        return {
            "status": "skipped_low_space",
            "free_bytes": free,
            "reserve_bytes": min_free_bytes,
            "checked_at": utc_now(),
        }

    state = session.ensure_clearance(project_url or str(resource["url"]))
    if state != "ready":
        return {
            "status": {
                "challenge": "access_blocked",
                "terms": "terms_required",
                "login": "auth_required",
            }.get(state, "failed"),
            "error": f"browser session stopped at {state} page",
            "checked_at": utc_now(),
        }

    temporary = final_path.with_name(f".{final_path.name}.part")
    if temporary.exists():
        if temporary.is_symlink() or not temporary.is_file():
            raise ValueError(f"refusing unsafe partial file: {temporary}")
        temporary.unlink()
    try:
        result = session.download(str(resource["url"]), temporary)
        if result.get("status") != "complete":
            result.setdefault("checked_at", utc_now())
            return result
        return finalize_downloaded_package(
            temporary, final_path, filename, max_bytes
        )
    except Exception:
        if temporary.exists() and temporary.is_file() and not temporary.is_symlink():
            temporary.unlink()
        raise


def normalize_record(
    source: dict[str, Any],
    previous_record: dict[str, Any],
    paper: dict[str, Any],
    paper_fetch: dict[str, Any],
    paper_errors: list[str],
    related_publications: list[dict[str, Any]],
) -> dict[str, Any]:
    study_id = str(source.get("ID") or source.get("STUDYQ") or "").strip()
    summary, summary_links = parse_summary(source)
    project = parse_project_url(str(source.get("URL") or ""), study_id)
    errors = list(paper_errors)
    resources: list[dict[str, Any]] = []

    if project is None:
        version = ""
        project_url = str(source.get("URL") or "")
        errors.append("package: discovery record did not contain a valid project/version URL")
    else:
        version = project["version"]
        project_url = project["project_url"]
        package = {
            "resource_id": f"{study_id}-{version}",
            "kind": "project_archive",
            "filename": project["filename"],
            "extension": "zip",
            "version": version,
            "url": project["package_url"],
            "reported_size_bytes": None,
            "download": default_download_state(),
        }
        preserve_download_state(package, previous_record)
        resources.append(package)

    for index, link in enumerate(summary_links, start=1):
        resources.append(
            {
                "resource_id": f"summary-link-{index}",
                "kind": "external_link",
                "title": link.get("text", ""),
                "url": link.get("url", ""),
                "source": "search_summary",
                "download": {"status": "not_applicable"},
            }
        )

    authors = source.get("AUTHOR")
    if not isinstance(authors, list):
        authors = [authors] if authors else []
    archives = source.get("ARCHIVE")
    if not isinstance(archives, list):
        archives = [archives] if archives else []
    return {
        "study_id": study_id,
        "version": version,
        "catalog_title": str(source.get("TITLE") or ""),
        "catalog_url": f"https://www.icpsr.umich.edu/sites/aea/view/studies/{study_id}",
        "project_url": project_url,
        "updated_at": str(source.get("DATEUPDATED") or ""),
        "authors": [str(author) for author in authors if author],
        "summary": summary,
        "owner": str(source.get("OWNER") or ""),
        "archives": [str(archive) for archive in archives if archive],
        "paper": paper,
        "paper_link_fetch": paper_fetch,
        "related_publications": related_publications,
        "resources": resources,
        "source": {
            "discovery_service": SEARCH_ENDPOINT,
            "source_record_file": "search_record.json",
        },
        "errors": errors,
    }


def build_summary(
    records: list[dict[str, Any]], reported_total: int, selection: str
) -> dict[str, Any]:
    paper_counts: dict[str, int] = {}
    paper_fetch_counts: dict[str, int] = {}
    download_counts: dict[str, int] = {}
    versions: dict[str, int] = {}
    packages = 0
    external_links = 0
    related_publications_found = 0
    downloaded_bytes = 0
    for record in records:
        paper = record.get("paper")
        paper_status = (
            str(paper.get("link_status") or "unknown")
            if isinstance(paper, dict)
            else "unknown"
        )
        paper_counts[paper_status] = paper_counts.get(paper_status, 0) + 1
        paper_fetch = record.get("paper_link_fetch")
        paper_fetch_status = (
            str(paper_fetch.get("status") or "unknown")
            if isinstance(paper_fetch, dict)
            else "unknown"
        )
        paper_fetch_counts[paper_fetch_status] = (
            paper_fetch_counts.get(paper_fetch_status, 0) + 1
        )
        version = str(record.get("version") or "unknown")
        versions[version] = versions.get(version, 0) + 1
        related = record.get("related_publications")
        if isinstance(related, list):
            related_publications_found += len(related)
        for resource in record.get("resources", []):
            if not isinstance(resource, dict):
                continue
            if resource.get("kind") == "project_archive":
                packages += 1
                download = resource.get("download")
                status = (
                    str(download.get("status") or "unknown")
                    if isinstance(download, dict)
                    else "unknown"
                )
                download_counts[status] = download_counts.get(status, 0) + 1
                if isinstance(download, dict) and status == "complete":
                    downloaded_bytes += int(download.get("bytes") or 0)
            elif resource.get("kind") == "external_link":
                external_links += 1
    return {
        "generated_at": utc_now(),
        "reported_catalog_total": reported_total,
        "processed_records": len(records),
        "selection": selection,
        "paper_links_present": paper_counts.get("present", 0),
        "paper_links_missing": paper_counts.get("absent", 0),
        "paper_link_fetch_errors": sum(
            paper_fetch_counts.get(status, 0)
            for status in ("failed", "auth_required", "access_blocked")
        ),
        "paper_links_not_checked": paper_counts.get("not_checked", 0),
        "paper_link_fetch_status_counts": dict(sorted(paper_fetch_counts.items())),
        "packages_found": packages,
        "external_summary_links": external_links,
        "related_publications_found": related_publications_found,
        "version_counts": dict(sorted(versions.items())),
        "downloaded_bytes": downloaded_bytes,
        "download_status_counts": dict(sorted(download_counts.items())),
        "records_with_errors": sum(bool(record.get("errors")) for record in records),
        "source_catalog": AEA_CATALOG_URL,
        "discovery_service": SEARCH_ENDPOINT,
        "metadata_download_date": datetime.now(timezone.utc).date().isoformat(),
    }


def build_catalog_document(
    records: list[dict[str, Any]], reported_total: int, selection: str
) -> dict[str, Any]:
    return {
        "summary": build_summary(records, reported_total, selection),
        "records": records,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Inventory American Economic Association studies at ICPSR and "
            "optionally download project ZIPs."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--download-files",
        action="store_true",
        help="attempt to download each current project ZIP",
    )
    mode.add_argument(
        "--inventory-only",
        action="store_true",
        help="write metadata only (the default)",
    )
    parser.add_argument(
        "--fetch-paper-links",
        action="store_true",
        help="query ICPSR bibliography metadata for explicit paper links",
    )
    parser.add_argument(
        "--study-id",
        help="process one numeric ICPSR study ID",
    )
    parser.add_argument(
        "--max-records", type=int, help="process at most this many records"
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=DEFAULT_PAGE_SIZE,
        help=f"discovery records per request (default: {DEFAULT_PAGE_SIZE})",
    )
    parser.add_argument(
        "--max-file-mb",
        type=float,
        help="skip a project ZIP if it exceeds this many MiB",
    )
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=5.0,
        help="preserve this much free disk during downloads (default: 5 GiB)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="minimum seconds between HTTP requests (default: 1.0)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reuse cached discovery/detail pages and verify existing ZIPs",
    )
    parser.add_argument(
        "--browser",
        action="store_true",
        help=(
            "download through a persistent Chromium profile with a person in "
            "the loop; required for openICPSR, which is behind a Cloudflare "
            "managed challenge"
        ),
    )
    parser.add_argument(
        "--browser-profile",
        type=Path,
        default=None,
        help="Chromium profile directory to reuse (default: browser-profile/)",
    )
    parser.add_argument(
        "--browser-wait",
        type=float,
        default=600.0,
        help="seconds to wait for the operator per study (default: 600)",
    )
    parser.add_argument(
        "--browser-auto-login",
        action="store_true",
        help=(
            "sign in using browser_login._auto_login with credentials from "
            "$ICPSR_EMAIL and $ICPSR_PASSWORD (openICPSR's session cookie dies "
            "with the browser, so the sign-in must happen in this same run)"
        ),
    )
    parser.add_argument(
        "--browser-headless",
        action="store_true",
        help="run Chromium headless (only useful once a profile is cleared)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="authorized Netscape-format cookie jar for downloads (optional)",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="output directory (default: the scraper project directory)",
    )
    args = parser.parse_args(argv)
    if args.study_id is not None and not str(args.study_id).isdigit():
        parser.error("--study-id must contain digits only")
    if args.max_records is not None and args.max_records <= 0:
        parser.error("--max-records must be greater than zero")
    if args.page_size <= 0 or args.page_size > 1000:
        parser.error("--page-size must be between 1 and 1000")
    if args.max_file_mb is not None and (
        not math.isfinite(args.max_file_mb) or args.max_file_mb <= 0
    ):
        parser.error("--max-file-mb must be finite and greater than zero")
    if not math.isfinite(args.min_free_gb) or args.min_free_gb < 0:
        parser.error("--min-free-gb must be finite and nonnegative")
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay must be finite and nonnegative")
    if args.browser and not args.download_files:
        parser.error("--browser only applies with --download-files")
    if not math.isfinite(args.browser_wait) or args.browser_wait < 0:
        parser.error("--browser-wait must be finite and nonnegative")
    if args.browser_profile is not None:
        args.browser_profile = args.browser_profile.expanduser().resolve()
    if args.cookie_file is not None:
        args.cookie_file = args.cookie_file.expanduser().resolve()
        if not args.cookie_file.is_file():
            parser.error("--cookie-file must point to an existing regular file")
    return args


def run(args: argparse.Namespace) -> int:
    project_root = Path(__file__).resolve().parent
    root = (
        args.output_root.expanduser().resolve()
        if args.output_root is not None
        else project_root
    )
    root.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    state_dir = root / "state"
    search_cache_dir = state_dir / "search_pages"
    logs_dir = root / "logs"
    for parent, child in (
        (root, data_dir),
        (root, state_dir),
        (state_dir, search_cache_dir),
        (root, logs_dir),
    ):
        ensure_direct_child_directory(parent, child)

    errors_path = logs_dir / "errors.jsonl"
    catalog_path = root / "catalog.json"
    checkpoint_path = state_dir / "checkpoint.json"
    client = AeaIcpsrClient(args.delay, args.cookie_file)
    browser_session = None
    if args.browser:
        from browser_download import AeaIcpsrBrowserSession

        profile_dir = args.browser_profile or (root / "browser-profile")
        browser_session = AeaIcpsrBrowserSession(
            profile_dir,
            headless=args.browser_headless,
            wait_seconds=args.browser_wait,
        )
        browser_session.start()
        print(
            f"Browser profile: {profile_dir} (a Chromium window will open).",
            flush=True,
        )
        if args.browser_auto_login:
            email = os.environ.get("ICPSR_EMAIL", "")
            password = os.environ.get("ICPSR_PASSWORD", "")
            if not email or not password:
                raise RuntimeError(
                    "--browser-auto-login needs $ICPSR_EMAIL and $ICPSR_PASSWORD"
                )
            from browser_login import _auto_login

            print("Signing in to openICPSR...", flush=True)
            _auto_login(browser_session, email, password)
            if browser_session.is_authenticated():
                print("Signed in.", flush=True)
            else:
                print(
                    "Sign-in did not register; the run will prompt per study.",
                    file=sys.stderr,
                    flush=True,
                )
    records: list[dict[str, Any]] = []
    failure_count = 0
    warning_count = 0
    started_at = utc_now()
    selection = f"study_id:{args.study_id}" if args.study_id else "aea_archive"
    try:
        print("Discovering AEA studies from ICPSR's public search service...", flush=True)
        source_records, reported_total = discover_records(
            client,
            search_cache_dir,
            args.page_size,
            args.max_records,
            args.study_id,
            args.resume,
        )
        print(
            f"Search reports {reported_total} record(s); processing {len(source_records)}.",
            flush=True,
        )
        checkpoint: dict[str, Any] = {
            "started_at": started_at,
            "updated_at": started_at,
            "reported_catalog_total": reported_total,
            "selected_total": len(source_records),
            "completed_count": 0,
            "processed_study_ids": [],
            "mode": "download" if args.download_files else "inventory",
            "fetch_paper_links": args.fetch_paper_links,
            "failure_count": 0,
            "warning_count": 0,
        }
        atomic_write_json(checkpoint_path, checkpoint)

        processed_ids: list[str] = []
        for index, source in enumerate(source_records, start=1):
            study_id = str(source.get("ID") or source.get("STUDYQ") or "").strip()
            record_key = safe_component(study_id, f"record_{index}")
            record_dir = data_dir / record_key
            ensure_direct_child_directory(data_dir, record_dir)
            record_path = record_dir / "record.json"
            previous_record: dict[str, Any] = {}
            if record_path.exists():
                try:
                    loaded = load_json(record_path)
                    if isinstance(loaded, dict):
                        previous_record = loaded
                except (OSError, json.JSONDecodeError):
                    pass

            print(f"[{index}/{len(source_records)}] ICPSR {study_id}", flush=True)
            atomic_write_json(record_dir / "search_record.json", source)
            paper, paper_fetch, paper_errors, related_publications = (
                fetch_paper_metadata(
                    client,
                    study_id,
                    record_dir,
                    args.fetch_paper_links,
                    args.resume,
                    previous_record,
                )
            )
            for message in paper_errors:
                failure_count += 1
                append_jsonl(
                    errors_path,
                    {
                        "time": utc_now(),
                        "study_id": study_id,
                        "stage": "paper_links",
                        "error": message,
                    },
                )

            normalized = normalize_record(
                source,
                previous_record,
                paper,
                paper_fetch,
                paper_errors,
                related_publications,
            )
            if any(error.startswith("package:") for error in normalized["errors"]):
                failure_count += 1
                append_jsonl(
                    errors_path,
                    {
                        "time": utc_now(),
                        "study_id": study_id,
                        "stage": "package_url",
                        "error": normalized["errors"][-1],
                    },
                )
            atomic_write_json(record_path, normalized)

            if args.download_files:
                max_bytes = (
                    int(args.max_file_mb * 1024 * 1024)
                    if args.max_file_mb is not None
                    else None
                )
                min_free_bytes = int(args.min_free_gb * 1024**3)
                for resource in normalized["resources"]:
                    if resource.get("kind") != "project_archive":
                        continue
                    try:
                        if browser_session is not None:
                            resource["download"] = download_package_via_browser(
                                browser_session,
                                resource,
                                record_dir,
                                max_bytes,
                                min_free_bytes,
                                normalized.get("project_url", ""),
                            )
                        else:
                            resource["download"] = download_package(
                                client,
                                resource,
                                record_dir,
                                max_bytes,
                                min_free_bytes,
                            )
                        if resource["download"]["status"] in {
                            "auth_required",
                            "terms_required",
                            "access_blocked",
                            "skipped_too_large",
                            "skipped_low_space",
                        }:
                            warning_count += 1
                        elif resource["download"]["status"] == "failed":
                            failure_count += 1
                            message = str(
                                resource["download"].get("error")
                                or "download returned failed status"
                            )
                            normalized["errors"].append(f"download: {message}")
                            append_jsonl(
                                errors_path,
                                {
                                    "time": utc_now(),
                                    "study_id": study_id,
                                    "stage": "download",
                                    "error": message,
                                },
                            )
                    except Exception as exc:
                        failure_count += 1
                        resource["download"] = {
                            "status": "failed",
                            "error": f"{type(exc).__name__}: {exc}",
                            "failed_at": utc_now(),
                        }
                        normalized["errors"].append(
                            f"download: {type(exc).__name__}: {exc}"
                        )
                        append_jsonl(
                            errors_path,
                            {
                                "time": utc_now(),
                                "study_id": study_id,
                                "stage": "download",
                                "error": str(exc),
                            },
                        )
                    atomic_write_json(record_path, normalized)

            records.append(normalized)
            processed_ids.append(study_id)
            if (
                index == 1
                or index % CATALOG_FLUSH_EVERY == 0
                or index == len(source_records)
            ):
                atomic_write_json(
                    catalog_path,
                    build_catalog_document(records, reported_total, selection),
                )
            checkpoint.update(
                {
                    "updated_at": utc_now(),
                    "completed_count": len(processed_ids),
                    "processed_study_ids": processed_ids,
                    "last_study_id": study_id,
                    "failure_count": failure_count,
                    "warning_count": warning_count,
                }
            )
            atomic_write_json(checkpoint_path, checkpoint)

        checkpoint.update({"updated_at": utc_now(), "finished_at": utc_now()})
        atomic_write_json(checkpoint_path, checkpoint)
    finally:
        client.close()
        if browser_session is not None:
            browser_session.close()

    print(f"Wrote {len(records)} normalized record(s) to {catalog_path}.", flush=True)
    if warning_count:
        print(
            f"Completed with {warning_count} access warning(s); see catalog.json.",
            file=sys.stderr,
        )
    if failure_count:
        print(
            f"Completed with {failure_count} error(s); see {errors_path}.",
            file=sys.stderr,
        )
        return 2
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        print("Interrupted; per-record files and checkpoint were retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Fatal error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
