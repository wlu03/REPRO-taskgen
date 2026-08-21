#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# Dates used by the REPRO-Bench GPT-4o baseline. Override them with command-line
# flags or environment variables when collecting data for another model.
KNOWLEDGE_CUTOFF_DATE="${KNOWLEDGE_CUTOFF_DATE:-2023-10-01}"
MODEL_SNAPSHOT_DATE="${MODEL_SNAPSHOT_DATE:-2024-05-13}"
START_DATE="${START_DATE:-2024-05-14}"
END_DATE="${END_DATE:-$(date -u +%F)}"

OUTPUT_ROOT="${OUTPUT_ROOT:-}"
MODE_FLAG="--inventory-only"
MAX_RECORDS=""
RESUME=0
REFRESH=0
PLAN_ONLY=0

usage() {
  cat <<'EOF'
Usage: ./run_script.sh [options]

Run every REPRO-taskgen source scraper sequentially.

Options:
  --start-date YYYY-MM-DD          Candidate window start (default: 2024-05-14)
  --end-date YYYY-MM-DD            Candidate window end (default: today, UTC)
  --knowledge-cutoff-date DATE     Model knowledge cutoff metadata (default: 2023-10-01)
  --model-snapshot-date DATE       Model snapshot metadata (default: 2024-05-13)
  --output-root DIR                Root for this run's isolated source outputs
  --inventory-only                 Collect metadata only (default)
  --download-files                 Attempt public downloads (AEA needs manual setup)
  --max-records N                  Crawl/debug cap per source/community, before filtering
  --resume                         Reuse an already initialized compatible output
  --refresh                        Refresh cached metadata where supported
  --plan                           Print commands without running or writing files
  -h, --help                       Show this help

Date enforcement:
  Zenodo applies the exact inclusive date range to metadata.publication_date.
  JCRE applies the inclusive start/end years, so boundary years are coarse.
  AEA-ICPSR, CODECHECK, AJPS, Political Analysis, and World Bank currently
  inventory their full live catalogs; their retained record dates must be
  filtered during candidate extraction. This limitation is written to the run
  manifest as well as printed before each source runs. These discovery dates do
  not prove that both the exact package and its gold evidence are post-cutoff.
EOF
}

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 2
}

require_value() {
  [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || die "$1 requires a value"
}

trap 'printf "\nInterrupted; stopping the source run.\n" >&2; exit 130' INT
trap 'printf "\nTerminated; stopping the source run.\n" >&2; exit 143' TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start-date)
      require_value "$@"
      START_DATE="$2"
      shift 2
      ;;
    --end-date)
      require_value "$@"
      END_DATE="$2"
      shift 2
      ;;
    --knowledge-cutoff-date)
      require_value "$@"
      KNOWLEDGE_CUTOFF_DATE="$2"
      shift 2
      ;;
    --model-snapshot-date)
      require_value "$@"
      MODEL_SNAPSHOT_DATE="$2"
      shift 2
      ;;
    --output-root)
      require_value "$@"
      OUTPUT_ROOT="$2"
      shift 2
      ;;
    --inventory-only)
      MODE_FLAG="--inventory-only"
      shift
      ;;
    --download-files)
      MODE_FLAG="--download-files"
      shift
      ;;
    --max-records)
      require_value "$@"
      MAX_RECORDS="$2"
      shift 2
      ;;
    --resume)
      RESUME=1
      shift
      ;;
    --refresh)
      REFRESH=1
      shift
      ;;
    --plan)
      PLAN_ONLY=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

command -v python3 >/dev/null 2>&1 || die "python3 is required"

validate_date() {
  local label="$1"
  local value="$2"
  if ! python3 - "$value" <<'PY'
from datetime import date
import sys

try:
    date.fromisoformat(sys.argv[1])
except ValueError:
    raise SystemExit(1)
PY
  then
    die "${label} must be a real ISO date in YYYY-MM-DD form: ${value}"
  fi
}

validate_date "--knowledge-cutoff-date" "$KNOWLEDGE_CUTOFF_DATE"
validate_date "--model-snapshot-date" "$MODEL_SNAPSHOT_DATE"
validate_date "--start-date" "$START_DATE"
validate_date "--end-date" "$END_DATE"

[[ "$START_DATE" < "$END_DATE" || "$START_DATE" == "$END_DATE" ]] || \
  die "--start-date must not be later than --end-date"
[[ -z "$MAX_RECORDS" || "$MAX_RECORDS" =~ ^[1-9][0-9]*$ ]] || \
  die "--max-records must be a positive integer"

if [[ -z "$OUTPUT_ROOT" ]]; then
  OUTPUT_ROOT="${SCRIPT_DIR}/output/runs/${START_DATE}_to_${END_DATE}"
elif [[ "$OUTPUT_ROOT" != /* ]]; then
  OUTPUT_ROOT="${SCRIPT_DIR}/${OUTPUT_ROOT}"
fi

START_YEAR="${START_DATE:0:4}"
END_YEAR="${END_DATE:0:4}"
ZENODO_DATE_QUERY="metadata.publication_date:[${START_DATE} TO ${END_DATE}]"
RUN_STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

if [[ "$PLAN_ONLY" -eq 0 ]]; then
  if [[ -f "${OUTPUT_ROOT}/run_parameters.tsv" && "$RESUME" -eq 0 && "$REFRESH" -eq 0 ]]; then
    die "output already contains a run; use --resume, --refresh, or a different --output-root"
  fi
  mkdir -p -- "${OUTPUT_ROOT}/sources"
  {
    printf 'parameter\tvalue\n'
    printf 'run_started_at\t%s\n' "$RUN_STARTED_AT"
    printf 'knowledge_cutoff_date\t%s\n' "$KNOWLEDGE_CUTOFF_DATE"
    printf 'model_snapshot_date\t%s\n' "$MODEL_SNAPSHOT_DATE"
    printf 'candidate_start_date\t%s\n' "$START_DATE"
    printf 'candidate_end_date\t%s\n' "$END_DATE"
    printf 'mode\t%s\n' "${MODE_FLAG#--}"
    printf 'max_records\t%s\n' "${MAX_RECORDS:-unlimited}"
    printf 'resume\t%s\n' "$RESUME"
    printf 'refresh\t%s\n' "$REFRESH"
  } > "${OUTPUT_ROOT}/run_parameters.tsv"
  {
    printf 'source\tdate_basis\tenforcement\tstart_date\tend_date\n'
    printf 'aea-icpsr\tDATEUPDATED\tnot_filtered_at_crawl\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'codecheck\tcheck.date\tnot_filtered_at_crawl\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'harvard-ajps\tpublication_date\tnot_filtered_at_crawl\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'jcre\tarticle_year\tcoarse_inclusive_years\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'political-analysis\tpublished_at\tnot_filtered_at_crawl\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'world-bank\tyear\tnot_filtered_at_crawl\t%s\t%s\n' "$START_DATE" "$END_DATE"
    printf 'zenodo\tmetadata.publication_date\texact_inclusive\t%s\t%s\n' "$START_DATE" "$END_DATE"
  } > "${OUTPUT_ROOT}/date_policy.tsv"
  printf 'source\tstatus\texit_code\n' > "${OUTPUT_ROOT}/source_status.tsv"
fi

printf 'REPRO-taskgen source collection\n'
printf '  knowledge cutoff: %s\n' "$KNOWLEDGE_CUTOFF_DATE"
printf '  model snapshot:   %s\n' "$MODEL_SNAPSHOT_DATE"
printf '  candidate window: %s through %s (inclusive)\n' "$START_DATE" "$END_DATE"
printf '  output root:      %s\n' "$OUTPUT_ROOT"

if [[ "$MODE_FLAG" == "--download-files" ]]; then
  printf '  warning: AEA-ICPSR downloads may require credentials, Chromium, and a manual browser run.\n' >&2
fi

failures=()

run_source() {
  local name="$1"
  local output_flag="$2"
  local date_policy="$3"
  shift 3

  local runner="${SCRIPT_DIR}/source/${name}/run_scraper.sh"
  local source_output="${OUTPUT_ROOT}/sources/${name}"
  local args=("$MODE_FLAG" "$output_flag" "$source_output")

  [[ -z "$MAX_RECORDS" ]] || args+=("--max-records" "$MAX_RECORDS")
  if [[ "$RESUME" -eq 1 ]]; then
    if [[ "$name" == "zenodo" && "$PLAN_ONLY" -eq 0 && ! -d "$source_output" ]]; then
      printf '[%s] no prior checkpoint found; starting this source without --resume\n' "$name"
    else
      args+=("--resume")
    fi
  fi
  if [[ "$REFRESH" -eq 1 && "$name" != "aea-icpsr" ]]; then
    args+=("--refresh")
  fi
  args+=("$@")

  printf '\n[%s] date policy: %s\n' "$name" "$date_policy"
  printf '[%s] command:' "$name"
  printf ' %q' bash "$runner" "${args[@]}"
  printf '\n'

  if [[ "$PLAN_ONLY" -eq 1 ]]; then
    return 0
  fi

  local status=0
  if bash "$runner" "${args[@]}"; then
    printf '[%s] complete\n' "$name"
    printf '%s\tcomplete\t0\n' "$name" >> "${OUTPUT_ROOT}/source_status.tsv"
  else
    status=$?
    failures+=("${name}:${status}")
    printf '[%s] failed with exit code %s; continuing\n' "$name" "$status" >&2
    printf '%s\tfailed\t%s\n' "$name" "$status" >> "${OUTPUT_ROOT}/source_status.tsv"
  fi
}

run_source "aea-icpsr" "--output-root" \
  "full live inventory; filter retained DATEUPDATED downstream"
run_source "codecheck" "--output" \
  "full live inventory; filter retained check.date downstream"
run_source "harvard-ajps" "--output-dir" \
  "full live inventory; filter retained publication_date downstream"
run_source "jcre" "--output" \
  "coarse ${START_YEAR}-${END_YEAR} article-year filter" \
  "--year-min" "$START_YEAR" "--year-max" "$END_YEAR"
run_source "political-analysis" "--output-dir" \
  "full live inventory; filter retained published_at downstream"
run_source "world-bank" "--output-root" \
  "full live inventory; filter retained year downstream"
run_source "zenodo" "--output" \
  "exact inclusive metadata.publication_date filter" \
  "--all-journals" "--query" "$ZENODO_DATE_QUERY"

if [[ "$PLAN_ONLY" -eq 1 ]]; then
  printf '\nPlan complete; no scrapers were run and no files were written.\n'
  exit 0
fi

printf 'run_finished_at\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "${OUTPUT_ROOT}/run_parameters.tsv"

if [[ "${#failures[@]}" -gt 0 ]]; then
  printf '\nCompleted with %s failed source(s): %s\n' \
    "${#failures[@]}" "${failures[*]}" >&2
  exit 1
fi

printf '\nAll source scrapers completed successfully.\n'
