from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any


DEFAULT_PROFILE_KEY = "the-economic-journal"
_PROFILE_PACKAGE = "zenodo_community_harvester.community_profiles"
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PROFILE_ORDER = ("the-economic-journal", "restud", "econometric-society", "jeea")


@dataclass(frozen=True, slots=True)
class CommunityProfile:
    key: str
    slug: str
    title: str
    abbreviation: str
    aliases: tuple[str, ...]
    records_url: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "slug": self.slug,
            "title": self.title,
            "abbreviation": self.abbreviation,
            "aliases": list(self.aliases),
            "records_url": self.records_url,
        }


def _normalized_alias(value: str) -> str:
    return re.sub(r"[-_\s]+", "-", value.strip().lower())


def _profile_from_mapping(payload: Any, source: str) -> CommunityProfile:
    if not isinstance(payload, dict):
        raise ValueError(f"{source}: profile must be a JSON object")
    required = ("key", "slug", "title", "abbreviation", "aliases", "records_url")
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"{source}: missing profile field(s): {', '.join(missing)}")
    key = _normalized_alias(str(payload["key"]))
    slug = str(payload["slug"]).strip()
    title = str(payload["title"]).strip()
    abbreviation = str(payload["abbreviation"]).strip()
    aliases_raw = payload["aliases"]
    records_url = str(payload["records_url"]).strip()
    if not key or not _SAFE_SLUG.fullmatch(key):
        raise ValueError(f"{source}: invalid profile key")
    if not _SAFE_SLUG.fullmatch(slug):
        raise ValueError(f"{source}: invalid Zenodo community slug")
    if not title:
        raise ValueError(f"{source}: title cannot be blank")
    if not isinstance(aliases_raw, list) or not all(isinstance(value, str) and value.strip() for value in aliases_raw):
        raise ValueError(f"{source}: aliases must be a list of non-empty strings")
    if records_url != f"https://zenodo.org/communities/{slug}/records":
        raise ValueError(f"{source}: records_url must match the official community slug")
    aliases = tuple(dict.fromkeys(_normalized_alias(value) for value in aliases_raw))
    return CommunityProfile(key, slug, title, abbreviation, aliases, records_url)


@lru_cache(maxsize=1)
def all_profiles() -> tuple[CommunityProfile, ...]:
    profile_root = resources.files(_PROFILE_PACKAGE)
    profiles = []
    for resource in sorted(profile_root.iterdir(), key=lambda item: item.name):
        if resource.name.endswith(".json"):
            profiles.append(_profile_from_mapping(json.loads(resource.read_text(encoding="utf-8")), resource.name))
    if not profiles:
        raise RuntimeError("no built-in Zenodo community profiles were found")
    order = {key: index for index, key in enumerate(_PROFILE_ORDER)}
    profiles.sort(key=lambda profile: (order.get(profile.key, len(order)), profile.key))
    keys: set[str] = set()
    aliases: dict[str, str] = {}
    for profile in profiles:
        if profile.key in keys:
            raise RuntimeError(f"duplicate profile key: {profile.key}")
        keys.add(profile.key)
        for value in (profile.key, profile.slug, profile.abbreviation, *profile.aliases):
            alias = _normalized_alias(value)
            previous = aliases.get(alias)
            if previous and previous != profile.key:
                raise RuntimeError(f"profile alias {value!r} belongs to both {previous} and {profile.key}")
            aliases[alias] = profile.key
    return tuple(profiles)


def resolve_profile(value: str) -> CommunityProfile:
    requested = _normalized_alias(value)
    for profile in all_profiles():
        candidates = (profile.key, profile.slug, profile.abbreviation, *profile.aliases)
        if requested in {_normalized_alias(candidate) for candidate in candidates}:
            return profile
    known = ", ".join(profile.key for profile in all_profiles())
    raise ValueError(f"unknown journal profile {value!r}; choose one of: {known}")


def custom_profile(slug: str) -> CommunityProfile:
    value = slug.strip()
    if not _SAFE_SLUG.fullmatch(value):
        raise ValueError("--community must be a Zenodo community slug containing only lowercase letters, digits, '.', '_' or '-'")
    return CommunityProfile(
        key=_normalized_alias(value),
        slug=value,
        title=value,
        abbreviation="",
        aliases=(),
        records_url=f"https://zenodo.org/communities/{value}/records",
    )
