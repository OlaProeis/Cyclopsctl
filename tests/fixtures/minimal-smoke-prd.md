# PRD: Cyclopsctl smoke test project

## Overview

Minimal project to validate the two-command adoption path: `cyclopsctl init` then `cyclopsctl launch`.

## Tech Stack

- Python 3.10+
- pytest for testing

## Requirements

1. Create `test-run/output-1.txt` containing exactly `smoke-test-1`.
2. Create `test-run/output-2.txt` containing exactly `smoke-test-2`.
3. Create `test-run/output-3.txt` containing exactly `smoke-test-3`.

Each task must only create its own file; do not modify prior outputs.

## Testing

```bash
python -m pytest
```
