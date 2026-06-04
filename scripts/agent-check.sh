#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PROFILE="targeted"
RUN_SYNC=false

usage() {
  cat <<'EOF'
Usage:
  scripts/agent-check.sh [options]

Options:
  --profile <targeted|full>  Verification profile. Default: targeted
  --sync                     Run uv sync before checks
  -h, --help                 Show this help message
EOF
}

while (($# > 0)); do
  case "$1" in
    --profile)
      PROFILE="${2:-}"
      if [[ "$PROFILE" != "targeted" && "$PROFILE" != "full" ]]; then
        echo "Unsupported profile: $PROFILE" >&2
        exit 1
      fi
      shift 2
      ;;
    --sync)
      RUN_SYNC=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
done

mkdir -p data/plugins data/config data/temp data/skills
export TESTING="${TESTING:-true}"
export ZHIPU_API_KEY="${ZHIPU_API_KEY:-test-api-key}"
export PYTHONPATH="$ROOT_DIR:$ROOT_DIR/dc_engines:${PYTHONPATH:-}"

if [[ "$RUN_SYNC" == true ]]; then
  echo "==> Syncing dependencies"
  uv sync --group dev
fi

echo "==> Checking repository hygiene"
uv run python scripts/check_clean.py

echo "==> Checking test closure"
uv run python scripts/check_test_closure.py

echo "==> Validating knowledge-base import contract"
uv run python -m harness.evaluator.kb_import_contract
uv run python -m harness.evaluator.kb_import_contract --contract harness/contracts/truth_intake_runtime.json
uv run python -m harness.evaluator.kb_import_contract --contract harness/contracts/client_content_sop.json
uv run python -m harness.evaluator.kb_import_contract --contract harness/contracts/planning_content_sop.json
uv run python -m harness.evaluator.kb_import_contract --contract harness/contracts/media_generation_sop.json
uv run python -m harness.evaluator.kb_import_contract --contract harness/contracts/content_sop_system.json

echo "==> Validating Feishu card system contract"
scripts-tools/card-system-health.py

echo "==> Running targeted harness tests"
uv run pytest \
  tests/harness/test_kb_import_contract.py \
  tests/harness/test_content_sop_contracts.py \
  tests/harness/truth_intake_runtime_contract_test.py \
  tests/harness/test_test_closure.py \
  tests/test_kb_import.py::test_import_documents \
  tests/test_kb_import.py::test_import_documents_returns_friendly_failure_message \
  tests/test_kb_import.py::test_import_documents_passes_source_path_to_kb_helper \
  tests/test_kb_import.py::test_upload_document_persists_source_path_as_file_path \
  tests/unit/test_kb_source_citations.py \
  tests/unit/test_astr_main_agent.py::TestApplyKb::test_apply_kb_without_agentic_mode \
  tests/unit/test_astr_main_agent.py::TestApplyKb::test_knowledge_base_tool_description_requires_source_citations \
  tests/unit/test_kb_source_citations.py::test_formatted_context_includes_obsidian_wikilink_for_source_document \
  -q

echo "==> Running truth-intake runtime harness checks"
uv run pytest \
  tests/test_computer_fs_tools.py::test_restricted_member_can_read_active_truth_intake_archive \
  tests/test_computer_fs_tools.py::test_restricted_member_grep_defaults_include_active_truth_intake_archive \
  tests/test_computer_fs_tools.py::test_restricted_member_broad_data_grep_narrows_to_active_truth_intake \
  tests/test_computer_fs_tools.py::test_restricted_member_still_cannot_read_unrelated_data_path \
  tests/test_harness_state_injector.py \
  -q
uv run pytest data/plugins/llm_router/test_dc_router_path.py -q
uv run pytest dc_engines/tests -q

if [[ "$PROFILE" == "full" ]]; then
  echo "==> Running format check"
  uv run ruff format --check .

  echo "==> Running lint check"
  uv run ruff check .

  echo "==> Running full pytest suite"
  uv run pytest tests --cov=. -v -o log_cli=true -o log_level=DEBUG

  echo "==> Running startup smoke check"
  uv run python scripts/smoke_startup_check.py
fi

echo "==> Agent checks completed successfully"
