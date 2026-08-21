from __future__ import annotations

import argparse
import copy
import math
import os
import sys
import time
from pathlib import Path

from . import __version__
from .engine import _reconcile_local_download_state, output_scope_error, run
from .platform_catalog import write_platform_catalog
from .profiles import DEFAULT_PROFILE_KEY, CommunityProfile, all_profiles, custom_profile, resolve_profile
from .util import read_json


DEFAULT_BASE_URL = "https://zenodo.org"


def _finite_nonnegative(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite, non-negative number")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=os.environ.get("ZENODO_HARVESTER_PROG") or Path(sys.argv[0]).name,
        description="Inventory and optionally download public replication files from one or all supported Zenodo communities.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--inventory-only", action="store_true", help="Collect metadata and file inventories only (default).")
    mode.add_argument("--download-files", action="store_true", help="Download public files exposed by the Zenodo files API.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--journal",
        "--profile",
        dest="journal",
        metavar="NAME",
        help=f"Built-in journal profile or alias (default: {DEFAULT_PROFILE_KEY}); use 'all' for every profile.",
    )
    selection.add_argument("--all-journals", action="store_true", help="Run every built-in journal profile into isolated subdirectories.")
    selection.add_argument("--community", metavar="SLUG", help="Advanced: crawl an arbitrary public Zenodo community slug.")
    parser.add_argument("--list-journals", action="store_true", help="List built-in profile names and Zenodo community slugs, then exit.")
    parser.add_argument("--smoke-test", action="store_true", help="Inventory at most two records per selected community and never download files.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Zenodo base URL; useful for the official sandbox.")
    parser.add_argument(
        "--output",
        help="Single-journal output directory, or parent directory in all-journals mode (defaults are journal-isolated).",
    )
    parser.add_argument("--query", default="", help="Optional Zenodo query-string search inside each selected community.")
    parser.add_argument("--sort", default="newest", choices=("newest", "oldest", "updated-desc", "updated-asc", "version"))
    parser.add_argument("--all-versions", action="store_true", help="Inventory all published versions instead of latest versions only.")
    parser.add_argument("--page-size", type=_positive_int, default=25, help="Search results per page (anonymous maximum: 25).")
    parser.add_argument("--max-records", type=_positive_int, help="Stop after this many unique records in each community.")
    parser.add_argument("--delay", type=_finite_nonnegative, default=2.1, help="Minimum delay between requests in seconds (default: 2.1).")
    parser.add_argument("--timeout", type=_finite_nonnegative, default=120.0, help="Per-request timeout in seconds (default: 120).")
    parser.add_argument("--retries", type=int, default=5, help="Retries for rate limits and transient failures (default: 5).")
    parser.add_argument("--resume", action="store_true", help="Resume partial files and require a matching checkpoint for every selected community.")
    parser.add_argument("--refresh", action="store_true", help="Refetch cached community, search, record, and file-list JSON.")
    parser.add_argument("--max-file-mb", type=_finite_nonnegative, help="Skip any individual file larger than this many MiB.")
    parser.add_argument("--min-free-gb", type=_finite_nonnegative, default=1.0, help="Keep at least this many GiB free (default: 1).")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _print_profiles() -> None:
    print("PROFILE\tABBREVIATION\tZENODO COMMUNITY\tTITLE")
    for profile in all_profiles():
        print(f"{profile.key}\t{profile.abbreviation}\t{profile.slug}\t{profile.title}")


def _selected_profiles(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[CommunityProfile, ...]:
    if args.community:
        try:
            return (custom_profile(args.community),)
        except ValueError as exc:
            parser.error(str(exc))
    if args.all_journals or (args.journal and args.journal.strip().lower() == "all"):
        return all_profiles()
    try:
        return (resolve_profile(args.journal or DEFAULT_PROFILE_KEY),)
    except ValueError as exc:
        parser.error(str(exc))
    raise AssertionError("argparse should have exited after an invalid profile")


def _prepare_single(args: argparse.Namespace, profile: CommunityProfile, output: Path) -> argparse.Namespace:
    child = copy.deepcopy(args)
    child.community = profile.slug
    child.profile = profile.as_dict()
    child.output = str(output)
    return child


def _guard_single_output(output: Path, parser: argparse.ArgumentParser) -> None:
    existing = read_json(output / "catalog.json", None)
    source = existing.get("source") if isinstance(existing, dict) and isinstance(existing.get("source"), dict) else {}
    if source.get("scope") == "replication_platform":
        parser.error("the selected --output is an all-journals root; choose one of its community directories or a new directory")


def _guard_platform_output(output: Path, parser: argparse.ArgumentParser) -> None:
    if (output / "data").exists() or (output / "state" / "checkpoint.json").exists():
        parser.error("the all-journals output root contains a single-community crawl; choose a new --output directory")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_journals:
        _print_profiles()
        return 0
    if args.smoke_test:
        args.download_files = False
        args.inventory_only = True
        args.max_records = min(args.max_records or 2, 2)
    profiles = _selected_profiles(args, parser)
    single_default_root = Path("smoke-output" if args.smoke_test else "output")
    if len(profiles) == 1:
        if args.output:
            output = Path(args.output)
        elif not args.journal and not args.all_journals:
            output = single_default_root
        else:
            output = single_default_root / profiles[0].key
        output = output.expanduser().resolve()
        _guard_single_output(output, parser)
        return run(_prepare_single(args, profiles[0], output), parser)

    platform_default_root = Path("smoke-platform-output" if args.smoke_test else "platform-output")
    output_root = (Path(args.output) if args.output else platform_default_root).expanduser().resolve()
    _guard_platform_output(output_root, parser)
    children = [_prepare_single(args, profile, output_root / profile.key) for profile in profiles]
    preflight_errors = []
    for profile, child in zip(profiles, children):
        error = output_scope_error(child, Path(child.output))
        if error:
            preflight_errors.append(f"{profile.key}: {error}")
    if preflight_errors:
        parser.error("cannot start all-journals run; " + "; ".join(preflight_errors))
    output_root.mkdir(parents=True, exist_ok=True)
    results: list[tuple[CommunityProfile, int]] = []
    for index, (profile, child) in enumerate(zip(profiles, children)):
        if index:
            time.sleep(args.delay)
        results.append((profile, run(child, parser)))
    write_platform_catalog(output_root, profiles, args, results)
    return 2 if any(code != 0 for _, code in results) else 0


__all__ = ["build_parser", "main", "_reconcile_local_download_state"]
