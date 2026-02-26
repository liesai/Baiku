# OpenClaw Agents

## Agent: BLE_FTMS_AGENT
**Mission**
Implement a BLE FTMS client compatible with Elite Direto XRT using `bleak`.

**Responsibilities**
- Scan BLE devices.
- Detect FTMS service UUID.
- Connect to selected trainer.
- Subscribe to Indoor Bike Data notifications.
- Decode instantaneous power.
- Implement `set_target_power(watts: int)`.

**Constraints**
- Async-only.
- No CLI logic.
- No business logic.
- No terminal printing.

**Allowed files**
- `backend/ble/ftms_client.py`
- `backend/ble/constants.py`

## Agent: ENGINE_AGENT
**Mission**
Create a minimal async runtime that:
- starts BLE connection,
- receives power metrics,
- updates state,
- prints live metrics every second.

**Constraints**
- No internal BLE protocol logic.
- Use only `FTMSClient` public API.

**Allowed files**
- `backend/core/engine.py`
- `backend/core/state.py`

## Agent: CLI_AGENT
**Mission**
Build a minimal terminal interface:
- `python -m backend.cli.main --scan`
- `python -m backend.cli.main --connect`
- `python -m backend.cli.main --erg 200`

**Constraints**
- No BLE protocol logic.
- No complex parsing.
- Orchestration only.

**Allowed files**
- `backend/cli/main.py`

## Agent: TEST_AGENT
**Mission**
Create unit tests for:
- FTMS flag parsing,
- power decoding.

**Allowed files**
- `tests/test_ftms_parsing.py`

## Agent: DOC_AGENT
**Mission**
Generate:
- `README.md`
- Linux setup instructions
- BLE permissions guidance
- troubleshooting section
