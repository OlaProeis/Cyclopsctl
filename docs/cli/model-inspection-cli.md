# Model inspection CLI

`cyclopsctl models` is a setup-time diagnostic that lists account model ids and reports which routing presets the cyclopsctl will use. It does not affect runtime routing.

## Command

```bash
cyclopsctl models
```

Uses `CURSOR_API_KEY` from the environment when set. Exit code **1** if `Cursor.models.list()` fails; **0** on success.

## Implementation

| Symbol | Module | Role |
|--------|--------|------|
| `fetch_model_inventory` | `models.py` | Calls SDK list + `discover_model_capabilities` |
| `format_models_diagnostic` | `models.py` | Human-readable inventory and routing table |
| `run_models_inspection` | `cli.py` | Testable entry point for the subcommand |

Output includes every model id, display name, variant params, the resolved Composer preset (complexity 1–8), Opus high-thinking preset (9–10) when available, and fallback behavior when Opus is unavailable.

## Errors

`ModelListingError` — wraps `CursorAgentError` from failed SDK listing.

Tests: `tests/test_cli_models.py`, format/fetch cases in `tests/test_models.py`.

Related: `docs/runtime/model-discovery.md` (preset detection heuristics).
