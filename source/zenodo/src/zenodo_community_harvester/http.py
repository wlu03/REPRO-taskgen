from __future__ import annotations

import email.utils
import http.client
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping
from urllib.parse import urlsplit

from .util import ensure_within, file_matches, hash_file, parse_checksum, utc_now


JSON_ACCEPT = "application/vnd.inveniordm.v1+json"
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HTTPRequestError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class DownloadSkipped(RuntimeError):
    pass


def _host_allowed(host: str, base_host: str) -> bool:
    host = host.lower().rstrip(".")
    base_host = base_host.lower().rstrip(".")
    if base_host == "zenodo.org" or base_host.endswith(".zenodo.org"):
        return host == "zenodo.org" or host.endswith(".zenodo.org")
    return host == base_host


def validate_remote_url(url: str, base_url: str) -> str:
    candidate = urlsplit(url)
    base = urlsplit(base_url)
    if candidate.username or candidate.password:
        raise ValueError("Remote URL must not contain user information")
    if not candidate.hostname or not base.hostname:
        raise ValueError("Remote URL is missing a host")
    local_test = base.scheme == "http" and base.hostname in {"127.0.0.1", "localhost", "::1"}
    if candidate.scheme != "https" and not (local_test and candidate.scheme == "http"):
        raise ValueError("Remote URL must use HTTPS")
    if not _host_allowed(candidate.hostname, base.hostname):
        raise ValueError(f"Remote URL host is not allowed: {candidate.hostname}")
    if candidate.port != base.port and candidate.port is not None:
        raise ValueError("Remote URL uses an unexpected port")
    if candidate.fragment:
        raise ValueError("Remote URL must not contain a fragment")
    return url


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], str]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(self, req: urllib.request.Request, fp: BinaryIO, code: int, msg: str, headers: Any, newurl: str) -> urllib.request.Request | None:
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_wait(headers: Mapping[str, str], attempt: int) -> float:
    retry_after = headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, min(float(retry_after), 300.0))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(retry_after)
                return max(0.0, min(parsed.timestamp() - time.time(), 300.0))
            except (TypeError, ValueError, OverflowError):
                pass
    reset = headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(0.0, min(float(reset) - time.time(), 300.0))
        except ValueError:
            pass
    return min(2 ** max(attempt - 1, 0) + random.random(), 60.0)


class HTTPClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        user_agent: str,
        delay: float,
        timeout: float,
        retries: int,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self._last_request_started: float | None = None
        self._opener = urllib.request.build_opener(_SafeRedirectHandler(self.validate_url))

    def validate_url(self, url: str) -> str:
        return validate_remote_url(url, self.base_url)

    def _headers(self, accept: str, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {"Accept": accept, "User-Agent": self.user_agent}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _pace(self) -> None:
        now = time.monotonic()
        if self._last_request_started is not None:
            remaining = self.delay - (now - self._last_request_started)
            if remaining > 0:
                self.sleep(remaining)
        self._last_request_started = time.monotonic()

    def open(
        self,
        url: str,
        *,
        accept: str = JSON_ACCEPT,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        self.validate_url(url)
        last_error: BaseException | None = None
        for attempt in range(1, self.retries + 2):
            self._pace()
            request = urllib.request.Request(url, headers=self._headers(accept, headers), method="GET")
            try:
                return self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code in RETRYABLE_STATUS
                if retryable and attempt <= self.retries:
                    self.sleep(_retry_wait(exc.headers or {}, attempt))
                    continue
                raise HTTPRequestError(
                    f"HTTP {exc.code} for {url}", status=exc.code, retryable=retryable
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt <= self.retries:
                    self.sleep(min(2 ** (attempt - 1) + random.random(), 60.0))
                    continue
                raise HTTPRequestError(f"Request failed for {url}: {exc}", retryable=True) from exc
        raise HTTPRequestError(f"Request failed for {url}: {last_error}", retryable=True)

    def get_bytes(self, url: str, *, accept: str = JSON_ACCEPT) -> tuple[bytes, dict[str, str], int, str]:
        for body_attempt in range(1, self.retries + 2):
            response = self.open(url, accept=accept)
            try:
                with response:
                    return (
                        response.read(),
                        dict(response.headers.items()),
                        int(response.getcode() or 200),
                        str(response.geturl()),
                    )
            except (http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
                if body_attempt <= self.retries:
                    self.sleep(min(2 ** (body_attempt - 1) + random.random(), 60.0))
                    continue
                raise HTTPRequestError(f"Response body failed for {url}: {exc}", retryable=True) from exc
        raise HTTPRequestError(f"Response body failed for {url}", retryable=True)


@dataclass
class DownloadResult:
    status: str
    bytes_on_disk: int
    completed_at: str
    checksum_verified: bool | None


def _disk_allows(root: Path, remaining: int | None, reserve: int) -> bool:
    free = shutil.disk_usage(root).free
    needed = reserve + max(remaining or 0, 0)
    return free >= needed


def _rotate_bad_partial(part: Path) -> Path:
    suffix = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = part.with_name(f"{part.name}.bad-checksum-{suffix}")
    os.replace(part, destination)
    return destination


def download_file(
    client: HTTPClient,
    *,
    url: str,
    destination: Path,
    output_root: Path,
    expected_size: int | None,
    checksum: str,
    resume: bool,
    max_bytes: int | None,
    min_free_bytes: int,
) -> DownloadResult:
    destination = ensure_within(output_root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = ensure_within(output_root, destination)
    part = ensure_within(output_root, destination.with_name(f"{destination.name}.part"))
    if destination.is_symlink() or part.is_symlink():
        raise ValueError("Refusing to use a symlink as a download target")
    if max_bytes is not None and expected_size is not None and expected_size > max_bytes:
        raise DownloadSkipped(f"size_limit: {expected_size} bytes exceeds {max_bytes}")
    if file_matches(destination, expected_size, checksum):
        return DownloadResult("existing", destination.stat().st_size, utc_now(), parse_checksum(checksum) is not None)
    if resume and file_matches(part, expected_size, checksum):
        os.replace(part, destination)
        return DownloadResult("downloaded", destination.stat().st_size, utc_now(), parse_checksum(checksum) is not None)

    offset = part.stat().st_size if resume and part.is_file() else 0
    if expected_size is not None and offset > expected_size:
        _rotate_bad_partial(part)
        offset = 0
    remaining = expected_size - offset if expected_size is not None else None
    if not _disk_allows(output_root, remaining, min_free_bytes):
        raise DownloadSkipped("free_space_reserve")

    request_headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        response = client.open(url, accept="*/*", headers=request_headers)
    except HTTPRequestError as exc:
        if exc.status == 416 and part.is_file() and file_matches(part, expected_size, checksum):
            os.replace(part, destination)
            return DownloadResult("downloaded", destination.stat().st_size, utc_now(), parse_checksum(checksum) is not None)
        raise

    try:
        with response:
            status = int(response.getcode() or 200)
            append = offset > 0 and status == 206
            content_range = str(response.headers.get("Content-Range") or "")
            if status == 206:
                match = re.match(r"^bytes\s+(\d+)-\d+/(?:\d+|\*)$", content_range)
                expected_start = offset if append else 0
                if not match or int(match.group(1)) != expected_start:
                    raise HTTPRequestError("Server returned an invalid Content-Range")
            if offset > 0 and status == 200:
                offset = 0
            elif status not in {200, 206}:
                raise HTTPRequestError(f"Unexpected download HTTP status {status}", status=status)

            content_length = response.headers.get("Content-Length")
            try:
                response_bytes = int(content_length) if content_length is not None else None
            except ValueError:
                response_bytes = None
            if max_bytes is not None and response_bytes is not None and offset + response_bytes > max_bytes:
                raise DownloadSkipped("size_limit_from_content_length")
            if not _disk_allows(output_root, response_bytes, min_free_bytes):
                raise DownloadSkipped("free_space_reserve")

            mode = "ab" if append else "wb"
            written_this_request = 0
            with part.open(mode) as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    projected = offset + written_this_request + len(chunk)
                    if max_bytes is not None and projected > max_bytes:
                        stream.close()
                        try:
                            part.unlink()
                        except FileNotFoundError:
                            pass
                        raise DownloadSkipped("size_limit_while_streaming")
                    if shutil.disk_usage(output_root).free - len(chunk) < min_free_bytes:
                        raise DownloadSkipped("free_space_reserve_while_streaming")
                    stream.write(chunk)
                    written_this_request += len(chunk)
                stream.flush()
                os.fsync(stream.fileno())
    except (http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
        raise HTTPRequestError(f"Download stream failed: {exc}", retryable=True) from exc

    actual_size = part.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise HTTPRequestError(f"Downloaded size mismatch: expected {expected_size}, got {actual_size}")
    parsed = parse_checksum(checksum)
    verified: bool | None = None
    if parsed:
        algorithm, expected_digest = parsed
        verified = hash_file(part, algorithm) == expected_digest
        if not verified:
            rotated = _rotate_bad_partial(part)
            raise HTTPRequestError(f"Checksum mismatch; preserved bytes as {rotated.name}")
    os.replace(part, destination)
    return DownloadResult("downloaded", destination.stat().st_size, utc_now(), verified)
