# Contributing to `cus`

`cus` is a single-file Python tool with a deliberately small dependency footprint. Most contributions touch `cus.py`.

## Quick start

```bash
git clone https://github.com/rayistern/claude-usage-swap
cd claude-usage-swap
# Verify it parses + runs on your machine
python3 -c "import ast; ast.parse(open('cus.py').read())"
python3 cus.py --help
```

Dependencies: Python 3.11+, `click`, `pyyaml`. If you have `uv`, `uv run cus.py` resolves dependencies from PEP 723 inline metadata automatically.

## Code style

- **Single file**: `cus.py` is monolithic on purpose. Section headers (`# --- Foo ---`) separate concerns; navigate by them. ~2000 LOC fits comfortably; no need to split.
- **Comprehensive docstrings on functions** — every public function explains *why*, not just *what*. Reasoning is the load-bearing artifact.
- **Atomic IO**: any write to `~/claude-accounts/` or `~/.claude/` uses `atomic_write_bytes()` (tempfile + `os.replace`). Don't reach for `open(path, 'w')` directly; use `write_json` / `write_yaml`.
- **No new external dependencies** without a strong reason. The whole point of single-file PEP 723 is "scp it to any machine and it works."
- **Type hints** on function signatures. Use `from __future__ import annotations` at module top.

## Testing

There is a pytest suite under `tests/` (run `pytest tests/ -q`; CI runs it on every push/PR across Python 3.11–3.13). Each test file is also runnable standalone (`python3 tests/test_foo.py`) and fakes `HOME`/account dirs via a `_Env` helper that monkeypatches the module path constants — follow that pattern for new tests so they stay hermetic and don't touch the real `~/claude-accounts/`.

`pytest` is a **dev-only** dependency — do NOT add it to `cus.py`'s PEP 723 runtime metadata.

For changes to the swap mechanism or the daemon loop, also smoke-test against real state before opening the PR:

```bash
python3 cus.py init --dry-run
python3 cus.py poll --no-write
python3 cus.py daemon --once --no-execute
python3 cus.py sos
```

## Lifted methodology

Several patterns are lifted from [cux](https://github.com/inulute/cux) (Go). When you touch the lifted parts, preserve the `# Lifted from cux/<file>:<line>` attribution comments. Specifically:

- OAuth usage API request (`poll_account_usage`)
- Strategy picker logic (`pick_swap_target`)
- Hook-based turn-boundary signaling (`Stop` hook + `wait_for_stop`)
- PostToolUseFailure 429 substring-match
- `--resume <id> "Continue."` wake-up pattern

If you reimplement these substantially, retain at minimum a `# Methodology lifted from cux/<file>` note.

## Architectural decisions

Read `inbox.md` for the major ARCH DECISION entries — these are non-obvious choices with documented walk-back paths. Before you change something covered by an entry, read the entry first.

Big ones:
- **Lift cux patterns in Python, don't fork the Go binary** — single-file Python over a Go process wrapper
- **File-copy swap, not `CLAUDE_CONFIG_DIR` env-var swap** — `~/.claude/` is always the active mount point
- **Direct OAuth usage API, not ccusage** — ccusage doesn't track plan caps as percentages
- **Single-file (~2000 LOC) over multi-module package** — single-file is the deployment story
- **Unified tree at `~/claude-accounts/`** — each account is a full `CLAUDE_CONFIG_DIR`

## PR checklist

- [ ] `cus.py` parses (`python3 -c "import ast; ast.parse(open('cus.py').read())"`)
- [ ] `cus.py --help` runs without import errors
- [ ] `pytest tests/ -q` passes; non-trivial changes add a test
- [ ] If you added a command, it shows in `--help`
- [ ] If you added a config knob, it has a default in `DEFAULT_CONFIG` and is documented in `docs/RUNBOOK.md`
- [ ] If you changed the swap mechanism, you smoke-tested it (`cus switch <name> --dry-run` and a real swap)
- [ ] If you added a hook, the bash script is in `hooks/` and the installer registers it
- [ ] You ran `cus sos` and it correctly diagnoses any new failure modes you introduced
- [ ] Documented why the change is needed (PR description, not just "fixed bug")
- [ ] If you made an architecture decision, added an inbox entry with a walk-back path

## Filing issues

See `docs/TROUBLESHOOTING.md` for the format. Redact credentials.

## License

MIT (see `LICENSE`). By contributing you agree to release your contributions under the same license.
