from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit

from . import __version__
from .http import JSON_ACCEPT, DownloadSkipped, HTTPClient, HTTPRequestError, download_file
from .model import (
    SCHEMA_VERSION,
    catalog_summary,
    community_summary,
    merge_download_state,
    normalize_record,
    search_hits,
    search_next,
    search_total,
)
from .util import atomic_write_bytes, atomic_write_json, ensure_within, file_matches, parse_json_bytes, read_json, safe_component, utc_now


def _validate_base_origin(value: str, *, token_present: bool) -> None:
    parts = urlsplit(value)
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("--base-url must be a clean Zenodo origin without user information, query, or fragment")
    if parts.path not in {"", "/"}:
        raise ValueError("--base-url must not contain a path")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("--base-url contains an invalid port") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if host in {"zenodo.org", "sandbox.zenodo.org"}:
        if parts.scheme != "https" or port not in {None, 443}:
            raise ValueError("official Zenodo base URLs must use HTTPS on the standard port")
        return
    if host in {"127.0.0.1", "localhost", "::1"} and parts.scheme == "http":
        if token_present:
            raise ValueError("refusing to send ZENODO_TOKEN to a loopback test server")
        return
    raise ValueError("--base-url must be https://zenodo.org or https://sandbox.zenodo.org")


def _safe_log_url(url: str) -> str:
    if not url:
        return ""
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "REDACTED" if key.lower() in {"access_token", "token", "authorization"} else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


class ErrorLogger:
    def __init__(self, path: Path, run_id: str, community_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.community_id = community_id
        self.count = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        stage: str,
        message: str,
        *,
        method: str = "",
        record_id: str = "",
        concept_id: str = "",
        file_id: str = "",
        url: str = "",
        status: int | None = None,
        retryable: bool | None = None,
        error_type: str = "",
    ) -> None:
        entry = {
            "timestamp": utc_now(),
            "run_id": self.run_id,
            "stage": stage,
            "method": method,
            "community_id": self.community_id,
            "record_id": record_id,
            "concept_id": concept_id,
            "file_id": file_id,
            "url": _safe_log_url(url),
            "http_status": status,
            "error_type": error_type,
            "message": str(message),
            "retryable": retryable,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.count += 1


def _config_fingerprint(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def _scope_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "base_url": str(args.base_url).rstrip("/"),
        "community_id": args.community,
        "query": args.query,
        "sort": args.sort,
        "page_size": args.page_size,
        "all_versions": bool(args.all_versions),
    }


def output_scope_error(args: argparse.Namespace, output_root: Path) -> str:
    fingerprint = _config_fingerprint(_scope_config(args))
    checkpoint_path = output_root / "state" / "checkpoint.json"
    checkpoint_exists = checkpoint_path.exists()
    previous_checkpoint = read_json(checkpoint_path, None)
    if checkpoint_exists and not isinstance(previous_checkpoint, dict):
        return "the output checkpoint is unreadable; use a new --output directory"
    if args.resume and not checkpoint_exists:
        return "--resume requires an existing valid checkpoint in the selected output directory"
    if isinstance(previous_checkpoint, dict) and previous_checkpoint.get("config_fingerprint") != fingerprint:
        return "the selected output directory belongs to a different community/query/sort/page-size/version scope; use a new --output directory"
    raw_cache_root = output_root / "data" / "raw"
    if not checkpoint_exists and raw_cache_root.exists() and any(path.is_file() for path in raw_cache_root.rglob("*")):
        return "raw API caches exist without a matching checkpoint; use a new --output directory"
    return ""


def _load_or_fetch(
    *,
    path: Path,
    url: str,
    client: HTTPClient,
    refresh: bool,
    accept: str = JSON_ACCEPT,
) -> tuple[Any, bool]:
    if path.is_file() and not refresh:
        try:
            return parse_json_bytes(path.read_bytes(), str(path)), True
        except ValueError:
            pass
    payload, _, _, _ = client.get_bytes(url, accept=accept)
    parsed = parse_json_bytes(payload, url)
    atomic_write_bytes(path, payload)
    return parsed, False


def _checkpoint_base(run_id: str, fingerprint: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "config": config,
        "config_fingerprint": fingerprint,
        "discovery": {
            "last_completed_page": 0,
            "records_discovered": 0,
            "total_reported": None,
            "complete": False,
            "truncated": False,
        },
        "records": {},
        "updated_at": utc_now(),
    }


def _save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    checkpoint["updated_at"] = utc_now()
    atomic_write_json(path, checkpoint)


def _absolute_output_path(root: Path, relative: str) -> Path:
    return ensure_within(root, root / relative)


def _reconcile_local_download_state(record: dict[str, Any], output_root: Path) -> dict[str, Any]:
    for file_entry in record.get("hosted_files", []):
        if file_entry.get("status") not in {"downloaded", "existing"}:
            continue
        try:
            destination = _absolute_output_path(output_root, str(file_entry.get("local_path") or ""))
        except ValueError:
            destination = output_root / "__invalid_local_path__"
        expected_size = file_entry.get("size_bytes") if isinstance(file_entry.get("size_bytes"), int) else None
        if file_matches(destination, expected_size, str(file_entry.get("checksum") or "")):
            file_entry["downloaded_bytes"] = destination.stat().st_size
            continue
        file_entry.update(
            {
                "status": "not_requested",
                "downloaded_bytes": 0,
                "downloaded_at": "",
                "checksum_verified": None,
                "error": "",
            }
        )
    return record


def _record_key(hit: dict[str, Any]) -> str:
    return str(hit.get("id") or hit.get("record_id") or hit.get("recid") or "")


def _discover_records(
    *,
    args: argparse.Namespace,
    client: HTTPClient,
    output_root: Path,
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    errors: ErrorLogger,
) -> tuple[list[tuple[dict[str, Any], str]], int | None, bool]:
    params: dict[str, Any] = {
        "page": 1,
        "size": args.page_size,
        "sort": args.sort,
        "allversions": str(bool(args.all_versions)).lower(),
    }
    if args.query:
        params["q"] = args.query
    endpoint = f"{args.base_url}/api/communities/{quote(args.community, safe='')}/records"
    next_url = f"{endpoint}?{urlencode(params)}"
    discovered: list[tuple[dict[str, Any], str]] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    total_reported: int | None = None
    page = 1
    complete = True

    while next_url:
        if next_url in seen_urls:
            errors.log("discover", "Pagination cycle detected", method="zenodo_community_records_api", url=next_url, error_type="PaginationCycle")
            complete = False
            break
        seen_urls.add(next_url)
        page_path = output_root / "data" / "raw" / "search" / f"page-{page:06d}.json"
        relative_page_path = page_path.relative_to(output_root).as_posix()
        try:
            payload, _ = _load_or_fetch(path=page_path, url=next_url, client=client, refresh=args.refresh)
        except (HTTPRequestError, ValueError, OSError) as exc:
            errors.log(
                "discover",
                str(exc),
                method="zenodo_community_records_api",
                url=next_url,
                status=getattr(exc, "status", None),
                retryable=getattr(exc, "retryable", None),
                error_type=type(exc).__name__,
            )
            complete = False
            break
        hits = search_hits(payload)
        if not isinstance(payload, (dict, list)) or (isinstance(payload, dict) and "hits" not in payload):
            errors.log("discover", "Unexpected search response shape", method="zenodo_community_records_api", url=next_url, error_type="SchemaError")
            complete = False
            break
        reported = search_total(payload)
        if reported is not None:
            total_reported = reported
        count_before_page = len(seen_ids)
        for hit in hits:
            record_id = _record_key(hit)
            if not record_id:
                errors.log("discover", "Search hit has no record ID", method="zenodo_community_records_api", url=next_url, error_type="SchemaError")
                continue
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            discovered.append((hit, relative_page_path))
            if args.max_records and len(discovered) >= args.max_records:
                truncated = total_reported is None or len(discovered) < total_reported
                checkpoint["discovery"].update(
                    {
                        "last_completed_page": page,
                        "records_discovered": len(discovered),
                        "total_reported": total_reported,
                        "complete": not truncated,
                        "truncated": truncated,
                    }
                )
                _save_checkpoint(checkpoint_path, checkpoint)
                return discovered, total_reported, not truncated
        if hits and len(seen_ids) == count_before_page:
            errors.log(
                "discover",
                "Non-empty search page contained no new record IDs",
                method="zenodo_community_records_api",
                url=next_url,
                error_type="PaginationStall",
            )
            complete = False
            break
        checkpoint["discovery"].update(
            {
                "last_completed_page": page,
                "records_discovered": len(discovered),
                "total_reported": total_reported,
                "complete": False,
                "truncated": False,
            }
        )
        _save_checkpoint(checkpoint_path, checkpoint)
        if not hits:
            break
        candidate = search_next(payload)
        if candidate:
            candidate = urljoin(next_url, candidate)
            try:
                client.validate_url(candidate)
            except ValueError as exc:
                errors.log("discover", str(exc), method="zenodo_community_records_api", url=candidate, error_type=type(exc).__name__)
                complete = False
                break
            next_url = candidate
        elif total_reported is not None and len(seen_ids) < total_reported:
            page += 1
            params["page"] = page
            next_url = f"{endpoint}?{urlencode(params)}"
            continue
        elif total_reported is None and len(hits) >= args.page_size:
            page += 1
            params["page"] = page
            next_url = f"{endpoint}?{urlencode(params)}"
            continue
        else:
            next_url = ""
        page += 1

    if complete and total_reported is not None and len(discovered) < total_reported and not args.max_records:
        errors.log(
            "discover",
            f"API reported {total_reported} records but the crawl collected {len(discovered)} unique IDs",
            method="zenodo_community_records_api",
            error_type="CountMismatch",
        )
        complete = False

    checkpoint["discovery"].update(
        {
            "last_completed_page": max(page - 1, 0),
            "records_discovered": len(discovered),
            "total_reported": total_reported,
            "complete": complete,
            "truncated": False,
        }
    )
    _save_checkpoint(checkpoint_path, checkpoint)
    return discovered, total_reported, complete


def _files_payload_into_record(record: dict[str, Any], files_payload: Any) -> dict[str, Any]:
    merged = copy.deepcopy(record)
    if isinstance(files_payload, dict):
        merged["files"] = copy.deepcopy(files_payload)
    elif isinstance(files_payload, list):
        merged["files"] = {"enabled": True, "entries": copy.deepcopy(files_payload)}
    return merged


def _process_record(
    *,
    hit: dict[str, Any],
    discovery_path: str,
    args: argparse.Namespace,
    client: HTTPClient,
    output_root: Path,
    community: dict[str, Any],
    checkpoint: dict[str, Any],
    checkpoint_path: Path,
    errors: ErrorLogger,
) -> dict[str, Any]:
    record_id = _record_key(hit)
    safe_record_id = safe_component(record_id, fallback_prefix="record")
    record_dir = output_root / "data" / safe_record_id
    raw_record_path = record_dir / "api_response.json"
    raw_files_path = record_dir / "files_response.json"
    normalized_path = record_dir / "record.json"
    relative_record_raw = raw_record_path.relative_to(output_root).as_posix()
    relative_files_raw = raw_files_path.relative_to(output_root).as_posix()
    record_url = f"{args.base_url}/api/records/{quote(record_id, safe='')}"
    files_url = f"{record_url}/files"

    source_status = "record_api"
    try:
        record_payload, _ = _load_or_fetch(path=raw_record_path, url=record_url, client=client, refresh=args.refresh)
        if not isinstance(record_payload, dict):
            raise ValueError("Record API response is not a JSON object")
        returned_record_id = _record_key(record_payload)
        if returned_record_id and returned_record_id != record_id:
            raise ValueError(f"Record API returned ID {returned_record_id}, expected {record_id}")
        if not returned_record_id:
            record_payload = copy.deepcopy(record_payload)
            record_payload["id"] = record_id
    except (HTTPRequestError, ValueError, OSError) as exc:
        errors.log(
            "metadata",
            str(exc),
            method="zenodo_record_api",
            record_id=record_id,
            url=record_url,
            status=getattr(exc, "status", None),
            retryable=getattr(exc, "retryable", None),
            error_type=type(exc).__name__,
        )
        record_payload = copy.deepcopy(hit)
        source_status = "search_hit_fallback"

    record_links = record_payload.get("links") if isinstance(record_payload.get("links"), dict) else {}
    candidate_files_url = urljoin(record_url, str(record_links.get("files") or files_url))
    try:
        client.validate_url(candidate_files_url)
        files_url = candidate_files_url
    except ValueError:
        files_url = f"{args.base_url}/api/records/{quote(record_id, safe='')}/files"

    files_source_status = "files_api"
    try:
        files_payload, _ = _load_or_fetch(
            path=raw_files_path,
            url=files_url,
            client=client,
            refresh=args.refresh,
            accept="application/json",
        )
        if not isinstance(files_payload, (dict, list)):
            raise ValueError("Files API response is not a JSON object or array")
        record_payload = _files_payload_into_record(record_payload, files_payload)
    except (HTTPRequestError, ValueError, OSError) as exc:
        errors.log(
            "files",
            str(exc),
            method="zenodo_record_files_api",
            record_id=record_id,
            url=files_url,
            status=getattr(exc, "status", None),
            retryable=getattr(exc, "retryable", None),
            error_type=type(exc).__name__,
        )
        files_source_status = "embedded_files_fallback"

    previous = read_json(normalized_path, None)
    normalized = normalize_record(
        record_payload,
        community=community,
        collection=args.profile,
        base_url=args.base_url,
        raw_response_path=relative_record_raw,
        files_response_path=relative_files_raw,
        discovery_response_path=discovery_path,
        source_status=source_status,
        files_source_status=files_source_status,
    )
    normalized = merge_download_state(normalized, previous)
    normalized = _reconcile_local_download_state(normalized, output_root)
    for method in normalized["methods"]:
        if method.get("stage") == "download_files":
            method["enabled"] = bool(args.download_files)
    atomic_write_json(normalized_path, normalized)

    record_state = checkpoint.setdefault("records", {}).setdefault(record_id, {})
    record_state.update(
        {
            "concept_record_id": normalized["identifiers"].get("concept_record_id", ""),
            "metadata_status": source_status,
            "files_status": files_source_status,
            "downloads": record_state.get("downloads", {}),
            "updated_at": utc_now(),
        }
    )
    for file_entry in normalized.get("hosted_files", []):
        file_id = str(file_entry.get("file_id") or "")
        record_state["downloads"].setdefault(file_id, {}).update(
            {
                "status": str(file_entry.get("status") or "not_requested"),
                "bytes_on_disk": int(file_entry.get("downloaded_bytes") or 0),
                "checksum_verified": file_entry.get("checksum_verified"),
                "updated_at": utc_now(),
            }
        )
    _save_checkpoint(checkpoint_path, checkpoint)

    if not args.download_files:
        return normalized

    for file_entry in normalized.get("hosted_files", []):
        file_id = str(file_entry.get("file_id") or "")
        file_state = record_state["downloads"].setdefault(file_id, {})
        if not file_entry.get("downloadable"):
            file_entry["status"] = "skipped_restricted" if file_entry.get("restricted") else "skipped_no_content_link"
            file_entry["error"] = ""
            file_state.update({"status": file_entry["status"], "bytes_on_disk": 0, "updated_at": utc_now()})
            atomic_write_json(normalized_path, normalized)
            _save_checkpoint(checkpoint_path, checkpoint)
            continue
        destination = _absolute_output_path(output_root, str(file_entry["local_path"]))
        expected_size = file_entry.get("size_bytes") if isinstance(file_entry.get("size_bytes"), int) else None
        try:
            result = download_file(
                client,
                url=str(file_entry["download_url"]),
                destination=destination,
                output_root=output_root,
                expected_size=expected_size,
                checksum=str(file_entry.get("checksum") or ""),
                resume=args.resume,
                max_bytes=args.max_file_bytes,
                min_free_bytes=args.min_free_bytes,
            )
            file_entry.update(
                {
                    "status": result.status,
                    "downloaded_bytes": result.bytes_on_disk,
                    "downloaded_at": result.completed_at,
                    "checksum_verified": result.checksum_verified,
                    "error": "",
                }
            )
            file_state.update(
                {
                    "status": result.status,
                    "bytes_on_disk": result.bytes_on_disk,
                    "checksum_verified": result.checksum_verified,
                    "updated_at": result.completed_at,
                }
            )
        except DownloadSkipped as exc:
            file_entry.update({"status": "skipped_safety_limit", "error": str(exc)})
            file_state.update({"status": "skipped_safety_limit", "reason": str(exc), "updated_at": utc_now()})
        except (HTTPRequestError, ValueError, OSError) as exc:
            file_entry.update({"status": "failed", "error": str(exc)})
            partial = destination.with_name(f"{destination.name}.part")
            file_entry["downloaded_bytes"] = partial.stat().st_size if partial.is_file() else 0
            file_state.update({"status": "failed", "bytes_on_disk": file_entry["downloaded_bytes"], "error": str(exc), "updated_at": utc_now()})
            errors.log(
                "download_file",
                str(exc),
                method="zenodo_file_content_api",
                record_id=record_id,
                concept_id=str(normalized["identifiers"].get("concept_record_id") or ""),
                file_id=file_id,
                url=str(file_entry.get("download_url") or ""),
                status=getattr(exc, "status", None),
                retryable=getattr(exc, "retryable", None),
                error_type=type(exc).__name__,
            )
        atomic_write_json(normalized_path, normalized)
        _save_checkpoint(checkpoint_path, checkpoint)
    return normalized


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    token = os.environ.get("ZENODO_TOKEN", "").strip()
    if args.page_size > (100 if token else 25):
        parser.error("--page-size exceeds the supported maximum (25 anonymous, 100 with ZENODO_TOKEN)")
    if args.retries < 0:
        parser.error("--retries must be non-negative")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    args.base_url = args.base_url.rstrip("/")
    try:
        _validate_base_origin(args.base_url, token_present=bool(token))
    except ValueError as exc:
        parser.error(str(exc))
    if args.smoke_test:
        args.download_files = False
        args.inventory_only = True
        args.max_records = min(args.max_records or 2, 2)
    output_name = args.output or ("smoke-output" if args.smoke_test else "output")
    output_root = Path(output_name).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    args.max_file_bytes = int(args.max_file_mb * 1024 * 1024) if args.max_file_mb is not None else None
    args.min_free_bytes = int(args.min_free_gb * 1024 * 1024 * 1024)
    run_id = utc_now().replace("-", "").replace(":", "")
    errors = ErrorLogger(output_root / "logs" / "errors.jsonl", run_id, args.community)
    user_agent = os.environ.get(
        "ZENODO_USER_AGENT",
        f"zenodo/{__version__} (public research metadata harvester)",
    )
    client = HTTPClient(
        base_url=args.base_url,
        token=token,
        user_agent=user_agent,
        delay=args.delay,
        timeout=args.timeout,
        retries=args.retries,
    )
    try:
        client.validate_url(args.base_url)
    except ValueError as exc:
        parser.error(str(exc))

    scope_config = _scope_config(args)
    fingerprint = _config_fingerprint(scope_config)
    checkpoint_path = output_root / "state" / "checkpoint.json"
    checkpoint_exists = checkpoint_path.exists()
    previous_checkpoint = read_json(checkpoint_path, None)
    scope_error = output_scope_error(args, output_root)
    if scope_error:
        parser.error(scope_error)
    checkpoint = previous_checkpoint if args.resume and isinstance(previous_checkpoint, dict) else _checkpoint_base(run_id, fingerprint, scope_config)
    checkpoint["schema_version"] = SCHEMA_VERSION
    checkpoint["run_id"] = run_id
    _save_checkpoint(checkpoint_path, checkpoint)

    community_url = f"{args.base_url}/api/communities/{quote(args.community, safe='')}"
    community_raw_path = output_root / "data" / "raw" / "community.json"
    try:
        community_raw, _ = _load_or_fetch(
            path=community_raw_path,
            url=community_url,
            client=client,
            refresh=args.refresh,
        )
        if not isinstance(community_raw, dict):
            raise ValueError("Community API response is not a JSON object")
    except (HTTPRequestError, ValueError, OSError) as exc:
        errors.log(
            "community",
            str(exc),
            method="zenodo_community_api",
            url=community_url,
            status=getattr(exc, "status", None),
            retryable=getattr(exc, "retryable", None),
            error_type=type(exc).__name__,
        )
        community_raw = {
            "id": args.community,
            "slug": args.community,
            "metadata": {"title": args.profile["title"]},
        }
    community = community_summary(community_raw, args.community, args.base_url)

    discovered, total_reported, discovery_complete = _discover_records(
        args=args,
        client=client,
        output_root=output_root,
        checkpoint=checkpoint,
        checkpoint_path=checkpoint_path,
        errors=errors,
    )
    records: list[dict[str, Any]] = []
    for hit, discovery_path in discovered:
        record_id = _record_key(hit)
        try:
            normalized = _process_record(
                hit=hit,
                discovery_path=discovery_path,
                args=args,
                client=client,
                output_root=output_root,
                community=community,
                checkpoint=checkpoint,
                checkpoint_path=checkpoint_path,
                errors=errors,
            )
            records.append(normalized)
        except (ValueError, OSError) as exc:
            errors.log("normalize", str(exc), record_id=record_id, error_type=type(exc).__name__)

    summary = catalog_summary(records, errors.count)
    summary.update(
        {
            "records_discovered": len(discovered),
            "records_normalized": len(records),
            "records_failed": len(discovered) - len(records),
            "unique_concept_count": len({record["identifiers"].get("concept_record_id") or record["identifiers"].get("record_id") for record in records}),
        }
    )
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source": {
            "repository": "Zenodo",
            "collection": copy.deepcopy(args.profile),
            "community": community,
            "community_api_url": community_url,
            "community_records_api_url": f"{args.base_url}/api/communities/{quote(args.community, safe='')}/records",
            "version_scope": "all_versions" if args.all_versions else "latest_only",
        },
        "run": {
            "run_id": run_id,
            "mode": "download_files" if args.download_files else "inventory_only",
            "complete": discovery_complete and len(records) == len(discovered) and errors.count == 0,
            "truncated_by_max_records": bool(checkpoint.get("discovery", {}).get("truncated")),
            "query": args.query,
            "sort": args.sort,
            "page_size": args.page_size,
            "max_records": args.max_records,
            "refresh": bool(args.refresh),
            "resume": bool(args.resume),
            "max_file_bytes": args.max_file_bytes,
            "min_free_bytes": args.min_free_bytes,
            "api_total_reported": total_reported,
        },
        "summary": summary,
        "records": records,
    }
    atomic_write_json(output_root / "catalog.json", catalog)
    checkpoint["catalog"] = {
        "path": "catalog.json",
        "records_written": len(records),
        "completed_at": utc_now(),
    }
    _save_checkpoint(checkpoint_path, checkpoint)

    print(f"[{args.profile['key']}] Wrote {len(records)} normalized records to {output_root / 'catalog.json'}")
    print(f"Files inventoried: {summary['hosted_file_count']}; estimated size: {summary['estimated_download_size_human']}")
    if errors.count:
        print(f"Completed with {errors.count} logged error(s): {output_root / 'logs' / 'errors.jsonl'}", file=sys.stderr)
        return 2
    return 0
