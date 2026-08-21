from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .ckan import JOURNALDATA_HOSTS, JournalDataClient
from .http import FileTooLargeError, HttpClient, InsufficientDiskSpaceError
from .models import PublicationRecord, ReplicationPackage, ResourceRecord
from .parser import ParsedRecord, parse_publications
from .storage import Storage
from .utils import (
    filename_from_url,
    parse_content_disposition_filename,
    safe_filename,
    utc_now,
)

DEFAULT_SOURCE_URL = "https://jcr-econ.org/publications/"
SOURCE_HOSTS = {"jcr-econ.org", "www.jcr-econ.org"}


@dataclass
class ScrapeConfig:
    output_dir: Path
    source_url: str = DEFAULT_SOURCE_URL
    download_files: bool = False
    resume: bool = False
    refresh: bool = False
    max_records: int | None = None
    max_file_mb: float | None = None
    min_free_gb: float = 2.0
    journal_codes: set[str] = field(default_factory=set)
    year_min: int | None = None
    year_max: int | None = None
    extra_download_hosts: set[str] = field(default_factory=set)

    @property
    def max_file_bytes(self) -> int | None:
        if self.max_file_mb is None:
            return None
        return max(0, int(self.max_file_mb * 1024 * 1024))

    @property
    def min_free_bytes(self) -> int:
        return max(0, int(self.min_free_gb * 1024 * 1024 * 1024))


class JcreScraper:
    def __init__(
        self,
        config: ScrapeConfig,
        http: HttpClient,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.http = http
        self.storage = Storage(config.output_dir)
        self.journaldata = JournalDataClient(http)
        self.logger = logger or logging.getLogger(__name__)
        self.current_error_count = 0
        self.downloads_halted = False

    def _record_error(
        self,
        *,
        stage: str,
        exc: Exception | str,
        record_id: str | None = None,
        url: str | None = None,
    ) -> None:
        message = str(exc)
        error_type = type(exc).__name__ if isinstance(exc, Exception) else "Warning"
        self.current_error_count += 1
        self.storage.append_error(
            stage=stage,
            message=message,
            record_id=record_id,
            url=url,
            error_type=error_type,
        )
        self.logger.warning("%s%s: %s", f"[{record_id}] " if record_id else "", stage, message)

    def _fetch_publications(self) -> str:
        html_path = self.storage.source_dir / "publications.html"
        metadata_path = self.storage.source_dir / "publications_response.json"
        cached_metadata = self.storage.read_json(metadata_path)
        cache_matches_source = bool(
            cached_metadata
            and cached_metadata.get("requested_url") == self.config.source_url
        )
        if html_path.exists() and cache_matches_source and not self.config.refresh:
            self.logger.info("Using cached publications page: %s", html_path)
            return html_path.read_text(encoding="utf-8")

        self.logger.info("Fetching publications index: %s", self.config.source_url)
        source_host = (urlparse(self.config.source_url).hostname or "").lower().rstrip(".")
        allowed_hosts = set(SOURCE_HOSTS)
        if source_host:
            allowed_hosts.add(source_host)
            if source_host.startswith("www."):
                allowed_hosts.add(source_host.removeprefix("www."))
            else:
                allowed_hosts.add(f"www.{source_host}")
        html, opened = self.http.get_text(self.config.source_url, allowed_hosts=allowed_hosts)
        self.storage.write_text(html_path, html)
        response = opened.response
        self.storage.write_json(
            metadata_path,
            {
                "fetched_at": utc_now(),
                "requested_url": self.config.source_url,
                "resolved_url": opened.final_url,
                "redirect_chain": opened.redirect_chain,
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type"),
                "etag": response.headers.get("ETag"),
                "last_modified": response.headers.get("Last-Modified"),
            },
        )
        return html

    def _filter_records(self, parsed: list[ParsedRecord]) -> list[ParsedRecord]:
        filtered: list[ParsedRecord] = []
        journals = {item.upper() for item in self.config.journal_codes}
        for item in parsed:
            record = item.record
            if journals and record.journal_code.upper() not in journals:
                continue
            if self.config.year_min is not None and (record.year is None or record.year < self.config.year_min):
                continue
            if self.config.year_max is not None and (record.year is None or record.year > self.config.year_max):
                continue
            filtered.append(item)
        if self.config.max_records is not None:
            filtered = filtered[: max(0, self.config.max_records)]
        return filtered

    @staticmethod
    def _methods_for(record: PublicationRecord) -> list[dict[str, Any]]:
        methods: list[dict[str, Any]] = [
            {
                "stage": "discover_publication",
                "method": "jcre_publications_html",
                "request_url": record.publications_url,
            }
        ]
        if record.replication.link_url:
            methods.extend(
                [
                    {
                        "stage": "resolve_replication_link",
                        "method": "doi_or_landing_page_redirect_resolution",
                        "request_url": record.replication.link_url,
                    },
                    {
                        "stage": "fetch_replication_metadata",
                        "method": "zbw_journal_data_ckan_package_show",
                        "request_url": "https://journaldata.zbw.eu/api/3/action/package_show",
                    },
                    {
                        "stage": "download_resources",
                        "method": "ckan_exact_resource_url",
                        "enabled": False,
                    },
                ]
            )
        return methods

    @staticmethod
    def _merge_download_state(new: ReplicationPackage, old: ReplicationPackage) -> None:
        old_by_id = {resource.resource_id: resource for resource in old.resources}
        for resource in new.resources:
            previous = old_by_id.get(resource.resource_id)
            if previous is None:
                continue
            for field_name in (
                "download_status",
                "local_path",
                "sha256",
                "downloaded_bytes",
                "final_url",
                "redirect_chain",
                "error",
            ):
                setattr(resource, field_name, getattr(previous, field_name))

    def _load_cached_package(self, record: PublicationRecord, record_path: Path) -> bool:
        if self.config.refresh:
            return False
        existing_payload = self.storage.read_json(record_path)
        if not existing_payload:
            return False
        try:
            existing = PublicationRecord.from_dict(existing_payload)
        except (TypeError, ValueError):
            return False
        if existing.replication.link_url != record.replication.link_url:
            return False
        if existing.replication.inventory_status not in {
            "complete",
            "external_repository",
            "package_not_found",
            "no_link",
        }:
            return False
        availability = record.replication.availability_text
        link_url = record.replication.link_url
        doi = record.replication.doi or existing.replication.doi
        record.replication = existing.replication
        record.replication.availability_text = availability
        record.replication.link_url = link_url
        record.replication.doi = doi
        self.logger.info("[%s] Using cached package inventory", record.record_id)
        return True

    def _inventory_package(self, record: PublicationRecord, record_dir: Path) -> None:
        if not record.replication.link_url:
            record.replication.inventory_status = "no_link"
            return
        try:
            resolution = self.journaldata.resolve(record.replication)
            record.replication = resolution.package
            if resolution.landing_metadata is not None:
                self.storage.write_json(record_dir / "replication_landing_response.json", resolution.landing_metadata)
            if resolution.landing_html is not None:
                self.storage.write_bytes(record_dir / "replication_landing.html", resolution.landing_html)
            if resolution.package_response is not None:
                self.storage.write_json(record_dir / "ckan_response.json", resolution.package_response)
            if resolution.search_response is not None:
                self.storage.write_json(record_dir / "ckan_search_response.json", resolution.search_response)
        except Exception as exc:
            record.replication.inventory_status = "failed"
            record.replication.error = str(exc)
            record.error_count += 1
            self._record_error(
                stage="replication_inventory",
                exc=exc,
                record_id=record.record_id,
                url=record.replication.link_url,
            )

    @staticmethod
    def _resource_filename(resource: ResourceRecord) -> str:
        url_name = filename_from_url(resource.url, fallback="")
        source_name = safe_filename(resource.name, fallback="")
        if url_name and "." in url_name:
            return url_name
        if source_name:
            if "." not in source_name and resource.format:
                extension = safe_filename(resource.format.lower(), fallback="").strip(".")
                if extension and len(extension) <= 10:
                    source_name = f"{source_name}.{extension}"
            return source_name
        return url_name or "download.bin"

    def _checkpoint_record(self, checkpoint: dict[str, Any], record: PublicationRecord, record_path: Path) -> None:
        checkpoint.setdefault("records", {})[record.record_id] = {
            "updated_at": utc_now(),
            "record_path": self.storage.relative(record_path),
            "inventory_status": record.replication.inventory_status,
            "status": record.status,
            "downloads": {
                resource.resource_id: {
                    "status": resource.download_status,
                    "local_path": resource.local_path,
                    "sha256": resource.sha256,
                    "downloaded_bytes": resource.downloaded_bytes,
                }
                for resource in record.replication.resources
            },
        }
        self.storage.save_checkpoint(checkpoint)

    def _write_record(self, record: PublicationRecord, record_path: Path) -> None:
        self.storage.write_json(record_path, record.to_dict())

    def _download_record(
        self,
        record: PublicationRecord,
        record_dir: Path,
        record_path: Path,
        checkpoint: dict[str, Any],
    ) -> None:
        if not self.config.download_files:
            return
        for method in record.methods:
            if method.get("stage") == "download_resources":
                method["enabled"] = True

        allowed_hosts = set(JOURNALDATA_HOSTS) | {host.lower() for host in self.config.extra_download_hosts}
        for resource in record.replication.resources:
            if self.downloads_halted:
                if resource.download_status in {"not_requested", "pending"}:
                    resource.download_status = "skipped_downloads_halted"
                continue
            if not resource.downloadable:
                if resource.download_status == "not_requested":
                    resource.download_status = "skipped_not_repository_hosted"
                continue
            if (
                self.config.max_file_bytes is not None
                and resource.size_bytes is not None
                and resource.size_bytes > self.config.max_file_bytes
            ):
                resource.download_status = "skipped_size_limit"
                resource.error = (
                    f"Resource metadata reports {resource.size_bytes} bytes; "
                    f"limit is {self.config.max_file_bytes} bytes"
                )
                continue

            if resource.local_path:
                try:
                    target = self.storage.resolve_relative(resource.local_path)
                except ValueError as exc:
                    self._record_error(
                        stage="cached_download_path",
                        exc=exc,
                        record_id=record.record_id,
                        url=resource.url,
                    )
                    resource.local_path = None
                    resource.sha256 = None
                    resource.downloaded_bytes = None
                    target = (
                        record_dir
                        / "files"
                        / safe_filename(resource.resource_id, fallback="resource")
                        / self._resource_filename(resource)
                    )
            else:
                target = (
                    record_dir
                    / "files"
                    / safe_filename(resource.resource_id, fallback="resource")
                    / self._resource_filename(resource)
                )
            resource.download_status = "pending"
            self._write_record(record, record_path)
            self._checkpoint_record(checkpoint, record, record_path)

            try:
                result = self.http.download_to(
                    resource.url,
                    target,
                    allowed_hosts=allowed_hosts,
                    resume=self.config.resume,
                    max_bytes=self.config.max_file_bytes,
                    min_free_bytes=self.config.min_free_bytes,
                )
                disposition_name = parse_content_disposition_filename(result.content_disposition)
                if disposition_name and disposition_name != result.local_path.name:
                    renamed = result.local_path.with_name(disposition_name)
                    if not renamed.exists():
                        result.local_path.replace(renamed)
                        result.local_path = renamed
                resource.download_status = result.status
                resource.local_path = self.storage.relative(result.local_path)
                resource.sha256 = result.sha256
                resource.downloaded_bytes = result.downloaded_bytes
                resource.final_url = result.final_url
                resource.redirect_chain = result.redirect_chain
                resource.error = None
            except FileTooLargeError as exc:
                resource.download_status = "skipped_size_limit"
                resource.error = str(exc)
                self._record_error(
                    stage="download_size_limit",
                    exc=exc,
                    record_id=record.record_id,
                    url=resource.url,
                )
            except InsufficientDiskSpaceError as exc:
                resource.download_status = "skipped_low_disk"
                resource.error = str(exc)
                self.downloads_halted = True
                self._record_error(
                    stage="download_disk_reserve",
                    exc=exc,
                    record_id=record.record_id,
                    url=resource.url,
                )
            except Exception as exc:
                resource.download_status = "failed"
                resource.error = str(exc)
                record.error_count += 1
                self._record_error(
                    stage="download",
                    exc=exc,
                    record_id=record.record_id,
                    url=resource.url,
                )
            finally:
                self._write_record(record, record_path)
                self._checkpoint_record(checkpoint, record, record_path)

    @staticmethod
    def _status_for(record: PublicationRecord) -> str:
        if record.replication.inventory_status == "failed":
            return "inventory_failed"
        if record.replication.inventory_status == "no_link":
            return "inventoried_no_replication_link"
        if record.replication.inventory_status == "external_repository":
            return "inventoried_external_replication_link"
        if record.replication.inventory_status == "package_not_found":
            return "inventoried_package_not_found"
        if record.replication.inventory_status == "complete":
            failed = any(resource.download_status == "failed" for resource in record.replication.resources)
            if failed:
                return "inventoried_with_download_errors"
            return "inventoried"
        return "discovered"

    def _build_summary(self, records: list[PublicationRecord]) -> dict[str, Any]:
        resources = [resource for record in records for resource in record.replication.resources]
        package_keys = {
            record.replication.dataset_id
            or record.replication.doi
            or record.replication.resolved_url
            or record.replication.link_url
            for record in records
            if record.replication.link_url
        }
        package_keys.discard(None)
        known_sizes = [resource.size_bytes for resource in resources if resource.size_bytes is not None]
        downloadable_known_sizes = [
            resource.size_bytes
            for resource in resources
            if resource.downloadable and resource.size_bytes is not None
        ]
        downloadable_unknown_size_count = sum(
            resource.downloadable and resource.size_bytes is None for resource in resources
        )
        download_statuses = Counter(resource.download_status for resource in resources)
        inventory_statuses = Counter(record.replication.inventory_status for record in records)
        journal_counts = Counter(record.journal_code for record in records)
        years = [record.year for record in records if record.year is not None]
        return {
            "record_count": len(records),
            "journal_counts": dict(sorted(journal_counts.items())),
            "year_min": min(years) if years else None,
            "year_max": max(years) if years else None,
            "records_with_article_doi": sum(bool(record.article_doi) for record in records),
            "records_with_replication_link": sum(bool(record.replication.link_url) for record in records),
            "unique_replication_packages_referenced": len(package_keys),
            "inventory_status_counts": dict(sorted(inventory_statuses.items())),
            "resource_count": len(resources),
            "repository_hosted_resource_count": sum(resource.hosted_by_repository for resource in resources),
            "downloadable_resource_count": sum(resource.downloadable for resource in resources),
            "external_resource_count": sum(not resource.hosted_by_repository for resource in resources),
            "known_size_resource_count": len(known_sizes),
            "unknown_size_resource_count": len(resources) - len(known_sizes),
            "known_size_downloadable_resource_count": len(downloadable_known_sizes),
            "unknown_size_downloadable_resource_count": downloadable_unknown_size_count,
            "estimated_download_bytes_from_known_sizes": sum(downloadable_known_sizes),
            "download_status_counts": dict(sorted(download_statuses.items())),
            "error_count_this_run": self.current_error_count,
            "downloads_halted_for_disk_reserve": self.downloads_halted,
        }

    def run(self) -> dict[str, Any]:
        checkpoint = self.storage.load_checkpoint()
        checkpoint["source_url"] = self.config.source_url

        html = self._fetch_publications()
        parsed_output = parse_publications(html, self.config.source_url)
        for warning in parsed_output.warnings:
            self._record_error(stage="parse_warning", exc=warning)
        parsed_records = self._filter_records(parsed_output.records)
        if not parsed_records:
            raise RuntimeError("No publication records remained after parsing and filtering.")

        records: list[PublicationRecord] = []
        self.logger.info("Processing %d publication records", len(parsed_records))
        for index, parsed in enumerate(parsed_records, start=1):
            record = parsed.record
            record.methods = self._methods_for(record)
            record_dir = self.storage.record_dir(record.record_id)
            record_path = record_dir / "record.json"
            fragment_path = record_dir / "publication_fragment.html"
            self.storage.write_text(fragment_path, parsed.fragment_html)
            record.source_fragment_path = self.storage.relative(fragment_path)

            cached_payload = self.storage.read_json(record_path)
            cached_package = None
            if cached_payload:
                try:
                    cached_package = PublicationRecord.from_dict(cached_payload).replication
                except (TypeError, ValueError):
                    cached_package = None

            used_cache = self._load_cached_package(record, record_path)
            if not used_cache:
                self._inventory_package(record, record_dir)
                if cached_package is not None:
                    self._merge_download_state(record.replication, cached_package)

            self.logger.info(
                "[%d/%d] %s — %s",
                index,
                len(parsed_records),
                record.record_id,
                record.title,
            )
            record.status = self._status_for(record)
            self._write_record(record, record_path)
            self._checkpoint_record(checkpoint, record, record_path)

            self._download_record(record, record_dir, record_path, checkpoint)
            record.status = self._status_for(record)
            self._write_record(record, record_path)
            self._checkpoint_record(checkpoint, record, record_path)
            records.append(record)

        catalog = {
            "schema_version": "1.0",
            "generated_at": utc_now(),
            "source": {
                "publications_url": self.config.source_url,
                "publications_html_path": self.storage.relative(self.storage.source_dir / "publications.html"),
                "discovery_method": "jcre_publications_html",
                "package_metadata_method": "zbw_journal_data_ckan_action_api",
                "download_method": "exact_ckan_resource_url",
                "ckan_api_base": "https://journaldata.zbw.eu/api/3/action",
            },
            "summary": self._build_summary(records),
            "records": [record.to_dict() for record in records],
        }
        self.storage.write_json(self.storage.catalog_path, catalog)
        return catalog
