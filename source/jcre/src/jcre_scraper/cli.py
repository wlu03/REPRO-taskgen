from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .http import HttpClient
from .scraper import DEFAULT_SOURCE_URL, JcreScraper, ScrapeConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jcre-scraper",
        description=(
            "Inventory JCRE/IREE publications, resolve their ZBW Journal Data replication packages, "
            "and optionally download repository-hosted resources."
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--inventory-only",
        action="store_true",
        help="Build metadata and resource inventories without downloading replication files (default).",
    )
    mode.add_argument(
        "--download-files",
        action="store_true",
        help="Build the inventory and download repository-hosted replication resources.",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Process the first three matching records and default to smoke-output/.",
    )
    parser.add_argument("--output", "--output-dir", "--output-root", type=Path, default=None, help="Output directory (default: output/).")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="JCRE publications index URL.")
    parser.add_argument("--resume", action="store_true", help="Resume .part downloads and preserve completed files.")
    parser.add_argument("--refresh", action="store_true", help="Refetch the publications page and package metadata.")
    parser.add_argument("--max-records", type=int, default=None, help="Stop after this many matching publications.")
    parser.add_argument("--max-file-mb", type=float, default=None, help="Skip any individual file over this many MiB.")
    parser.add_argument(
        "--min-free-gb",
        type=float,
        default=2.0,
        help="Keep at least this many GiB free while downloading (default: 2).",
    )
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum delay between HTTP requests in seconds.")
    parser.add_argument("--timeout", type=float, default=60.0, help="Per-request read timeout in seconds.")
    parser.add_argument("--retries", type=int, default=4, help="Retries for transient HTTP failures.")
    parser.add_argument("--max-redirects", type=int, default=10, help="Maximum redirects per request.")
    parser.add_argument(
        "--journal",
        action="append",
        choices=("JCRE", "IREE"),
        default=[],
        help="Limit records to JCRE or IREE. Repeat to include both.",
    )
    parser.add_argument("--year-min", type=int, default=None, help="Keep records from this year or later.")
    parser.add_argument("--year-max", type=int, default=None, help="Keep records from this year or earlier.")
    parser.add_argument(
        "--allow-download-host",
        action="append",
        default=[],
        metavar="HOST",
        help=(
            "Allow a documented storage host used only as a redirect target for Journal Data uploads. "
            "External resources still are not selected for download."
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get(
            "JCRE_SCRAPER_USER_AGENT",
            f"jcre-replication-scraper/{__version__} (research inventory)",
        ),
        help="HTTP User-Agent string. It can also be set with JCRE_SCRAPER_USER_AGENT.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress informational console logging.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging and tracebacks.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.max_records is not None and args.max_records < 1:
        parser.error("--max-records must be at least 1")
    if args.max_file_mb is not None and args.max_file_mb < 0:
        parser.error("--max-file-mb cannot be negative")
    if args.min_free_gb < 0:
        parser.error("--min-free-gb cannot be negative")
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.retries < 0:
        parser.error("--retries cannot be negative")
    if args.max_redirects < 0:
        parser.error("--max-redirects cannot be negative")
    if args.year_min is not None and args.year_max is not None and args.year_min > args.year_max:
        parser.error("--year-min cannot be greater than --year-max")


def _configure_logging(output_dir: Path, quiet: bool, verbose: bool) -> logging.Logger:
    logs_dir = output_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("jcre_scraper")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    file_handler = logging.FileHandler(logs_dir / "run.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if not quiet:
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if verbose else logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(console)
    return logger


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    output_dir = args.output
    if output_dir is None:
        output_dir = Path("smoke-output" if args.smoke_test else "output")
    max_records = args.max_records
    if args.smoke_test and max_records is None:
        max_records = 3

    output_dir = output_dir.expanduser().resolve()
    logger = _configure_logging(output_dir, args.quiet, args.verbose)
    config = ScrapeConfig(
        output_dir=output_dir,
        source_url=args.source_url,
        download_files=bool(args.download_files),
        resume=bool(args.resume),
        refresh=bool(args.refresh),
        max_records=max_records,
        max_file_mb=args.max_file_mb,
        min_free_gb=args.min_free_gb,
        journal_codes=set(args.journal),
        year_min=args.year_min,
        year_max=args.year_max,
        extra_download_hosts={item.lower().strip(".") for item in args.allow_download_host if item.strip()},
    )

    try:
        with HttpClient(
            user_agent=args.user_agent,
            delay_seconds=args.delay,
            timeout_seconds=args.timeout,
            retries=args.retries,
            max_redirects=args.max_redirects,
        ) as http:
            scraper = JcreScraper(config, http, logger=logger)
            catalog = scraper.run()
        summary = catalog["summary"]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"Catalog: {output_dir / 'catalog.json'}")
        return 2 if summary.get("error_count_this_run", 0) else 0
    except KeyboardInterrupt:
        logger.error("Interrupted by user. Progress already written to the checkpoint can be resumed.")
        return 130
    except Exception as exc:
        logger.exception("Scrape failed: %s", exc) if args.verbose else logger.error("Scrape failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
