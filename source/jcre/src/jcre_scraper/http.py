from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .utils import parse_int, sha256_file

REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class HttpError(RuntimeError):
    pass


class UnsafeUrlError(HttpError):
    pass


class FileTooLargeError(HttpError):
    pass


class InsufficientDiskSpaceError(HttpError):
    pass


@dataclass
class OpenedResponse:
    response: requests.Response
    final_url: str
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class DownloadResult:
    status: str
    local_path: Path
    final_url: str
    redirect_chain: list[dict[str, Any]]
    downloaded_bytes: int
    sha256: str
    content_type: str | None = None
    content_disposition: str | None = None


def _normalized_host(host: str | None) -> str:
    return (host or "").rstrip(".").lower()


def validate_http_url(url: str, allowed_hosts: Iterable[str] | None = None) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError(f"Unsupported URL scheme: {parsed.scheme or '(missing)'}")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs containing credentials are not allowed")
    host = _normalized_host(parsed.hostname)
    if not host:
        raise UnsafeUrlError("URL does not contain a hostname")
    if allowed_hosts is not None:
        normalized = {_normalized_host(item) for item in allowed_hosts}
        if host not in normalized:
            raise UnsafeUrlError(f"Host is not allowlisted: {host}")


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        delay_seconds: float = 1.0,
        timeout_seconds: float = 60.0,
        retries: int = 4,
        max_redirects: int = 10,
    ) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout_seconds = max(1.0, timeout_seconds)
        self.max_redirects = max(0, max_redirects)
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        retry = Retry(
            total=max(0, retries),
            connect=max(0, retries),
            read=max(0, retries),
            status=max(0, retries),
            backoff_factor=0.75,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def _throttle(self) -> None:
        if self.delay_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        stream: bool,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> requests.Response:
        self._throttle()
        try:
            response = self.session.request(
                method,
                url,
                allow_redirects=False,
                stream=stream,
                timeout=(min(15.0, self.timeout_seconds), self.timeout_seconds),
                headers=headers,
                params=params,
            )
        except requests.RequestException as exc:
            raise HttpError(f"{method} {url} failed: {exc}") from exc
        finally:
            self._last_request_at = time.monotonic()
        return response

    def open(
        self,
        method: str,
        url: str,
        *,
        allowed_hosts: Iterable[str] | None,
        allowed_statuses: Iterable[int] = (),
        stream: bool = False,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> OpenedResponse:
        current_url = url
        current_method = method.upper()
        redirect_chain: list[dict[str, Any]] = []
        current_params = params

        for redirect_index in range(self.max_redirects + 1):
            validate_http_url(current_url, allowed_hosts)
            response = self._request_once(
                current_method,
                current_url,
                stream=stream,
                headers=headers,
                params=current_params,
            )
            request_url = response.url
            current_params = None
            if response.status_code not in REDIRECT_STATUSES:
                accepted = set(allowed_statuses)
                if response.status_code >= 400 and response.status_code not in accepted:
                    snippet = ""
                    if not stream:
                        try:
                            snippet = response.text[:500]
                        except Exception:
                            snippet = ""
                    response.close()
                    suffix = f" Response: {snippet}" if snippet else ""
                    raise HttpError(f"{current_method} {request_url} returned HTTP {response.status_code}.{suffix}")
                return OpenedResponse(response=response, final_url=request_url, redirect_chain=redirect_chain)

            location = response.headers.get("Location")
            status = response.status_code
            response.close()
            if not location:
                raise HttpError(f"Redirect from {request_url} did not include a Location header")
            next_url = urljoin(request_url, location)
            validate_http_url(next_url, allowed_hosts)
            redirect_chain.append(
                {
                    "status": status,
                    "from_url": request_url,
                    "location": location,
                    "to_url": next_url,
                }
            )
            if status == 303 and current_method != "HEAD":
                current_method = "GET"
            elif status in {301, 302} and current_method not in {"GET", "HEAD"}:
                current_method = "GET"
            current_url = next_url

            if redirect_index == self.max_redirects:
                raise HttpError(f"Too many redirects while requesting {url}")

        raise HttpError(f"Could not complete request for {url}")

    def get_text(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str] | None,
        params: dict[str, Any] | None = None,
    ) -> tuple[str, OpenedResponse]:
        opened = self.open("GET", url, allowed_hosts=allowed_hosts, stream=False, params=params)
        response = opened.response
        try:
            response.encoding = response.encoding or response.apparent_encoding or "utf-8"
            return response.text, opened
        finally:
            response.close()

    def get_json(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str] | None,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], OpenedResponse]:
        opened = self.open("GET", url, allowed_hosts=allowed_hosts, stream=False, params=params)
        response = opened.response
        try:
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError) as exc:
                snippet = response.text[:500]
                raise HttpError(f"Expected JSON from {opened.final_url}, received: {snippet}") from exc
            if not isinstance(payload, dict):
                raise HttpError(f"Expected a JSON object from {opened.final_url}")
            return payload, opened
        finally:
            response.close()

    def resolve_page(
        self,
        url: str,
        *,
        allowed_hosts: Iterable[str],
        max_html_bytes: int = 5 * 1024 * 1024,
    ) -> tuple[dict[str, Any], bytes | None]:
        opened = self.open("GET", url, allowed_hosts=allowed_hosts, stream=True)
        response = opened.response
        try:
            content_type = (response.headers.get("Content-Type") or "").lower()
            metadata = {
                "resolved_url": opened.final_url,
                "redirect_chain": opened.redirect_chain,
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type"),
                "content_length": parse_int(response.headers.get("Content-Length")),
            }
            body: bytes | None = None
            if "text/html" in content_type or "application/xhtml+xml" in content_type:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_html_bytes:
                        raise HttpError(f"Landing page exceeds {max_html_bytes} bytes: {opened.final_url}")
                    chunks.append(chunk)
                body = b"".join(chunks)
            return metadata, body
        finally:
            response.close()

    def download_to(
        self,
        url: str,
        target: Path,
        *,
        allowed_hosts: Iterable[str],
        resume: bool,
        max_bytes: int | None,
        min_free_bytes: int,
    ) -> DownloadResult:
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")

        if target.exists():
            size = target.stat().st_size
            return DownloadResult(
                status="already_present",
                local_path=target,
                final_url=url,
                redirect_chain=[],
                downloaded_bytes=size,
                sha256=sha256_file(target),
            )

        if partial.exists() and not resume:
            partial.unlink()

        start = partial.stat().st_size if resume and partial.exists() else 0
        headers = {"Accept-Encoding": "identity"}
        if start > 0:
            headers["Range"] = f"bytes={start}-"
        opened = self.open(
            "GET",
            url,
            allowed_hosts=allowed_hosts,
            allowed_statuses={416} if start > 0 else (),
            stream=True,
            headers=headers,
        )
        response = opened.response

        if start > 0 and response.status_code == 416:
            content_range = response.headers.get("Content-Range") or ""
            remote_total = parse_int(content_range.rsplit("/", 1)[-1]) if "/" in content_range else None
            response.close()
            if remote_total is not None and remote_total == start:
                os.replace(partial, target)
                return DownloadResult(
                    status="resumed",
                    local_path=target,
                    final_url=opened.final_url,
                    redirect_chain=opened.redirect_chain,
                    downloaded_bytes=target.stat().st_size,
                    sha256=sha256_file(target),
                )
            partial.unlink(missing_ok=True)
            start = 0
            opened = self.open(
                "GET",
                url,
                allowed_hosts=allowed_hosts,
                stream=True,
                headers={"Accept-Encoding": "identity"},
            )
            response = opened.response
        elif start > 0 and response.status_code != 206:
            response.close()
            partial.unlink(missing_ok=True)
            start = 0
            opened = self.open(
                "GET",
                url,
                allowed_hosts=allowed_hosts,
                stream=True,
                headers={"Accept-Encoding": "identity"},
            )
            response = opened.response

        try:
            content_length = parse_int(response.headers.get("Content-Length"))
            expected_total = None
            content_range = response.headers.get("Content-Range")
            if content_range and "/" in content_range:
                total_text = content_range.rsplit("/", 1)[-1]
                expected_total = parse_int(total_text) if total_text != "*" else None
            elif content_length is not None:
                expected_total = start + content_length if response.status_code == 206 else content_length

            if max_bytes is not None and expected_total is not None and expected_total > max_bytes:
                raise FileTooLargeError(f"Remote file is {expected_total} bytes; limit is {max_bytes} bytes")

            free = shutil.disk_usage(target.parent).free
            required = min_free_bytes
            if expected_total is not None:
                remaining = max(0, expected_total - start)
                required += remaining
            if free < required:
                raise InsufficientDiskSpaceError(
                    f"Need at least {required} free bytes before writing {target}; only {free} are available"
                )

            mode = "ab" if start > 0 and response.status_code == 206 else "wb"
            written = start if mode == "ab" else 0
            next_disk_check = written + 64 * 1024 * 1024
            with partial.open(mode) as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise FileTooLargeError(f"Download exceeded the {max_bytes}-byte limit")
                    if written >= next_disk_check:
                        free = shutil.disk_usage(target.parent).free
                        if free < min_free_bytes:
                            raise InsufficientDiskSpaceError(
                                f"Free space fell below the required reserve of {min_free_bytes} bytes"
                            )
                        next_disk_check = written + 64 * 1024 * 1024
                handle.flush()
                os.fsync(handle.fileno())

            os.replace(partial, target)
            return DownloadResult(
                status="downloaded",
                local_path=target,
                final_url=opened.final_url,
                redirect_chain=opened.redirect_chain,
                downloaded_bytes=target.stat().st_size,
                sha256=sha256_file(target),
                content_type=response.headers.get("Content-Type"),
                content_disposition=response.headers.get("Content-Disposition"),
            )
        except FileTooLargeError:
            partial.unlink(missing_ok=True)
            raise
        except InsufficientDiskSpaceError:
            # Preserve a valid .part file so --resume can continue after space is freed.
            raise
        finally:
            response.close()
