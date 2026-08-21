from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RelatedLink:
    label: str
    url: str
    doi: str | None = None
    role: str = "related"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceRecord:
    resource_id: str
    name: str
    url: str
    description: str = ""
    format: str = ""
    mimetype: str = ""
    mimetype_inner: str = ""
    url_type: str = ""
    size_bytes: int | None = None
    created: str | None = None
    last_modified: str | None = None
    hash: str | None = None
    hosted_by_repository: bool = False
    downloadable: bool = False
    download_reason: str = ""
    download_status: str = "not_requested"
    local_path: str | None = None
    sha256: str | None = None
    downloaded_bytes: int | None = None
    final_url: str | None = None
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ResourceRecord":
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: val for key, val in value.items() if key in known})


@dataclass
class ReplicationPackage:
    availability_text: str = ""
    link_url: str | None = None
    doi: str | None = None
    resolved_url: str | None = None
    redirect_chain: list[dict[str, Any]] = field(default_factory=list)
    repository: str | None = None
    dataset_slug: str | None = None
    dataset_id: str | None = None
    title: str | None = None
    notes: str | None = None
    author: str | None = None
    maintainer: str | None = None
    version: str | None = None
    license_id: str | None = None
    license_title: str | None = None
    metadata_created: str | None = None
    metadata_modified: str | None = None
    organization: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    resources: list[ResourceRecord] = field(default_factory=list)
    inventory_status: str = "not_started"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["resources"] = [resource.to_dict() for resource in self.resources]
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ReplicationPackage":
        if not value:
            return cls()
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload = {key: val for key, val in value.items() if key in known and key != "resources"}
        payload["resources"] = [ResourceRecord.from_dict(item) for item in value.get("resources", [])]
        return cls(**payload)


@dataclass
class PublicationRecord:
    record_id: str
    journal_code: str
    journal_name: str
    volume: int | None
    year: int | None
    issue: str | None
    title: str
    authors_text: str
    citation_text: str
    article_doi: str | None
    article_url: str | None
    publications_url: str
    volume_heading: str
    source_order: int
    source_fragment_path: str | None = None
    related_links: list[RelatedLink] = field(default_factory=list)
    methods: list[dict[str, Any]] = field(default_factory=list)
    replication: ReplicationPackage = field(default_factory=ReplicationPackage)
    status: str = "discovered"
    error_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["related_links"] = [item.to_dict() for item in self.related_links]
        payload["replication"] = self.replication.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PublicationRecord":
        known = {field_name for field_name in cls.__dataclass_fields__}
        payload = {
            key: val
            for key, val in value.items()
            if key in known and key not in {"related_links", "replication"}
        }
        payload["related_links"] = [RelatedLink(**item) for item in value.get("related_links", [])]
        payload["replication"] = ReplicationPackage.from_dict(value.get("replication"))
        return cls(**payload)
