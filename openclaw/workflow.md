# OpenClaw Workflow

## Recommended execution order
1. `BLE_FTMS_AGENT`
2. `TEST_AGENT` (parsing validation)
3. `ENGINE_AGENT`
4. `CLI_AGENT`
5. `DOC_AGENT`

## Automatic validation
Run after each agent step:
- `flake8`
- `mypy`
- `pytest`

If validation fails, rerun the failing agent with relevant diff context.
