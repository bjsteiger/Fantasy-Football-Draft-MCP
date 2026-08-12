## What this changes

## Why

## If it touches the model

Model changes need evidence, not just reasoning. Show before-and-after on real players —
a board top-10, or a handful of affected projections. Several defaults in this project
exist because a plausible-sounding first attempt produced visibly wrong output.

## Checklist

- [ ] `pytest tests -q` passes
- [ ] `ruff check src tests` clean
- [ ] Added tests for new behaviour
- [ ] Updated docs if behaviour changed
- [ ] No credentials, cached data or `.parquet` files committed
