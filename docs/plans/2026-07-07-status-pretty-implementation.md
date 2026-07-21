# `cus status --pretty` Implementation Plan

> **For agentic workers:** Execute this plan task-by-task: run each task's full TDD cycle, have each task independently reviewed before moving to the next, and track steps via the checkbox (`- [ ]`) syntax.

**Goal:** Add a `--pretty` flag to `cus status` that renders a rich, color, width-adaptive one-shot view of the same data, per the approved spec `docs/plans/2026-07-07-status-pretty.md`.

**Architecture:** Extract the per-account display derivation from `status()` into a pure helper `_account_row()` shared by both renderers (plain output stays byte-identical). A new `_render_status_pretty(state, config)` builds rich tables sized to the terminal; all rich imports live inside it so daemon/statusline hot paths never import rich.

**Tech Stack:** Python ≥3.11 single-file uv script (`cus.py`), click, rich ≥13.0 (system python has 13.7.1), pytest 9 / standalone test runners.

## Global Constraints

- Plain `cus status` output stays **byte-identical** — existing tests (`tests/test_token_stale_5h_display.py`, `tests/test_disabled_accounts.py`, …) are the guard.
- Staleness markers keep their **literal characters**: `?` for unknown, trailing `~` for stale-known, in pretty mode too (2026-07-05 incident — load-bearing).
- No `rich` import at module top level of `cus.py` — only inside `_render_status_pretty`.
- uv header dependency line is exactly `"rich>=13.0",` (do not add pytest to runtime deps — CONTRIBUTING.md).
- New tests go in `tests/test_status_pretty.py`, standalone-runnable (`python3 tests/test_status_pretty.py`) **and** pytest-compatible, using the `_Env`-style monkeypatch pattern from `tests/test_token_stale_5h_display.py`.
- Full-suite check command: `python3 -m pytest tests/ -q` (pytest 9.0.3 is installed).
- All work happens in `/home/yaz/code/misc/claude-usage-swap`.

---

### Task 1: `_account_row()` — shared per-account derivation

**Files:**
- Modify: `cus.py` (helper inserted directly above the `@cli.command()` decorator of `def status()`, ~line 10651; plain loop body inside `status()`, ~lines 10682–10729)
- Test: `tests/test_status_pretty.py` (new)

**Interfaces:**
- Consumes: existing `_disabled_accounts(config) -> set`, `estimate_window_pct(acct, window, config, now=None) -> float`.
- Produces: `_account_row(name: str, acct: dict, config: dict, first_step: float) -> dict` with keys:
  - `flags: list[str]` — subset of `["DISABLED","TOKEN_EXPIRED","RATE_LIMITED","POLL_ERROR","TOKEN_STALE"]` in that order
  - `status_col: str` — `",".join(flags)` or `"ok"`
  - `est: dict[str, tuple[float, float]]` — `{"5h": (est_pct, rate), "7d": (...)}`, only windows where `est - polled >= 1.0`, insertion order 5h then 7d
  - `next_swap_pct: float | None` — `None` when equal to `first_step`
  - `per_model: list[tuple[str, float]]` — sorted highest-first
  - `last: str` — `last_swap_ts` or `"never"`
  Task 2's pretty renderer consumes exactly this dict.

- [ ] **Step 1: Write the failing unit tests**

Create `tests/test_status_pretty.py`:

```python
"""Tests for `cus status --pretty` (rich renderer) and the shared
`_account_row` derivation both renderers consume.

Spec: docs/plans/2026-07-07-status-pretty.md

Run standalone:  python3 tests/test_status_pretty.py
Or under pytest: pytest tests/test_status_pretty.py
"""

import json
import sys
import tempfile
from pathlib import Path

from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# --- _account_row: shared derivation ---------------------------------------


def test_account_row_flags_order_and_status_col():
    row = cus._account_row("a", {"token_stale": True, "rate_limited": True}, {}, 50)
    assert row["flags"] == ["RATE_LIMITED", "TOKEN_STALE"]
    assert row["status_col"] == "RATE_LIMITED,TOKEN_STALE"


def test_account_row_ok_when_no_flags():
    row = cus._account_row("a", {}, {}, 50)
    assert row["flags"] == []
    assert row["status_col"] == "ok"
    assert row["last"] == "never"


def test_account_row_disabled_comes_from_config():
    config = {"accounts": [{"name": "a", "disabled": True}, {"name": "b"}]}
    assert cus._account_row("a", {}, config, 50)["flags"] == ["DISABLED"]
    assert cus._account_row("b", {}, config, 50)["flags"] == []


def test_account_row_next_swap_only_when_off_first_step():
    assert cus._account_row("a", {"next_swap_at_pct": 75}, {}, 50)["next_swap_pct"] == 75
    assert cus._account_row("a", {"next_swap_at_pct": 50}, {}, 50)["next_swap_pct"] is None
    assert cus._account_row("a", {}, {}, 50)["next_swap_pct"] is None


def test_account_row_per_model_sorted_highest_first():
    acct = {"per_model_weekly_pct": {"sonnet": 10.0, "fable": 80.0}}
    assert cus._account_row("a", acct, {}, 50)["per_model"] == [("fable", 80.0), ("sonnet", 10.0)]


def test_account_row_est_only_on_divergence():
    # Deterministic despite now(): extrapolation is capped at
    # estimator.max_extrapolation_minutes (default 10), and the anchor ts is
    # far in the past — est = 50 + 2.0 * 10 = 70.
    acct = {
        "current_5h_pct": 50.0,
        "burn_rate_5h_pct_per_min": 2.0,
        "last_observed_ts": "2026-01-01T00:00:00Z",
    }
    row = cus._account_row("a", acct, {}, 50)
    assert row["est"] == {"5h": (70.0, 2.0)}
    # No burn rate measured → estimator falls back to polled → no est entry.
    assert cus._account_row("a", {"current_5h_pct": 50.0}, {}, 50)["est"] == {}


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: 6 failures, each `AttributeError: module 'cus' has no attribute '_account_row'`

- [ ] **Step 3: Add `_account_row` to cus.py**

Insert immediately **above** the `@cli.command()` decorator that precedes `def status() -> None:` (~line 10651):

```python
def _account_row(name: str, acct: dict, config: dict, first_step: float) -> dict:
    """Shared per-account display derivation for `status` (plain + --pretty).

    Pure data — no printing, no click.style — so both renderers consume the
    SAME flags/estimator/next-swap/per-model semantics and cannot drift.

    - flags: operator DISABLED (config, not polled state — surfaced so nobody
      wonders why the picker skips the account) + the polled health flags, in
      fixed order.
    - est: estimator (C) — extrapolated CURRENT usage per window, included
      only when it diverges >= 1.0pct from the last poll, so a fast-climbing
      account is visible between polls. Insertion order 5h then 7d matches
      the plain-output note order.
    - next_swap_pct: None while on the shared first ladder step (the status
      header already prints the ladder once as a legend); a value only for a
      mid-ladder / non-default account, the only case worth a per-row callout.
    - per_model: 7d-by-model, sorted highest-first so a model nearing its own
      weekly cap is the first thing the eye lands on.
    """
    flags = []
    if name in _disabled_accounts(config):
        flags.append("DISABLED")
    if acct.get("token_expired"):
        flags.append("TOKEN_EXPIRED")
    if acct.get("rate_limited"):
        flags.append("RATE_LIMITED")
    if acct.get("poll_error"):
        flags.append("POLL_ERROR")
    if acct.get("token_stale"):
        flags.append("TOKEN_STALE")
    est: dict = {}
    for win, key in (("5h", "current_5h_pct"), ("7d", "current_7d_pct")):
        polled = acct.get(key, 0.0)
        est_val = estimate_window_pct(acct, win, config)
        if est_val - polled >= 1.0:
            est[win] = (est_val, acct.get(f"burn_rate_{win}_pct_per_min", 0.0) or 0.0)
    nxt = acct.get("next_swap_at_pct", first_step)
    return {
        "flags": flags,
        "status_col": ",".join(flags) if flags else "ok",
        "est": est,
        "next_swap_pct": nxt if nxt != first_step else None,
        "per_model": sorted((acct.get("per_model_weekly_pct") or {}).items(),
                            key=lambda kv: -kv[1]),
        "last": acct.get("last_swap_ts") or "never",
    }
```

- [ ] **Step 4: Rewire the plain loop in `status()` to consume it**

Replace the block from `disabled = _disabled_accounts(config)` through the per-model sub-line `click.echo` (currently ~lines 10682–10729, ending just before the `click.echo()` blank line ahead of the Lanes section) with:

```python
    for name, a in sorted(state["accounts"].items()):
        row = _account_row(name, a, config, first_step)
        marker = " *" if name == state["active"] else ""
        est_note = "".join(f"  [{win} est {e:.0f}% @ +{rate:.1f}%/min]"
                           for win, (e, rate) in row["est"].items())
        nxt_note = (f"  [next-swap: {row['next_swap_pct']:.0f}%]"
                    if row["next_swap_pct"] is not None else "")
        click.echo(f"{name+marker:<20} {_fmt_pct(a, 'current_5h_pct', color_on=color_on)} {_fmt_pct(a, 'current_7d_pct', color_on=color_on)} {row['status_col']:<24} {row['last']:<28}{nxt_note}{est_note}")
        # Stale/unknown per-model treatment lives in _fmt_model_pct; which
        # models to show and their order comes from _account_row.
        if row["per_model"]:
            parts = "  ".join(f"{m}={_fmt_model_pct(a, p, color_on=color_on)}"
                              for m, p in row["per_model"])
            click.echo(f"{'':<20}   └ 7d by model: {parts}")
```

Notes for the implementer:
- The long explanatory comments currently inline in that block (estimator C, next-swap legend rationale, 2026-07-05 per-model staleness) move into `_account_row`'s docstring — do not duplicate them in both places.
- `f"{row['status_col']:<24}"` replaces `f"{status_col:<24}"`; output characters are identical.
- Delete the now-unused `disabled = _disabled_accounts(config)` line inside `status()`.

- [ ] **Step 5: Run the new tests and the full suite**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: 6 passed

Run: `python3 -m pytest tests/ -q`
Expected: all pass (plain-output fidelity; `test_token_stale_5h_display.py` and `test_disabled_accounts.py` are the sharpest guards)

- [ ] **Step 6: Commit**

```bash
git add cus.py tests/test_status_pretty.py
git commit -m "refactor: extract _account_row shared derivation from status()"
```

---

### Task 2: `--pretty` flag, rich dependency, header + accounts table

**Files:**
- Modify: `cus.py` (uv header lines 1–8; `status()` signature; new `_render_status_pretty` inserted directly below the end of `status()`)
- Test: `tests/test_status_pretty.py`

**Interfaces:**
- Consumes: `_account_row(...)` (Task 1), `_pct_is_unknown(acct, key)`, `_pct_is_stale_known(acct, key)`, `_model_pct_is_stale(acct)`, `_time_since(iso) -> float | None`, `_fmt_duration(seconds) -> str`, `list_login_families(account) -> list[str]`, `leased_families(account, state) -> set[str]`, `THRESHOLD_STEPS`.
- Produces: `_render_status_pretty(state: dict, config: dict) -> None` — prints header + accounts table (+ gate-OFF caption). Task 3 appends the lanes/locks/swaps sections **inside this same function**, after the accounts table. The closures `rel_ts()` and `width` defined here are reused by Task 3.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_status_pretty.py` (above `_run_all`):

```python
# --- --pretty: fixture env ---------------------------------------------------


class _PrettyEnv:
    """Throwaway state/config with every host-touching helper stubbed, so
    --pretty renders deterministically from the fixture alone."""

    STATE = {
        "active": "alpha",
        "accounts": {
            "alpha": {
                "current_5h_pct": 7.0,
                "current_7d_pct": 78.0,
                "last_swap_ts": "2026-01-01T00:00:00Z",
                "per_model_weekly_pct": {"Fable": 74.0},
            },
            "bravo": {
                "current_5h_pct": 82.0,
                "current_7d_pct": 30.0,
                "token_stale": True,
                "next_swap_at_pct": 75,
            },
            "charlie": {},
        },
        "swap_history": [
            {"ts": "2026-01-01T00:00:00Z", "from": "alpha", "to": "bravo",
             "trigger": "auto-ladder"},
        ],
        "slots": {"slot-1": {"account": "alpha"}},
    }
    CONFIG = (
        "mode: per_session\n"
        "accounts:\n"
        "  - name: alpha\n"
        "  - name: bravo\n"
        "  - name: charlie\n"
        "    disabled: true\n"
        "session_locks:\n"
        "  pinned:\n"
        "    sess-x: alpha\n"
    )

    def __init__(self, state: dict | None = None, config: str | None = None,
                 families: dict | None = None, with_slot_dir: bool = True,
                 sessions: list | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "state.json").write_text(json.dumps(state if state is not None else self.STATE))
        (root / "config.yaml").write_text(config if config is not None else self.CONFIG)
        self._families = families if families is not None else {"alpha": ["family-1", "family-2"]}
        self._sessions = sessions if sessions is not None else [
            cus.LiveSession(session_id="26f23a57beef", account="alpha",
                            pane="%1", cwd="/home/yaz/code/chabad-commons/GabAI",
                            started_at="2026-01-01T00:00:00Z",
                            last_stop_at=None, transcript_path=None),
            cus.LiveSession(session_id="5c0e416adead", account="alpha",
                            pane="%3", cwd="/home/yaz",
                            started_at="2026-01-01T00:00:00Z",
                            last_stop_at=None, transcript_path=None),
        ]
        slot_dirs = []
        if with_slot_dir:
            (root / "slot-1").mkdir()
            slot_dirs = [root / "slot-1"]
        stubs = {
            "STATE_JSON": root / "state.json",
            "CONFIG_YAML": root / "config.yaml",
            "diagnose": lambda state, config: [],
            "find_live_sessions": lambda *a, **k: self._sessions,
            "list_slot_dirs": lambda: slot_dirs,
            "mount_pids": lambda d: [11, 12] if d.name == "slot-1" else [],
            "pane_mount_name": lambda pane, tmux_socket=None: "slot-1" if pane == "%1" else None,
            "list_login_families": lambda a: self._families.get(a, []),
            "leased_families": lambda a, st: set(),
            "slot_leased_family": lambda st, slot: None,
            "has_independent_login": lambda a, slot: False,
            "list_provisioned_logins": lambda: [],
        }
        self._saved = {k: getattr(cus, k) for k in stubs}
        for k, v in stubs.items():
            setattr(cus, k, v)

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _pretty(env_kwargs: dict | None = None, columns: str = "160") -> str:
    env = _PrettyEnv(**(env_kwargs or {}))
    try:
        res = CliRunner().invoke(cus.status, ["--pretty"], env={"COLUMNS": columns},
                                 catch_exceptions=False)
        return res.output
    finally:
        env.restore()


# --- --pretty: header + accounts table --------------------------------------


def test_pretty_header_mode_and_ladder():
    out = _pretty()
    assert "per_session" in out
    assert "bare-launch: alpha" in out
    assert "50% → 75% → 90%" in out


def test_pretty_accounts_table_core():
    out = _pretty()
    assert "● alpha" in out                      # active marker replaces ' *'
    assert "Fable 74%" in out                    # per-model column
    assert " ago" in out                         # relative last swap
    assert "never" in out                        # bravo/charlie never swapped
    assert "█" in out                            # bars at 160 cols


def test_pretty_staleness_markers_survive():
    out = _pretty()
    assert "82%~" in out                         # stale-known 5h keeps ~
    assert "?" in out                            # unknown 7d keeps ?
    assert "TOKEN_STALE" in out


def test_pretty_disabled_and_next_swap():
    out = _pretty()
    assert "DISABLED" in out
    assert "next@75%" in out


def test_pretty_pool_column_and_gate_caption():
    out = _pretty()
    assert "2/2 free" in out
    assert "gate OFF" in out                     # pools exist, gate not enabled


def test_pretty_plain_mode_untouched_by_flag_addition():
    env = _PrettyEnv()
    try:
        out = CliRunner().invoke(cus.status, catch_exceptions=False).output
        assert "●" not in out
        assert "alpha *" in out                  # plain active marker survives
    finally:
        env.restore()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: the 6 Task-1 tests pass; the new tests fail with `UsageError`/`no such option: --pretty`

- [ ] **Step 3: Add the rich dependency to the uv header**

In `cus.py` lines 1–8, change the dependency list to:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.0",
#     "pyyaml>=6.0",
#     "rich>=13.0",
# ]
# ///
```

Then warm the uv cache immediately (first resolution needs the network):

Run: `cus --help >/dev/null && echo resolved`
Expected: `resolved` (works even against an uninitialized state; proves uv fetched rich)

- [ ] **Step 4: Add the flag and the renderer**

Change the `status` command signature (~line 10651):

```python
@cli.command()
@click.option("--pretty", is_flag=True, help="Rich, width-adaptive tables (color, usage bars).")
def status(pretty: bool) -> None:
    """Show active account, per-account usage state, locks, and recent activity."""
    if not STATE_JSON.exists():
        click.echo("Not initialized. Run `cus init` first.")
        sys.exit(1)

    state = read_json(STATE_JSON)
    config = load_config()
    if pretty:
        _render_status_pretty(state, config)
        return
    color_on = sys.stdout.isatty()
    ...  # everything below unchanged
```

Insert `_render_status_pretty` directly **after** the end of `status()` (before the `cus sessions` comment banner):

```python
def _render_status_pretty(state: dict, config: dict) -> None:
    """`cus status --pretty` — rich one-shot render, sized to the terminal.

    Spec: docs/plans/2026-07-07-status-pretty.md. Same data as plain status,
    restructured for scanning: bracket-notes become cells, login pools fold
    into a Pool column, Lanes+Live-sessions merge into one table.

    rich imports stay INSIDE this function so the daemon / statusline hot
    paths (and every non-pretty `cus` invocation) never pay for them.
    """
    from rich import box
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console(highlight=False)
    width = console.width
    show_bars = width >= 110      # spec breakpoints: bars only when roomy,
    fold_models = width < 80      # by-model folds under the account when tight

    mode = config.get("mode", "global")
    cfg_steps = config.get("thresholds", {}).get("steps") or THRESHOLD_STEPS[:-1]
    first_step = cfg_steps[0] if cfg_steps else 50
    last_step = cfg_steps[-1] if cfg_steps else 90

    if mode == "per_session":
        head = f"per_session · bare-launch: {state['active']}"
    elif mode == "hybrid":
        head = f"hybrid · shared-mount: {state['active']}"
    else:
        head = f"global · active: {state['active']}"
    ladder = " → ".join(f"{s}%" for s in cfg_steps)
    console.print(Text.assemble((head, "bold"), ("  ·  ladder ", "dim"), (ladder, "dim")))
    console.print()

    def rel_ts(iso: str | None) -> str:
        if not iso or iso == "never":
            return "never"
        secs = _time_since(iso)
        return iso if secs is None else _fmt_duration(secs) + " ago"

    def ladder_style(val: float) -> str:
        # Colors mean "how close to a swap": the ladder's own thresholds.
        if val >= last_step:
            return "red"
        if val >= first_step:
            return "yellow"
        return "green"

    def usage_cell(acct: dict, key: str, row: dict) -> Text:
        win = "5h" if key == "current_5h_pct" else "7d"
        if _pct_is_unknown(acct, key):
            return Text("?", style="dim")
        val = acct.get(key, 0.0)
        stale = _pct_is_stale_known(acct, key)
        style = "dim" if stale else ladder_style(val)
        t = Text()
        if show_bars:
            filled = min(8, max(0, round(val / 100 * 8)))
            t.append("█" * filled + "░" * (8 - filled) + " ", style=style)
        t.append(f"{val:.0f}%" + ("~" if stale else ""), style=style)
        if win in row["est"]:
            est_val, rate = row["est"][win]
            t.append(f" ↗{est_val:.0f} +{rate:.1f}/m", style="dim")
        return t

    def per_model_text(acct: dict, row: dict) -> Text:
        stale = _model_pct_is_stale(acct)
        t = Text()
        for i, (m, p) in enumerate(row["per_model"]):
            if i:
                t.append("  ")
            t.append(f"{m} {p:.0f}%" + ("~" if stale else ""),
                     style="dim" if stale else "")
        return t

    def status_cell(row: dict) -> Text:
        t = Text()
        if not row["flags"]:
            t.append("ok", style="green")
        else:
            for i, f in enumerate(row["flags"]):
                if i:
                    t.append(",")
                t.append(f, style="red dim" if f == "DISABLED" else "yellow")
        if row["next_swap_pct"] is not None:
            t.append(f" next@{row['next_swap_pct']:.0f}%", style="dim")
        return t

    pool_want = config.get("independent_logins", {}).get("pool_size", 3)

    def pool_cell(name: str) -> Text:
        fams = list_login_families(name)
        if not fams:
            return Text("")
        free = [f for f in fams if f not in leased_families(name, state)]
        return Text(f"{len(free)}/{len(fams)} free",
                    style="yellow" if len(fams) < pool_want else "")

    tbl = Table(box=box.SIMPLE_HEAD, pad_edge=False)
    tbl.add_column("Account", no_wrap=True)
    tbl.add_column("5h")
    tbl.add_column("7d")
    if not fold_models:
        tbl.add_column("7d by model")
    tbl.add_column("Status")
    tbl.add_column("Last swap")
    tbl.add_column("Pool")
    for name, a in sorted(state["accounts"].items()):
        row = _account_row(name, a, config, first_step)
        acct_cell = Text()
        active = name == state["active"]
        acct_cell.append("● " if active else "  ")
        acct_cell.append(name, style="bold" if active else "")
        pm = per_model_text(a, row)
        if fold_models and row["per_model"]:
            acct_cell.append("\n    ")
            acct_cell.append_text(pm)
        cells = [acct_cell,
                 usage_cell(a, "current_5h_pct", row),
                 usage_cell(a, "current_7d_pct", row)]
        if not fold_models:
            cells.append(pm)
        cells += [status_cell(row), Text(rel_ts(row["last"])), pool_cell(name)]
        tbl.add_row(*cells)
    console.print(tbl)

    il_on = config.get("independent_logins", {}).get("use_independent_logins", False)
    if not il_on and any(list_login_families(n) for n in state["accounts"]):
        console.print(Text("  (login-pool gate OFF — swaps still copy; "
                           "set use_independent_logins: true)", style="yellow"))
    console.print()
```

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: all pass

Run: `python3 -m pytest tests/ -q`
Expected: all pass (the `status(pretty)` signature change must not disturb plain-path tests — CliRunner invokes with no args, flag defaults False)

- [ ] **Step 6: Eyeball it on the real machine**

Run: `cus status --pretty`
Expected: header line, accounts table with bars/colors on the live state; no traceback. (Visual sanity only — the fixtures are the real gate.)

- [ ] **Step 7: Commit**

```bash
git add cus.py tests/test_status_pretty.py
git commit -m "feat: cus status --pretty — rich header + accounts table"
```

---

### Task 3: Lanes & sessions merged table, locks panel, recent swaps

**Files:**
- Modify: `cus.py` (append inside `_render_status_pretty`, after the gate-OFF caption block)
- Test: `tests/test_status_pretty.py`

**Interfaces:**
- Consumes: closures `rel_ts()`, `width`, `console`, `Text`, `Table`, `box` from Task 2's function body; existing `list_slot_dirs()`, `mount_pids(d)`, `pane_mount_name(pane, tmux_socket)`, `SLOT_PREFIX`, `_locked_slots(config)`, `_slot_pool(state, slot, config)`, `slot_leased_family(state, slot)`, `list_login_families(account)`, `has_independent_login(account, slot)`, `list_provisioned_logins()`, `find_live_sessions()`.
- Produces: the remaining pretty sections; no new module-level names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_status_pretty.py` (above `_run_all`):

```python
# --- --pretty: lanes & sessions, locks, swaps --------------------------------


def test_pretty_lane_row_merges_sessions():
    out = _pretty()
    assert "slot-1" in out
    assert "live 2 pids" in out
    assert "26f23a57" in out                    # session id on the lane row
    assert "GabAI" in out                       # cwd tail survives truncation
    assert "pool avail" in out                  # login annotation carried over


def test_pretty_bare_pseudo_lane():
    out = _pretty()
    assert "(bare)" in out
    assert "5c0e416a" in out                    # the %3 session groups under it


def test_pretty_locks_panel():
    out = _pretty()
    assert "Locks" in out
    assert "pinned: sess-x → alpha" in out


def test_pretty_recent_swaps():
    out = _pretty()
    assert "Recent swaps (1 of 1)" in out
    assert "alpha → bravo" in out
    assert "auto-ladder" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: the 4 new tests fail (`assert "slot-1" in out` etc.); all earlier tests pass

- [ ] **Step 3: Append the sections to `_render_status_pretty`**

Add after the gate-OFF caption block, still inside the function:

```python
    # --- Lanes & sessions (merged) ---------------------------------------
    # Mirrors the plain Lanes + Live-sessions logic (see status()); the lane
    # annotations are re-derived here from the same helpers rather than
    # extracted, per the spec's refactor boundary (_account_row only).
    per_session = mode == "per_session"
    slots_state = state.get("slots", {}) or {}
    slot_dirs = list_slot_dirs()
    live = find_live_sessions()
    machine_active = state.get("active", "?")
    hot_swap_on = config.get("hot_swap", {}).get("enabled", False)
    cwd_max = max(20, width - 60)

    def trunc_cwd(cwd: str) -> str:
        # Left-truncate: tails distinguish (home prefixes are shared).
        return cwd if len(cwd) <= cwd_max else "…" + cwd[-(cwd_max - 1):]

    buckets: dict = {}
    for s in live:
        if per_session:
            mnt = pane_mount_name(s.pane, s.tmux_socket)
            if mnt and mnt.startswith(SLOT_PREFIX):
                key, eff = mnt, (slots_state.get(mnt, {}).get("account") or s.account)
            elif mnt:  # account-dir launch (relogin flow)
                key, eff = f"mount:{mnt}", mnt.removeprefix("account-")
            else:
                key, eff = "(bare)", machine_active
        else:
            key = "(global)"
            eff = s.account if hot_swap_on else machine_active
        buckets.setdefault(key, []).append((s, eff))

    def session_lines(entries, show_acct: bool = False) -> Text:
        t = Text()
        for i, (s, eff) in enumerate(entries):
            if i:
                t.append("\n")
            t.append(f"{(s.session_id or '?')[:8]} {s.pane} ")
            if show_acct:
                t.append(f"{eff} ")
            if not per_session and eff != s.account:
                t.append(f"(start:{s.account}) ", style="dim")
            t.append(trunc_cwd(s.cwd), style="dim")
        return t

    locked = _locked_slots(config)
    il_cfg = config.get("independent_logins", {})
    il_flag = il_cfg.get("use_independent_logins", False)
    il_visible = (il_flag or bool(list_provisioned_logins())
                  or any(list_login_families(a) for a in state["accounts"]))
    default_pool = config.get("per_session", {}).get("default_pool", "premium")

    lane_rows = []
    seen = set()
    for d in slot_dirs:
        seen.add(d.name)
        entry = slots_state.get(d.name, {})
        acct = entry.get("account")
        pids = mount_pids(d)
        state_txt = Text(f"live {len(pids)} pids", style="green") if pids else Text("idle", style="dim")
        notes = []
        if d.name in locked:
            notes.append(("🔒locked", "yellow"))
        pool = _slot_pool(state, d.name, config)
        if pool != default_pool:
            notes.append((pool, "cyan"))
        if il_visible and acct:
            lease = slot_leased_family(state, d.name)
            if lease and lease[0] == acct:
                notes.append((f"pool {lease[1]}", "green"))
            elif list_login_families(acct):
                notes.append(("pool avail", "green"))
            elif has_independent_login(acct, d.name):
                notes.append(("indep-login ✓", "green"))
            elif il_flag:
                notes.append(("indep-login MISSING", "yellow"))
            else:
                notes.append(("copy", "cyan"))
        if d.name not in slots_state:
            notes.append(("orphan — not in state", "yellow"))
        notes_txt = Text(" · ").join(Text(n, style=st) for n, st in notes) if notes else Text("")
        lane_rows.append((Text(d.name), Text(acct or "(empty)"), state_txt,
                          notes_txt, session_lines(buckets.pop(d.name, []))))
    for name in sorted(set(slots_state) - seen):
        lane_rows.append((Text(name), Text(slots_state[name].get("account") or "(empty)"),
                          Text("state entry, dir missing", style="yellow"), Text(""), Text("")))
    for key in sorted(buckets):
        entries = buckets[key]
        if key == "(bare)":
            lane_rows.append((Text("(bare)", style="yellow"), Text(machine_active),
                              Text(f"{len(entries)} session(s)"),
                              Text("observe-only", style="yellow"),
                              session_lines(entries)))
        elif key.startswith("mount:"):
            lane_rows.append((Text(key.removeprefix("mount:")), Text(entries[0][1]),
                              Text(f"{len(entries)} session(s)"),
                              Text("account-dir mount", style="cyan"),
                              session_lines(entries)))
        else:  # (global) — background/hybrid modes
            acct_label = "(per-pane)" if hot_swap_on else machine_active
            lane_rows.append((Text("(global)"), Text(acct_label),
                              Text(f"{len(entries)} session(s)"), Text(""),
                              session_lines(entries, show_acct=hot_swap_on)))

    if lane_rows:
        lt = Table(box=box.SIMPLE_HEAD, pad_edge=False,
                   title="Lanes & sessions", title_justify="left", title_style="bold")
        lt.add_column("Lane", no_wrap=True)
        lt.add_column("Account")
        lt.add_column("State")
        lt.add_column("Notes")
        lt.add_column("Sessions")
        for r in lane_rows:
            lt.add_row(*r)
        console.print(lt)
        console.print()

    # --- Locks -------------------------------------------------------------
    pins = config.get("session_locks", {}).get("pinned", {}) or {}
    patterns = config.get("session_locks", {}).get("never_restart_patterns", []) or []
    locked_cfg = sorted(locked)
    if pins or patterns or locked_cfg:
        console.print(Text("Locks", style="bold"))
        for k, v in pins.items():
            console.print(f"  pinned: {k} → {v}")
        for s_ in locked_cfg:
            console.print(f"  locked slot: {s_}")
        for p in patterns:
            console.print(f"  never_restart: {p}")
        console.print()

    # --- Recent swaps --------------------------------------------------------
    history = state.get("swap_history", [])
    if history:
        st = Table(box=box.SIMPLE_HEAD, pad_edge=False,
                   title=f"Recent swaps ({min(5, len(history))} of {len(history)})",
                   title_justify="left", title_style="bold")
        st.add_column("When", no_wrap=True)
        st.add_column("Swap")
        st.add_column("Trigger")
        for e in history[-5:]:
            st.add_row(rel_ts(e["ts"]), f"{e['from']} → {e['to']}",
                       Text(e.get("trigger", "unknown"), style="dim"))
        console.print(st)
```

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: all pass

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 5: Eyeball on the real machine**

Run: `cus status --pretty`
Expected: full render — lanes with live sessions attached, locks (if any), recent swaps; compare content (not formatting) against plain `cus status`.

- [ ] **Step 6: Commit**

```bash
git add cus.py tests/test_status_pretty.py
git commit -m "feat: status --pretty lanes+sessions merged table, locks, swaps"
```

---

### Task 4: Width adaptation, section hiding, README

**Files:**
- Modify: `cus.py` (no code changes expected — this task *verifies* the `show_bars`/`fold_models`/`cwd_max` behavior already wired in Tasks 2–3 and fixes anything the width tests flush out)
- Modify: `README.md` (command reference, line ~89)
- Test: `tests/test_status_pretty.py`

**Interfaces:**
- Consumes: everything from Tasks 2–3.
- Produces: nothing new — regression coverage + docs.

- [ ] **Step 1: Write the failing/verifying tests**

Append to `tests/test_status_pretty.py` (above `_run_all`):

```python
# --- --pretty: width adaptation & section hiding -----------------------------


def _max_line_len(out: str) -> int:
    return max((len(l) for l in out.splitlines()), default=0)


def test_pretty_wide_has_bars():
    out = _pretty(columns="160")
    assert "█" in out or "░" in out
    assert _max_line_len(out) <= 160


def test_pretty_medium_drops_bars_keeps_model_column():
    out = _pretty(columns="100")
    assert "█" not in out and "░" not in out
    assert "7d by model" in out
    assert _max_line_len(out) <= 100


def test_pretty_narrow_folds_model_column():
    out = _pretty(columns="60")
    assert "7d by model" not in out
    assert "Fable 74%" in out                   # folded under the account name
    assert _max_line_len(out) <= 60


def test_pretty_minimal_state_hides_sections():
    minimal = {"active": "a", "accounts": {"a": {}}, "swap_history": []}
    out = _pretty({"state": minimal, "config": "mode: per_session\n",
                   "families": {}, "with_slot_dir": False, "sessions": []})
    assert "Lanes" not in out
    assert "Recent swaps" not in out
    assert "Locks" not in out
    assert "gate OFF" not in out
```

- [ ] **Step 2: Run the tests**

Run: `python3 -m pytest tests/test_status_pretty.py -q`
Expected: ideally all pass (the breakpoints were wired in Task 2). If a width assertion fails, fix the renderer — likely suspects: a `no_wrap=True` column overflowing at 60 cols (add `overflow="ellipsis"` to that column), or `cwd_max` too generous (tighten the `width - 60` constant). Re-run until green; do not weaken the `_max_line_len` assertions.

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: all pass

- [ ] **Step 4: Update README**

In `README.md`, directly below line 89 (`cus status                    # active + per-account state + live sessions`), add:

```
cus status --pretty           # same data, rich width-adaptive tables (color, usage bars)
```

- [ ] **Step 5: Final real-machine check at three widths**

Run (inside tmux, resize or use a narrow pane): `cus status --pretty`, `COLUMNS=100 cus status --pretty | head -40`, `COLUMNS=60 cus status --pretty | head -40`
Expected: three readable renders; piped output has no ANSI garbage.

- [ ] **Step 6: Commit**

```bash
git add cus.py tests/test_status_pretty.py README.md
git commit -m "feat: status --pretty width breakpoints verified + README"
```
