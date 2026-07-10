<!-- See CONTRIBUTING.md for the full checklist and code-style notes. -->

## What

## Why

## How verified
<!-- e.g. `pytest tests/ -q` (N passing), `cus daemon --once --no-execute` against real state, a real swap smoke-test -->

## Checklist
- [ ] `python3 -c "import ast; ast.parse(open('cus.py').read())"` passes
- [ ] `cus.py --help` runs without import errors
- [ ] `pytest tests/ -q` passes (add tests for non-trivial changes)
- [ ] New config knobs have a `DEFAULT_CONFIG` default + `docs/RUNBOOK.md` mention
- [ ] Backward-compatible by default, or the breaking change is called out and justified
- [ ] Credential-touching changes preserve the atomic-write + backup discipline
