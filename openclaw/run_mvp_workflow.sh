#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

run_step() {
  local agent_name="$1"
  local prompt="$2"

  echo "[OpenClaw] Running ${agent_name}..."
  openclaw agent --local --thinking medium --message "$prompt"
  echo "[OpenClaw] ${agent_name} completed"
}

run_step "BLE_FTMS_AGENT" "You are BLE_FTMS_AGENT for velox-engine. Work only in backend/ble/ftms_client.py and backend/ble/constants.py. Implement scan, FTMS detection, connect, Indoor Bike Data subscribe, decode instantaneous power/cadence, set_target_power(watts). Async only. No CLI/business logic/printing. After changes run: .venv/bin/flake8 --jobs=1 backend tests && .venv/bin/mypy backend && .venv/bin/pytest -q"

run_step "TEST_AGENT" "You are TEST_AGENT for velox-engine. Work only in tests/test_ftms_parsing.py. Add/adjust tests for FTMS flags parsing and power decoding. After changes run: .venv/bin/flake8 --jobs=1 backend tests && .venv/bin/mypy backend && .venv/bin/pytest -q"

run_step "ENGINE_AGENT" "You are ENGINE_AGENT for velox-engine. Work only in backend/core/engine.py and backend/core/state.py. Build minimal async engine that uses FTMSClient public API only, updates state, prints metrics every second, clean stop. No BLE protocol internals. After changes run: .venv/bin/flake8 --jobs=1 backend tests && .venv/bin/mypy backend && .venv/bin/pytest -q"

run_step "CLI_AGENT" "You are CLI_AGENT for velox-engine. Work only in backend/cli/main.py. Implement terminal orchestration commands: python -m backend.cli.main --scan, --connect, --erg 200. No BLE protocol logic. Keep parser simple. After changes run: .venv/bin/flake8 --jobs=1 backend tests && .venv/bin/mypy backend && .venv/bin/pytest -q"

run_step "DOC_AGENT" "You are DOC_AGENT for velox-engine. Update README.md with Linux setup, BLE permissions, troubleshooting, and usage examples. Keep concise and practical. After changes run: .venv/bin/flake8 --jobs=1 backend tests && .venv/bin/mypy backend && .venv/bin/pytest -q"

echo "[OpenClaw] MVP workflow finished"
