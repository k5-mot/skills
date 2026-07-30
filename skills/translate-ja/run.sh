#!/usr/bin/env bash
set -euo pipefail

show_usage() {
  cat <<'USAGE'
Usage:
  ./skills/translate-ja/run.sh --input ./docs/source/source.pdf [--output-dir ./docs/source/output] [--template ./skills/translate-ja/template.dotx] [--force]

Runs the translate-ja pipeline and skips each step when its expected output already exists.
USAGE
}

log() {
  printf '[translate-ja] %s\n' "$*"
}

run_step() {
  local label="$1"
  local output_path="$2"
  shift 2

  if [[ "${FORCE}" != "1" && -e "${output_path}" ]]; then
    log "skip ${label}: ${output_path} already exists"
    return 0
  fi

  log "run ${label}"
  "$@"
}

load_env_file() {
  local env_file="$1"

  if [[ -f "${env_file}" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "${env_file}"
    set +a
  fi
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

INPUT_PATH=""
OUTPUT_DIR=""
TEMPLATE_PATH="${SCRIPT_DIR}/template.dotx"
FORCE="0"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input|-i)
      INPUT_PATH="$2"
      shift 2
      ;;
    --output-dir|-o)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --template|-t)
      TEMPLATE_PATH="$2"
      shift 2
      ;;
    --force)
      FORCE="1"
      shift
      ;;
    --help|-h)
      show_usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      show_usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "${INPUT_PATH}" ]]; then
  INPUT_PATH="./docs/source/source.pdf"
fi

if [[ ! -f "${INPUT_PATH}" ]]; then
  printf 'Input file not found: %s\n' "${INPUT_PATH}" >&2
  exit 2
fi

INPUT_DIR="$(cd "$(dirname "${INPUT_PATH}")" && pwd)"
INPUT_FILE="$(basename "${INPUT_PATH}")"
INPUT_ABS="${INPUT_DIR}/${INPUT_FILE}"
STEM="${INPUT_FILE%.*}"

if [[ -z "${OUTPUT_DIR}" ]]; then
  OUTPUT_DIR="${INPUT_DIR}/output"
fi

mkdir -p "${OUTPUT_DIR}" "${OUTPUT_DIR}/artifacts" "${OUTPUT_DIR}/chunks-en" "${OUTPUT_DIR}/chunks-ja" "${OUTPUT_DIR}/reports" "${OUTPUT_DIR}/logs"

load_env_file "${REPO_ROOT}/.env"

BRONZE_JSON="${OUTPUT_DIR}/${STEM}.bronze.json"
SILVER_JSON="${OUTPUT_DIR}/${STEM}.silver.json"
GOLD_JSON="${OUTPUT_DIR}/${STEM}.gold.json"
CHUNKS_EN_JSONL="${OUTPUT_DIR}/chunks-en/chunks.source.jsonl"
CHUNKS_JA_JSONL="${OUTPUT_DIR}/chunks-ja/chunks.ja.jsonl"
JA_MD="${OUTPUT_DIR}/${STEM}.ja.md"
JA_DOCX="${OUTPUT_DIR}/${STEM}.ja.docx"
PREPROCESS_REPORT="${OUTPUT_DIR}/reports/preprocess_report.json"

PYTHON_BIN="${PYTHON_BIN:-python3}"
SCRIPTS_DIR="${SCRIPT_DIR}/scripts"
FORCE_ARGS=()
if [[ "${FORCE}" == "1" ]]; then
  FORCE_ARGS=(--force)
fi

run_step "1 preprocess document with Docling" "${BRONZE_JSON}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/preprocess_doc_with_docling.py" \
  --input "${INPUT_ABS}" \
  --output "${BRONZE_JSON}" \
  "${FORCE_ARGS[@]}"

run_step "2 realign document structure with LLM" "${SILVER_JSON}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/realign_doc_struct_with_llm.py" \
  --input "${BRONZE_JSON}" \
  --output "${SILVER_JSON}" \
  "${FORCE_ARGS[@]}"

run_step "3 clean Docling schema JSON" "${GOLD_JSON}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/clean_doc.py" \
  --input "${SILVER_JSON}" \
  --output "${GOLD_JSON}" \
  --report "${PREPROCESS_REPORT}" \
  "${FORCE_ARGS[@]}"

run_step "4 chunk Docling schema JSON" "${CHUNKS_EN_JSONL}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/chunk_docling_json.py" \
  --input "${GOLD_JSON}" \
  --output "${CHUNKS_EN_JSONL}" \
  "${FORCE_ARGS[@]}"

run_step "5 translate chunks" "${CHUNKS_JA_JSONL}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/translate_chunks.py" \
  --input "${OUTPUT_DIR}/chunks-en" \
  --output "${OUTPUT_DIR}/chunks-ja" \
  "${FORCE_ARGS[@]}"

run_step "6 concatenate translated chunks" "${JA_MD}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/concat_chunks.py" \
  --input "${OUTPUT_DIR}/chunks-ja" \
  --output "${JA_MD}" \
  "${FORCE_ARGS[@]}"

run_step "7 convert Markdown to docx" "${JA_DOCX}" \
  "${PYTHON_BIN}" "${SCRIPTS_DIR}/convert_md_to_docx_with_docling.py" \
  --input "${JA_MD}" \
  --output "${JA_DOCX}" \
  --template "${TEMPLATE_PATH}" \
  "${FORCE_ARGS[@]}"

log "done: ${JA_DOCX}"
