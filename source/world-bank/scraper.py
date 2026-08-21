#!/usr/bin/env python3
"""Inventory and download World Bank reproducibility packages.

Record discovery and metadata come from the public JSON API. Download links
come from each record's related-materials page. The complete inventory summary
and normalized records live in catalog.json; each record is also saved as
data/<reference_id>/record.json. The program deliberately does not extract
archives, execute downloaded code, or follow external resources.
"""

from __future__ import annotations

import argparse
import hashlib
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
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests  # pyright: ignore[reportMissingImports]  # type: ignore[import-not-found]
from bs4 import BeautifulSoup  # pyright: ignore[reportMissingImports]  # type: ignore[import-not-found]


BASE_URL = "https://reproducibility.worldbank.org"
ALLOWED_HOST = "reproducibility.worldbank.org"
LIST_ENDPOINT = f"{BASE_URL}/api/catalog"
PAGE_SIZE = 100
USER_AGENT = (
    "Mozilla/5.0 (compatible; WorldBankReproScraper/1.0; "
    "+https://reproducibility.worldbank.org/)"
)
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
DOWNLOAD_PATH_RE = re.compile(
    r"^/+catalog/(?P<catalog_id>[0-9]+)/download/(?P<resource_id>[0-9]+)"
    r"(?:/(?P<filename>[^?#]*))?/?$"
)
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9._-]+")
DISPLAY_SIZE_RE = re.compile(
    r"\[\s*[^,\]]+\s*,\s*(?P<number>[0-9][0-9,.]*)\s*"
    r"(?P<unit>B|KB|MB|GB|TB)\s*\]",
    flags=re.IGNORECASE,
)
SMOKE_TEST_REFERENCE_IDS = (
    "RR_WLD_2025_394",  # Has an associated paper URI.
    "RR_WLD_2024_111",  # Has no associated paper URI/DOI.
)


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
    if not cleaned:
        cleaned = fallback
    return cleaned[:180]


def safe_filename(value: str, resource_id: str, extension: str = "") -> str:
    # Both slash styles are removed so a server-controlled name cannot escape
    # the resource directory on either Unix or Windows.
    value = unquote(value or "").replace("\\", "/").split("/")[-1]
    value = "".join(ch for ch in value if ch >= " " and ch != "\x7f").strip()
    value = value.strip(". ")
    if not value:
        suffix = f".{extension.lstrip('.')}" if extension else ""
        value = f"resource_{resource_id}{suffix}"
    value = SAFE_COMPONENT_RE.sub("_", value)
    if len(value) > 180:
        suffix = Path(value).suffix[:20]
        value = value[: 180 - len(suffix)] + suffix
    return value or f"resource_{resource_id}"


def ensure_direct_child_directory(parent: Path, child: Path) -> None:
    """Create a direct child directory without following a child symlink."""

    if child.parent != parent:
        raise ValueError(f"directory is not a direct child of {parent}: {child}")
    if child.is_symlink():
        raise ValueError(f"refusing symlinked output directory: {child}")
    child.mkdir(exist_ok=True)
    if child.resolve().parent != parent.resolve():
        raise ValueError(f"output directory escaped its parent: {child}")


class RateLimitedClient:
    """A sequential requester that never leaves the repository host."""

    def __init__(
        self,
        delay: float,
        timeout: tuple[float, float] = (15.0, 120.0),
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
                "Accept": "application/json,text/html,application/octet-stream;q=0.9,*/*;q=0.8",
            }
        )

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def validate_url(url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            raise ValueError(f"refusing non-HTTPS URL: {url}")
        if parts.hostname is None or parts.hostname.lower() != ALLOWED_HOST:
            raise ValueError(f"refusing URL outside {ALLOWED_HOST}: {url}")
        if parts.username or parts.password or (parts.port not in (None, 443)):
            raise ValueError(f"refusing URL with credentials or a nonstandard port: {url}")
        return url

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
        return min(60.0, 2.0**attempt)

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        stream: bool = False,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        current_url = self.validate_url(url)
        redirects = 0
        attempt = 0
        while True:
            self._rate_limit()
            try:
                response = self.session.get(
                    current_url,
                    params=params,
                    stream=stream,
                    timeout=self.timeout,
                    allow_redirects=False,
                    headers=headers,
                )
            except requests.RequestException:
                if attempt >= self.max_retries:
                    raise
                time.sleep(min(60.0, 2.0**attempt))
                attempt += 1
                continue

            if response.status_code in RETRYABLE_STATUSES:
                if attempt >= self.max_retries:
                    response.raise_for_status()
                wait_for = self._retry_after(response, attempt)
                response.close()
                time.sleep(wait_for)
                attempt += 1
                continue

            if 300 <= response.status_code < 400:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise requests.HTTPError("redirect response had no Location header")
                redirects += 1
                if redirects > 5:
                    raise requests.TooManyRedirects(current_url)
                # This is the key safety boundary: even a hosted download may
                # not redirect the scraper to an external service.
                current_url = self.validate_url(urljoin(current_url, location))
                # Query params belong to the original request only. Preserve
                # them across retries, but never append them to a redirect.
                params = None
                attempt = 0
                continue

            response.raise_for_status()
            return response

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        with self.get(url, params=params) as response:
            try:
                return response.json()
            except requests.JSONDecodeError as exc:
                raise ValueError(f"expected JSON from {response.url}") from exc

    def get_text(self, url: str) -> str:
        with self.get(url) as response:
            response.encoding = response.encoding or "utf-8"
            return response.text


def discover_records(client: RateLimitedClient, max_records: int | None) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    page = 1
    reported_total: int | None = None

    while reported_total is None or len(seen) < reported_total:
        payload = client.get_json(
            LIST_ENDPOINT,
            params={
                "page": page,
                "ps": PAGE_SIZE,
                "sort_by": "title",
                "sort_order": "asc",
            },
        )
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("rows") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"catalog page {page} did not contain result.rows")
        if reported_total is None:
            raw_total = result.get("total", result.get("found", len(rows)))
            reported_total = int(raw_total)

        if not rows:
            if len(seen) < reported_total:
                raise ValueError(
                    f"catalog ended after {len(seen)} unique records, expected {reported_total}"
                )
            break

        count_before_page = len(seen)
        for row in rows:
            if not isinstance(row, dict):
                continue
            reference_id = str(row.get("idno") or "").strip()
            if not reference_id:
                continue
            if reference_id not in seen:
                seen.add(reference_id)
                records.append(row)
                if max_records is not None and len(records) >= max_records:
                    return records, reported_total
        if len(seen) == count_before_page:
            raise ValueError(
                f"catalog page {page} repeated records without adding a new idno"
            )
        page += 1
        maximum_pages = (reported_total + PAGE_SIZE - 1) // PAGE_SIZE + 5
        if page > maximum_pages and len(seen) < reported_total:
            raise ValueError(
                f"catalog pagination exceeded {maximum_pages} pages before "
                f"reaching the reported total of {reported_total}"
            )

    return records, reported_total or len(records)


def cached_json(
    client: RateLimitedClient, url: str, path: Path, refresh: bool
) -> Any:
    if path.exists() and not refresh:
        try:
            return load_json(path)
        except (OSError, json.JSONDecodeError):
            pass
    payload = client.get_json(url)
    atomic_write_json(path, payload)
    return payload


def cached_html(
    client: RateLimitedClient, catalog_id: str, path: Path, refresh: bool
) -> tuple[str, str]:
    related_url = f"{BASE_URL}/catalog/{catalog_id}/related-materials"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8"), related_url
    try:
        html = client.get_text(related_url)
        source_url = related_url
    except requests.HTTPError as exc:
        if exc.response is None or exc.response.status_code != 404:
            raise
        source_url = f"{BASE_URL}/catalog/{catalog_id}"
        html = client.get_text(source_url)
    atomic_write_text(path, html)
    return html, source_url


def _anchor_filename(anchor: Any, path_filename: str, resource_id: str) -> str:
    explicit = str(anchor.get("data-filename") or "").strip()
    extension = str(anchor.get("data-extension") or "").strip().lower()
    title = str(anchor.get("title") or "").strip()
    candidate = explicit or path_filename or title
    return safe_filename(candidate, resource_id, extension)


def displayed_size(anchor: Any) -> tuple[str, int | None]:
    """Read the rounded, human-readable file size in a download button."""

    text = anchor.get_text(" ", strip=True)
    match = DISPLAY_SIZE_RE.search(text)
    if match is None:
        return "", None
    raw_number = match.group("number").replace(",", "")
    unit = match.group("unit").upper()
    multipliers = {
        "B": 1,
        "KB": 1024,
        "MB": 1024**2,
        "GB": 1024**3,
        "TB": 1024**4,
    }
    try:
        estimate = round(float(raw_number) * multipliers[unit])
    except (KeyError, ValueError, OverflowError):
        return "", None
    return f"{match.group('number')} {unit}", estimate


def discover_resources(
    html: str, catalog_id: str, source_url: str
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    hosted: dict[str, dict[str, Any]] = {}
    external: dict[str, dict[str, Any]] = {}

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(source_url, href)
        parts = urlsplit(absolute)
        marked_external = str(anchor.get("data-isurl") or "").strip() == "1"
        is_main_package = (
            str(anchor.get("title") or "").strip() == "Get Reproducibility Package"
        )
        match = DOWNLOAD_PATH_RE.match(parts.path)
        is_hosted_download = (
            not marked_external
            and parts.scheme.lower() in {"http", "https"}
            and (parts.hostname or "").lower() == ALLOWED_HOST
            and match is not None
            and match.group("catalog_id") == catalog_id
        )

        if is_hosted_download and match is not None:
            resource_id = match.group("resource_id")
            path_filename = unquote(match.group("filename") or "")
            filename = _anchor_filename(anchor, path_filename, resource_id)
            extension = str(anchor.get("data-extension") or Path(filename).suffix.lstrip("."))
            size_text, size_estimate = displayed_size(anchor)
            canonical_url = f"{BASE_URL}/catalog/{catalog_id}/download/{resource_id}"
            candidate = {
                "catalog_id": catalog_id,
                "resource_id": resource_id,
                "kind": "hosted",
                "filename": filename,
                "extension": extension.lower(),
                "data_type": str(anchor.get("data-dctype") or "").strip(),
                "title": str(anchor.get("title") or anchor.get_text(" ", strip=True)).strip(),
                "url": canonical_url,
                "source_url": absolute,
                "reported_size": size_text,
                "reported_size_bytes_estimate": size_estimate,
                "download": {"status": "not_requested"},
            }
            old = hosted.get(resource_id)
            if old is None:
                hosted[resource_id] = candidate
            else:
                # Detailed resource anchors have data-filename; prefer them to
                # the header's generic "Get package" anchor.
                if anchor.get("data-filename"):
                    for key in (
                        "filename",
                        "extension",
                        "data_type",
                        "title",
                        "reported_size",
                        "reported_size_bytes_estimate",
                    ):
                        if candidate[key]:
                            old[key] = candidate[key]
                if len(candidate["source_url"]) > len(old["source_url"]):
                    old["source_url"] = candidate["source_url"]
            continue

        # NADA marks remote resources with data-isurl=1.  Some older records
        # incorrectly use data-isurl=0 for an external URL, so any data-isurl
        # anchor outside the allowed host is also inventoried but never fetched.
        has_isurl_attribute = anchor.has_attr("data-isurl")
        if parts.scheme.lower() in {"http", "https"} and (
            marked_external
            or (
                (has_isurl_attribute or is_main_package)
                and (parts.hostname or "").lower() != ALLOWED_HOST
            )
        ):
            key = hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:16]
            external.setdefault(
                absolute,
                {
                    "resource_id": f"external-{key}",
                    "kind": "external",
                    "filename": safe_filename(
                        str(anchor.get("data-filename") or anchor.get("title") or ""),
                        key,
                        str(anchor.get("data-extension") or ""),
                    ),
                    "extension": str(anchor.get("data-extension") or "").lower(),
                    "data_type": str(anchor.get("data-dctype") or "").strip(),
                    "title": str(anchor.get("title") or anchor.get_text(" ", strip=True)).strip(),
                    "url": absolute,
                    "download": {"status": "external_not_fetched"},
                },
            )

    return {
        "catalog_id": catalog_id,
        "source_url": source_url,
        "discovered_at": utc_now(),
        "hosted_resources": sorted(hosted.values(), key=lambda item: int(item["resource_id"])),
        "external_resources": sorted(external.values(), key=lambda item: item["url"]),
    }


def preserve_download_state(manifest: dict[str, Any], previous: Any) -> bool:
    if not isinstance(previous, dict):
        return False
    old_resources = previous.get("hosted_resources")
    if not isinstance(old_resources, list):
        # New output stores hosted and external resources together inside the
        # per-record record.json. Supporting both shapes also lets an existing
        # crawl migrate without redownloading already verified files.
        resources = previous.get("resources")
        if not isinstance(resources, list):
            return False
        old_resources = [
            item
            for item in resources
            if isinstance(item, dict) and item.get("kind") == "hosted"
        ]
    old_by_id = {
        str(item.get("resource_id")): item
        for item in old_resources
        if isinstance(item, dict) and item.get("resource_id") is not None
    }
    preserved = False
    for resource in manifest["hosted_resources"]:
        old = old_by_id.get(str(resource["resource_id"]))
        if isinstance(old, dict) and isinstance(old.get("download"), dict):
            resource["download"] = old["download"]
            preserved = True
    return preserved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verified_download_path(resource: dict[str, Any], record_dir: Path) -> Path | None:
    state = resource.get("download")
    if not isinstance(state, dict) or state.get("status") != "complete":
        return None
    relative = state.get("relative_path")
    expected_hash = state.get("sha256")
    expected_size = state.get("bytes")
    if not isinstance(relative, str) or not isinstance(expected_hash, str):
        return None
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    path = record_dir / relative_path
    expected_parent = (record_dir / "files" / str(resource["resource_id"])).resolve()
    try:
        if path.resolve().parent != expected_parent:
            return None
    except OSError:
        return None
    if not path.is_file():
        return None
    if isinstance(expected_size, int) and path.stat().st_size != expected_size:
        return None
    if sha256_file(path) != expected_hash:
        return None
    return path


def sanity_check(path: Path, extension: str) -> dict[str, Any]:
    extension = extension.lower().lstrip(".")
    size = path.stat().st_size
    if size <= 0:
        return {"ok": False, "kind": extension or "file", "reason": "empty file"}
    if extension == "zip":
        try:
            if not zipfile.is_zipfile(path):
                return {"ok": False, "kind": "zip", "reason": "invalid ZIP signature"}
            with zipfile.ZipFile(path, "r") as archive:
                members = len(archive.infolist())
            return {
                "ok": True,
                "kind": "zip",
                "members": members,
                "check": "central directory readable",
            }
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            return {"ok": False, "kind": "zip", "reason": str(exc)}
    if extension == "pdf":
        with path.open("rb") as handle:
            header = handle.read(1024)
            handle.seek(max(0, size - 8192))
            trailer = handle.read()
        if b"%PDF-" not in header:
            return {"ok": False, "kind": "pdf", "reason": "missing PDF header"}
        if b"%%EOF" not in trailer:
            return {"ok": False, "kind": "pdf", "reason": "missing PDF EOF marker"}
        return {"ok": True, "kind": "pdf", "check": "header and EOF marker present"}
    if extension == "rar":
        with path.open("rb") as handle:
            header = handle.read(8)
        if not header.startswith(b"Rar!\x1a\x07"):
            return {"ok": False, "kind": "rar", "reason": "invalid RAR signature"}
        return {"ok": True, "kind": "rar", "check": "RAR signature present"}
    return {"ok": True, "kind": extension or "file", "check": "non-empty file"}


def download_resource(
    client: RateLimitedClient,
    resource: dict[str, Any],
    record_dir: Path,
    data_dir: Path,
    max_bytes: int | None,
    min_free_bytes: int,
) -> dict[str, Any]:
    resource_id = str(resource["resource_id"])
    catalog_id = str(resource.get("catalog_id") or "")
    if not resource_id.isdigit() or not catalog_id.isdigit():
        raise ValueError("hosted resource must have numeric catalog and resource IDs")

    url = RateLimitedClient.validate_url(str(resource["url"]))
    # A second, path-specific check prevents a malformed manifest from turning
    # this routine into a general downloader for other pages on the host.
    match = DOWNLOAD_PATH_RE.match(urlsplit(url).path)
    if (
        match is None
        or match.group("resource_id") != resource_id
        or match.group("catalog_id") != catalog_id
    ):
        raise ValueError(f"refusing mismatched resource download URL: {url}")

    if record_dir.is_symlink() or record_dir.resolve().parent != data_dir.resolve():
        raise ValueError(f"refusing record directory outside data root: {record_dir}")
    files_root = record_dir / "files"
    ensure_direct_child_directory(record_dir, files_root)
    resource_dir = files_root / resource_id
    ensure_direct_child_directory(files_root, resource_dir)

    existing = verified_download_path(resource, record_dir)
    if existing is not None:
        state = dict(resource["download"])
        state["status"] = "complete"
        state["verified_at"] = utc_now()
        state["skipped_existing"] = True
        return state

    filename = safe_filename(
        str(resource.get("filename") or ""),
        resource_id,
        str(resource.get("extension") or ""),
    )
    final_path = resource_dir / filename
    part_path = resource_dir / f"{filename}.part"
    if part_path.is_symlink() or part_path.exists():
        part_path.unlink()

    digest = hashlib.sha256()
    written = 0
    try:
        with client.get(
            url,
            stream=True,
            headers={"Accept-Encoding": "identity", "Accept": "application/octet-stream,*/*;q=0.8"},
        ) as response:
            raw_length = response.headers.get("Content-Length")
            expected_length = int(raw_length) if raw_length and raw_length.isdigit() else None
            if max_bytes is not None and expected_length is not None and expected_length > max_bytes:
                return {
                    "status": "skipped_too_large",
                    "limit_bytes": max_bytes,
                    "reported_bytes": expected_length,
                    "checked_at": utc_now(),
                }
            free_bytes = shutil.disk_usage(resource_dir).free
            if (
                expected_length is not None
                and free_bytes - expected_length < min_free_bytes
            ):
                return {
                    "status": "skipped_insufficient_space",
                    "reported_bytes": expected_length,
                    "free_bytes": free_bytes,
                    "reserved_free_bytes": min_free_bytes,
                    "checked_at": utc_now(),
                }
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            with part_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise ValueError(f"download exceeded --max-file-mb limit ({max_bytes} bytes)")
                    if shutil.disk_usage(resource_dir).free < min_free_bytes:
                        raise OSError(
                            "download stopped to preserve the --min-free-gb reserve"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())

        if expected_length is not None and written != expected_length:
            raise ValueError(f"incomplete download: received {written}, expected {expected_length} bytes")
        extension = str(resource.get("extension") or Path(filename).suffix).lstrip(".")
        if content_type == "text/html" and extension.lower() not in {"html", "htm"}:
            raise ValueError("download endpoint returned HTML instead of the requested file")
        check = sanity_check(part_path, extension)
        if not check.get("ok"):
            raise ValueError(f"downloaded file failed sanity check: {check.get('reason')}")
        os.replace(part_path, final_path)
        return {
            "status": "complete",
            "relative_path": final_path.relative_to(record_dir).as_posix(),
            "bytes": written,
            "sha256": digest.hexdigest(),
            "content_type": content_type,
            "sanity": check,
            "completed_at": utc_now(),
            "skipped_existing": False,
        }
    except Exception:
        if part_path.exists():
            part_path.unlink()
        raise


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def normalize_doi(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        if (parts.hostname or "").lower() not in {"doi.org", "dx.doi.org"}:
            # Some repository records put an ordinary publication URL in the
            # DOI field. Preserve that usable URL instead of corrupting it.
            return text
        text = parts.path.lstrip("/")
    else:
        text = re.sub(r"^doi\s*:\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^(?:dx\.)?doi\.org/", "", text, flags=re.IGNORECASE)
    if not re.fullmatch(r"10\.\d{4,9}/\S+", text, flags=re.IGNORECASE):
        return ""
    return "https://doi.org/" + quote(text, safe="/:;()-._~")


def normalize_uri(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "na", "none", "null"}:
        return ""
    if text.startswith("//"):
        return "https:" + text
    parts = urlsplit(text)
    if parts.scheme.lower() in {"http", "https"} and parts.netloc:
        return text
    if " " not in text and "." in text:
        return "https://" + text.lstrip("/")
    return ""


def normalized_paper_outputs(project: dict[str, Any]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw in _as_list(project.get("output")):
        if not isinstance(raw, dict):
            continue
        uri = normalize_uri(raw.get("uri"))
        doi_url = normalize_doi(raw.get("doi"))
        paper_url = uri or doi_url
        normalized.append(
            {
                "title": str(raw.get("title") or "").strip(),
                "type": str(raw.get("type") or "").strip(),
                "authors": str(raw.get("authors") or "").strip(),
                "description": str(raw.get("description") or "").strip(),
                "uri": uri,
                "doi": doi_url,
                "paper_url": paper_url,
                "paper_url_source": "uri" if uri else ("doi" if doi_url else ""),
                "paper_link_status": "present" if paper_url else "absent",
            }
        )
    return normalized


def select_primary_paper(outputs: list[dict[str, str]]) -> dict[str, str] | None:
    """Prefer any declared paper URI, then any DOI, then the first output."""

    return (
        next((item for item in outputs if item["uri"]), None)
        or next((item for item in outputs if item["doi"]), None)
        or (outputs[0] if outputs else None)
    )


def normalize_record(
    listing: dict[str, Any],
    detail: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    fetch_errors: list[str],
) -> dict[str, Any]:
    dataset = detail.get("dataset") if isinstance(detail, dict) else None
    if not isinstance(dataset, dict):
        dataset = {}
    metadata = dataset.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    project = metadata.get("project_desc")
    if not isinstance(project, dict):
        project = {}
    outputs = normalized_paper_outputs(project)
    primary = select_primary_paper(outputs)
    linked_primary = primary if primary and primary["paper_url"] else None
    catalog_id = str(dataset.get("id") or listing.get("id") or "").strip()
    reference_id = str(dataset.get("idno") or listing.get("idno") or "").strip()

    resources: list[dict[str, Any]] = []
    if isinstance(manifest, dict):
        for item in manifest.get("hosted_resources", []):
            if isinstance(item, dict):
                resources.append(
                    {
                        "catalog_id": item.get("catalog_id"),
                        "resource_id": item.get("resource_id"),
                        "kind": "hosted",
                        "filename": item.get("filename", ""),
                        "extension": item.get("extension", ""),
                        "data_type": item.get("data_type", ""),
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "source_url": item.get("source_url", ""),
                        "reported_size": item.get("reported_size", ""),
                        "reported_size_bytes_estimate": item.get(
                            "reported_size_bytes_estimate"
                        ),
                        "download": item.get("download", {"status": "not_requested"}),
                    }
                )
        for item in manifest.get("external_resources", []):
            if isinstance(item, dict):
                resources.append(
                    {
                        "resource_id": item.get("resource_id"),
                        "kind": "external",
                        "filename": item.get("filename", ""),
                        "extension": item.get("extension", ""),
                        "data_type": item.get("data_type", ""),
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "download": {"status": "external_not_fetched"},
                    }
                )

    status = "fetch_error" if detail is None else ("present" if linked_primary else "absent")
    return {
        "catalog_id": catalog_id,
        "reference_id": reference_id,
        "catalog_title": str(dataset.get("title") or listing.get("title") or "").strip(),
        "catalog_url": f"{BASE_URL}/catalog/{catalog_id}" if catalog_id else "",
        "collection": str(listing.get("repo_title") or "").strip(),
        "year": str(dataset.get("year_start") or listing.get("year_start") or "").strip(),
        "dates": {
            "published": str(dataset.get("year_start") or listing.get("year_start") or "").strip(),
            "updated": "",
            "retrieved": utc_now(),
        },
        "authors": str(dataset.get("authoring_entity") or listing.get("authoring_entity") or "").strip(),
        "package_doi": normalize_doi(dataset.get("doi") or listing.get("doi")),
        "paper": {
            "title": primary["title"] if primary else "",
            "type": primary["type"] if primary else "",
            "authors": primary["authors"] if primary else "",
            "description": primary["description"] if primary else "",
            "url": primary["paper_url"] if primary else "",
            "url_source": primary["paper_url_source"] if primary else "",
            "link_status": status,
            "outputs": outputs,
        },
        "resources": resources,
        "errors": list(fetch_errors),
    }


def build_inventory_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    hosted = [
        resource
        for record in records
        for resource in record.get("resources", [])
        if isinstance(resource, dict) and resource.get("kind") == "hosted"
    ]
    external = [
        resource
        for record in records
        for resource in record.get("resources", [])
        if isinstance(resource, dict) and resource.get("kind") == "external"
    ]
    known_sizes = [
        value
        for resource in hosted
        if isinstance((value := resource.get("reported_size_bytes_estimate")), int)
    ]
    download_states: dict[str, int] = {}
    exact_downloaded_bytes = 0
    for resource in hosted:
        download = resource.get("download")
        status = (
            str(download.get("status") or "unknown")
            if isinstance(download, dict)
            else "unknown"
        )
        download_states[status] = download_states.get(status, 0) + 1
        if isinstance(download, dict) and download.get("status") == "complete":
            exact_downloaded_bytes += int(download.get("bytes") or 0)
    total_estimate = sum(known_sizes)
    paper_statuses = {"present": 0, "absent": 0, "fetch_error": 0}
    for record in records:
        paper = record.get("paper")
        status = paper.get("link_status") if isinstance(paper, dict) else None
        if status in paper_statuses:
            paper_statuses[status] += 1
    return {
        "generated_at": utc_now(),
        "total_records": len(records),
        "paper_links_present": paper_statuses["present"],
        "paper_links_missing": paper_statuses["absent"],
        "paper_link_fetch_errors": paper_statuses["fetch_error"],
        "hosted_resources": len(hosted),
        "external_resources": len(external),
        "hosted_resources_with_reported_size": len(known_sizes),
        "hosted_resources_without_reported_size": len(hosted) - len(known_sizes),
        "estimated_download_bytes": total_estimate,
        "estimated_download_gib": round(total_estimate / 1024**3, 3),
        "downloaded_bytes": exact_downloaded_bytes,
        "download_status_counts": dict(sorted(download_states.items())),
        "size_note": (
            "Reported sizes are rounded values parsed from the repository HTML; "
            "completed download byte counts are exact."
        ),
    }


def build_catalog_document(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the single top-level catalog.json document."""

    return {
        "summary": build_inventory_summary(records),
        "records": records,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory World Bank reproducibility records and optionally download hosted files."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--download-files",
        action="store_true",
        help="download repository-hosted attachments after building the inventory",
    )
    mode.add_argument(
        "--inventory-only",
        action="store_true",
        help="build catalog.json and per-record record.json files only (the default)",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="process two known records (one with and one without a paper link)",
    )
    parser.add_argument("--max-records", type=int, help="process at most this many records")
    parser.add_argument(
        "--max-file-mb",
        type=float,
        help="skip a hosted file if it exceeds this many MiB (unlimited by default)",
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
        "--refresh",
        action="store_true",
        help="refetch API metadata and resource pages; verified files are still retained",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="explicitly resume from saved per-record files (cache reuse is safe by default)",
    )
    parser.add_argument(
        "--output-root",
        "--output-dir",
        "--output",
        type=Path,
        default=None,
        help=(
            "output directory (default: output/, or smoke-output/ "
            "when --smoke-test is used)"
        ),
    )
    args = parser.parse_args(argv)
    if args.max_records is not None and args.max_records <= 0:
        parser.error("--max-records must be greater than zero")
    if args.max_file_mb is not None and (
        not math.isfinite(args.max_file_mb) or args.max_file_mb <= 0
    ):
        parser.error("--max-file-mb must be finite and greater than zero")
    if not math.isfinite(args.min_free_gb) or args.min_free_gb < 0:
        parser.error("--min-free-gb must be finite and nonnegative")
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay must be finite and nonnegative")
    return args


def run(args: argparse.Namespace) -> int:
    if args.output_root is None:
        project_root = Path(__file__).resolve().parent
        root = project_root / ("smoke-output" if args.smoke_test else "output")
    else:
        root = args.output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    state_dir = root / "state"
    errors_path = root / "logs" / "errors.jsonl"
    catalog_path = root / "catalog.json"
    checkpoint_path = state_dir / "checkpoint.json"
    for directory in (data_dir, state_dir, errors_path.parent):
        ensure_direct_child_directory(root, directory)

    client = RateLimitedClient(args.delay)
    normalized_records: list[dict[str, Any]] = []
    processed_ids: list[str] = []
    failure_count = 0
    started_at = utc_now()
    try:
        print("Discovering catalog records from the public API...", flush=True)
        if args.smoke_test:
            listings = [
                {"idno": reference_id}
                for reference_id in SMOKE_TEST_REFERENCE_IDS
            ]
            if args.max_records is not None:
                listings = listings[: args.max_records]
            reported_total = len(SMOKE_TEST_REFERENCE_IDS)
        else:
            listings, reported_total = discover_records(client, args.max_records)
        print(
            f"Discovered {reported_total} total records; processing {len(listings)}.",
            flush=True,
        )
        checkpoint: dict[str, Any] = {
            "started_at": started_at,
            "updated_at": started_at,
            "reported_catalog_total": reported_total,
            "selected_total": len(listings),
            "completed_count": 0,
            "processed_reference_ids": [],
            "mode": "download" if args.download_files else "inventory",
            "failure_count": 0,
        }
        atomic_write_json(checkpoint_path, checkpoint)

        for index, listing in enumerate(listings, start=1):
            reference_id = str(listing.get("idno") or "").strip()
            record_key = safe_component(reference_id, f"record_{index}")
            record_dir = data_dir / record_key
            ensure_direct_child_directory(data_dir, record_dir)
            record_path = record_dir / "record.json"
            previous_record: dict[str, Any] = {}
            if record_path.exists():
                try:
                    loaded_record = load_json(record_path)
                    if isinstance(loaded_record, dict):
                        previous_record = loaded_record
                except (OSError, json.JSONDecodeError):
                    pass
            record_errors: list[str] = []
            detail: dict[str, Any] | None = None
            manifest: dict[str, Any] | None = None
            print(f"[{index}/{len(listings)}] {reference_id}", flush=True)

            try:
                detail_url = f"{LIST_ENDPOINT}/{quote(reference_id, safe='')}"
                raw = cached_json(
                    client,
                    detail_url,
                    record_dir / "api_response.json",
                    args.refresh,
                )
                if not isinstance(raw, dict) or not isinstance(raw.get("dataset"), dict):
                    raise ValueError("detail API response did not contain dataset")
                detail = raw
            except Exception as exc:  # continue so the partial inventory is useful
                failure_count += 1
                message = f"metadata: {type(exc).__name__}: {exc}"
                record_errors.append(message)
                append_jsonl(
                    errors_path,
                    {"time": utc_now(), "reference_id": reference_id, "stage": "metadata", "error": str(exc)},
                )

            dataset = detail.get("dataset") if isinstance(detail, dict) else {}
            catalog_id = str(
                (dataset.get("id") if isinstance(dataset, dict) else None)
                or listing.get("id")
                or ""
            ).strip()
            if catalog_id.isdigit():
                try:
                    html, source_url = cached_html(
                        client,
                        catalog_id,
                        record_dir / "related_materials.html",
                        args.refresh,
                    )
                    manifest = discover_resources(html, catalog_id, source_url)
                    manifest["reference_id"] = reference_id
                    previous_paths = (
                        record_path,
                        # Migration fallback for output created by older
                        # versions of this scraper. No new resources.json is
                        # written by the simplified output format.
                        record_dir / "resources.json",
                    )
                    for previous_path in previous_paths:
                        if not previous_path.exists():
                            continue
                        try:
                            if preserve_download_state(
                                manifest, load_json(previous_path)
                            ):
                                break
                        except (OSError, json.JSONDecodeError):
                            continue

                    # Write once before downloads so an interruption still
                    # leaves a complete normalized record and resource list.
                    atomic_write_json(
                        record_path,
                        normalize_record(listing, detail, manifest, record_errors),
                    )

                    if args.download_files:
                        max_bytes = (
                            int(args.max_file_mb * 1024 * 1024)
                            if args.max_file_mb is not None
                            else None
                        )
                        min_free_bytes = int(args.min_free_gb * 1024**3)
                        for resource in manifest["hosted_resources"]:
                            try:
                                resource["download"] = download_resource(
                                    client,
                                    resource,
                                    record_dir,
                                    data_dir,
                                    max_bytes,
                                    min_free_bytes,
                                )
                            except Exception as exc:
                                failure_count += 1
                                resource["download"] = {
                                    "status": "failed",
                                    "error": f"{type(exc).__name__}: {exc}",
                                    "failed_at": utc_now(),
                                }
                                message = (
                                    f"download {resource['resource_id']}: "
                                    f"{type(exc).__name__}: {exc}"
                                )
                                record_errors.append(message)
                                append_jsonl(
                                    errors_path,
                                    {
                                        "time": utc_now(),
                                        "reference_id": reference_id,
                                        "stage": "download",
                                        "resource_id": resource["resource_id"],
                                        "error": str(exc),
                                    },
                                )
                            # Persist checksum/status progress directly in the
                            # record instead of maintaining resources.json.
                            atomic_write_json(
                                record_path,
                                normalize_record(
                                    listing, detail, manifest, record_errors
                                ),
                            )
                except Exception as exc:
                    failure_count += 1
                    message = f"resources: {type(exc).__name__}: {exc}"
                    record_errors.append(message)
                    append_jsonl(
                        errors_path,
                        {"time": utc_now(), "reference_id": reference_id, "stage": "resources", "error": str(exc)},
                    )
            else:
                failure_count += 1
                message = "resources: record has no numeric catalog ID"
                record_errors.append(message)
                append_jsonl(
                    errors_path,
                    {"time": utc_now(), "reference_id": reference_id, "stage": "resources", "error": message},
                )

            normalized_record = normalize_record(
                listing, detail, manifest, record_errors
            )
            if manifest is None and isinstance(previous_record.get("resources"), list):
                # A temporary refresh/page failure must not discard download
                # checksums and resource state saved by an earlier run.
                normalized_record["resources"] = previous_record["resources"]
            atomic_write_json(record_path, normalized_record)
            normalized_records.append(normalized_record)
            processed_ids.append(reference_id)
            atomic_write_json(
                catalog_path,
                build_catalog_document(normalized_records),
            )
            checkpoint.update(
                {
                    "updated_at": utc_now(),
                    "completed_count": len(processed_ids),
                    "processed_reference_ids": processed_ids,
                    "last_reference_id": reference_id,
                    "failure_count": failure_count,
                }
            )
            atomic_write_json(checkpoint_path, checkpoint)

        checkpoint.update({"updated_at": utc_now(), "finished_at": utc_now()})
        atomic_write_json(checkpoint_path, checkpoint)
    finally:
        client.close()

    print(
        f"Wrote {len(normalized_records)} normalized records to {catalog_path}.",
        flush=True,
    )
    if failure_count:
        print(f"Completed with {failure_count} error(s); see {errors_path}.", file=sys.stderr)
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