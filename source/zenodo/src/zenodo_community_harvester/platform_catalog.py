from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterable

from .model import SCHEMA_VERSION, catalog_summary
from .profiles import CommunityProfile
from .util import atomic_write_json, read_json, utc_now


def _prefixed_path(prefix: str, value: Any) -> str:
    text = str(value or "")
    return f"{prefix}/{text}" if text else ""


def _rebase_record(record: dict[str, Any], profile: CommunityProfile) -> dict[str, Any]:
    rebased = copy.deepcopy(record)
    rebased["collection"] = profile.as_dict()
    for file_entry in rebased.get("hosted_files", []):
        if isinstance(file_entry, dict):
            file_entry["local_path"] = _prefixed_path(profile.key, file_entry.get("local_path"))
    local_paths = rebased.get("local_paths")
    if isinstance(local_paths, dict):
        for key, value in list(local_paths.items()):
            local_paths[key] = _prefixed_path(profile.key, value)
    for method in rebased.get("methods", []):
        if isinstance(method, dict) and method.get("response_path"):
            method["response_path"] = _prefixed_path(profile.key, method["response_path"])
    return rebased


def write_platform_catalog(
    output_root: Path,
    profiles: Iterable[CommunityProfile],
    args: Any,
    results: Iterable[tuple[CommunityProfile, int]],
) -> Path:
    result_codes = {profile.key: code for profile, code in results}
    records: list[dict[str, Any]] = []
    community_catalogs = []
    failed_steps = 0
    child_complete = True
    truncated = False
    for profile in profiles:
        relative_catalog = f"{profile.key}/catalog.json"
        catalog = read_json(output_root / relative_catalog, None)
        if not isinstance(catalog, dict):
            child_complete = False
            community_catalogs.append(
                {
                    "collection": profile.as_dict(),
                    "catalog_path": relative_catalog,
                    "status": "failed",
                    "exit_code": result_codes.get(profile.key),
                }
            )
            continue
        child_records = catalog.get("records") if isinstance(catalog.get("records"), list) else []
        records.extend(_rebase_record(record, profile) for record in child_records if isinstance(record, dict))
        child_summary = catalog.get("summary") if isinstance(catalog.get("summary"), dict) else {}
        failed_steps += int(child_summary.get("failed_step_count") or 0)
        child_run = catalog.get("run") if isinstance(catalog.get("run"), dict) else {}
        complete = bool(child_run.get("complete"))
        truncated = truncated or bool(child_run.get("truncated_by_max_records"))
        child_complete = child_complete and complete and result_codes.get(profile.key) == 0
        community_catalogs.append(
            {
                "collection": profile.as_dict(),
                "catalog_path": relative_catalog,
                "status": "completed" if result_codes.get(profile.key) == 0 else "completed_with_errors",
                "exit_code": result_codes.get(profile.key),
                "complete": complete,
                "summary": copy.deepcopy(child_summary),
            }
        )
    summary = catalog_summary(records, failed_steps)
    successful_communities = sum(item.get("status") == "completed" for item in community_catalogs)
    summary.update(
        {
            "community_count": len(community_catalogs),
            "community_success_count": successful_communities,
            "community_failure_count": len(community_catalogs) - successful_communities,
            "records_discovered": sum(int(item.get("summary", {}).get("records_discovered") or 0) for item in community_catalogs),
            "records_normalized": len(records),
            "unique_community_record_count": len(
                {
                    (record.get("collection", {}).get("key"), record.get("identifiers", {}).get("record_id"))
                    for record in records
                }
            ),
        }
    )
    catalog = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now(),
        "source": {
            "repository": "Zenodo",
            "scope": "replication_platform",
            "community_catalogs": community_catalogs,
        },
        "run": {
            "mode": "download_files" if args.download_files else "inventory_only",
            "complete": child_complete,
            "truncated_by_max_records": truncated,
            "query": args.query,
            "sort": args.sort,
            "page_size": args.page_size,
            "max_records_per_community": args.max_records,
            "all_versions": bool(args.all_versions),
            "refresh": bool(args.refresh),
            "resume": bool(args.resume),
        },
        "summary": summary,
        "records": records,
    }
    destination = output_root / "catalog.json"
    atomic_write_json(destination, catalog)
    print(f"[platform] Wrote {len(records)} normalized records to {destination}")
    return destination
