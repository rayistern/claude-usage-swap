"""Regression tests for the 8 confirmed findings from the 2026-07-02 sonnet
code review of the per_session branch (dogfood run on the pinned merkos slot).

Each test pins the behavior the fix established, so the bug can't silently
return. Findings, in review order:

  1. HIGH  _execute_swap_locked: empty-.claude.json substitution must key on
     `current is None` (empty-slot install), not `slot is None` — and must
     still READ an existing file to preserve non-account keys; an OCCUPIED
     slot with a missing .claude.json must RAISE, not write a {} snapshot.
  2. CRIT  acquire_slot reserves the chosen slot so a concurrent acquire can't
     claim it.
  3. CRIT  gc_slot refuses a freshly-reserved (in-flight-launch) slot.
  4. HIGH  scaffold_mount_dir mkdir is exist_ok (racing creators don't crash).
  5. HIGH  per-slot fan-out advances an account's ladder ONCE per cycle.
  6. HIGH  429 attribution uses the slot's CURRENT occupant, not the stale
     sessions.log launch-time account.
  7. HIGH  rename rewrites state.slots[*].account.
  8. MED   lazy-warm filtering resolves sessions by slot (pane→mount), not by
     the stale sessions.log account.

Run standalone:  python3 tests/test_review_fixes_20260702.py
Run under pytest: pytest tests/test_review_fixes_20260702.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from click.testing import CliRunner  # noqa: E402

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


class _Env:
    def __init__(self, accounts=("alpha", "beta", "gamma", "delta")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-bare")))
        self.claude_json.write_text(json.dumps({**_identity("bare"), "mcpServers": {"m": {}}}))
        for name in accounts:
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))
        cus.write_json(self.accounts_dir / "state.json", {
            "active": accounts[0],
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
            "swap_history": [],
        })
        (self.accounts_dir / "config.yaml").write_text("# keep\nmode: global\n")
        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON",
                        "CONFIG_YAML", "SESSIONS_LOG", "RATE_LIMIT_LOG", "DECISIONS_LOG", "INBOX_MD")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        cus.SESSIONS_LOG = self.accounts_dir / "sessions.log"
        cus.RATE_LIMIT_LOG = self.accounts_dir / "429.log"
        cus.DECISIONS_LOG = self.accounts_dir / "decisions.jsonl"
        cus.INBOX_MD = self.accounts_dir / "inbox.md"
        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        self._saved_scs = cus.session_current_slot
        self._saved_lsos = cus.live_sessions_on_slot
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def unreserve(self):
        st = cus.load_state()
        for e in st.get("slots", {}).values():
            e.pop("reserved_until", None)
        cus.save_state(st)

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus.session_current_slot = self._saved_scs
        cus.live_sessions_on_slot = self._saved_lsos
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


# --- Finding 1 -------------------------------------------------------------

def test_occupied_slot_missing_claude_json_raises_not_corrupts():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)  # current now alpha
        # Corruption: slot's live .claude.json vanishes while state says alpha.
        cus.mount_claude_json_path(d).unlink()
        snap = env.accounts_dir / "account-alpha" / ".claude.json"
        snap_before = snap.read_text()
        try:
            cus.execute_swap("beta", trigger="threshold", slot=name)
            raise AssertionError("expected FileNotFoundError, not a silent {} save-back")
        except FileNotFoundError:
            pass
        assert snap.read_text() == snap_before, "alpha snapshot must NOT be clobbered with {}"
    finally:
        env.restore()


def test_empty_slot_install_preserves_existing_nonaccount_keys():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        # Slot seeded with synced non-account keys, no identity yet (current None).
        cus.write_json(cus.mount_claude_json_path(d), {"mcpServers": {"x": {}}, "numStartups": 5})
        cus.execute_swap("alpha", trigger="launch", slot=name)
        cj = json.loads(cus.mount_claude_json_path(d).read_text())
        assert cj["mcpServers"] == {"x": {}}, "empty-slot install must keep synced keys"
        assert cj["userID"] == "uid-alpha"
    finally:
        env.restore()


# --- Findings 2 & 4: acquire reservation + racing create --------------------

def test_acquire_reserves_so_concurrent_acquire_gets_different_slot():
    env = _Env()
    try:
        # Two back-to-back acquires (no live PID yet on either) must not land
        # on the same slot — the first reserves it.
        n1, _ = cus.acquire_slot(cus.load_state())
        n2, _ = cus.acquire_slot(cus.load_state())
        assert n1 != n2, "reservation must stop a concurrent acquire reusing the slot"
    finally:
        env.restore()


def test_scaffold_mount_dir_is_exist_ok():
    env = _Env()
    try:
        d = env.accounts_dir / "slot-7"
        d.mkdir()
        # Second scaffold over an existing dir must not raise (racing creators).
        cus.scaffold_mount_dir(d)
        cus.scaffold_mount_dir(d)
        assert (d / "settings.json").is_symlink()
    finally:
        env.restore()


# --- Finding 3: gc refuses a reserved slot ---------------------------------

def test_gc_refuses_reserved_slot():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)  # fresh → reserved
        r = cus.gc_slot(name, cus.load_state())
        assert r["action"] == "refused_reserved", "in-flight launch must not be gc'd"
        env.unreserve()
        r = cus.gc_slot(name, cus.load_state())
        assert r["action"] == "reaped"
    finally:
        env.restore()


# --- Finding 5: ladder bumps once per account per cycle ---------------------

def test_fanout_bumps_ladder_once_per_account():
    env = _Env()
    try:
        # Two live slots on alpha; alpha over its 50% step.
        state = cus.load_state()
        n1, _ = cus.create_slot(state)
        n2, _ = cus.create_slot(state)
        env.unreserve()
        state = cus.load_state()
        for n in (n1, n2):
            d = env.accounts_dir / n
            (d / ".credentials.json").write_text(json.dumps(_creds("rt-alpha")))
            cus.write_json(d / ".claude.json", _identity("alpha"))
            state["slots"][n]["account"] = "alpha"
        state["accounts"]["alpha"].update({"current_5h_pct": 85.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 5.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 6.0, "current_7d_pct": 6.0})
        cus.save_state(state)
        env.live_slots.update({n1, n2})
        cus._OCCUPIED_SLOTS_CACHE.clear()

        cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
            "mode": "per_session", "strategy": "lowest_usage",
            "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
            "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        })
        usage = {"alpha": cus.AccountUsage(five_hour=cus.UsageWindow(85.0, None), seven_day=cus.UsageWindow(20.0, None)),
                 "beta": cus.AccountUsage(five_hour=cus.UsageWindow(5.0, None), seven_day=cus.UsageWindow(5.0, None)),
                 "gamma": cus.AccountUsage(five_hour=cus.UsageWindow(6.0, None), seven_day=cus.UsageWindow(6.0, None))}
        cus._per_session_cycle(cus.load_state(), cfg, usage, no_execute=False)

        st = cus.load_state()
        # Both slots moved off alpha, but the shared ladder advanced ONE rung
        # (50 -> 75), not two (50 -> 90).
        assert st["accounts"]["alpha"]["next_swap_at_pct"] == 75, \
            f"ladder should be 75, got {st['accounts']['alpha']['next_swap_at_pct']}"
        assert st["slots"][n1]["account"] != "alpha"
        assert st["slots"][n2]["account"] != "alpha"
    finally:
        env.restore()


# --- Finding 6: 429 attributed to slot's CURRENT occupant -------------------

def test_429_attributes_to_current_slot_occupant_not_stale_log():
    env = _Env()
    try:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        env.unreserve()
        state = cus.load_state()
        state["slots"][name]["account"] = "beta"  # slot was MOVED alpha->beta since launch
        cus.save_state(state)
        env.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        # sessions.log still says the launch-time account (alpha) — stale.
        from datetime import datetime, timedelta, timezone
        def _iso(m): return (datetime.now(timezone.utc) - timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
        cus.SESSIONS_LOG.write_text(f"{_iso(30)},sX,alpha,%3,/tmp\n")
        cus.RATE_LIMIT_LOG.write_text(f"{_iso(1)},sX,429\n")
        state = cus.load_state()
        state["last_429_check_ts"] = _iso(5)
        # session sX currently runs on `name` (which now holds beta).
        cus.session_current_slot = lambda sid: name if sid == "sX" else None
        # PR #187 incorporation (safety fix 1): reactive ships OFF in DEFAULT_CONFIG;
        # enable it explicitly for this current-slot-attribution regression test.
        cfg = cus.deep_merge(cus.DEFAULT_CONFIG,
                             {"mode": "per_session", "swap_hysteresis": {"enabled": False},
                              "reactive": {"enabled": True}})
        moves = cus.check_rate_limit_reactive_per_session(state, cfg)
        assert len(moves) == 1
        assert moves[0]["from"] == "beta", "must attribute to the slot's CURRENT account, not stale alpha"
        assert moves[0]["slot"] == name
    finally:
        env.restore()


# --- Finding 7: rename rewrites slot occupancy ------------------------------

def test_rename_rewrites_slot_account():
    env = _Env()
    try:
        state = cus.load_state()
        name, _ = cus.create_slot(state)
        env.unreserve()
        state = cus.load_state()
        state["slots"][name]["account"] = "alpha"
        cus.save_state(state)
        runner = CliRunner()
        r = runner.invoke(cus.cli, ["rename", "alpha", "alpha2"])
        assert r.exit_code == 0, r.output
        st = cus.load_state()
        assert st["slots"][name]["account"] == "alpha2", "slot occupant must follow the rename"
        assert "alpha" not in st["accounts"] and "alpha2" in st["accounts"]
    finally:
        env.restore()


# --- Finding 8: lazy-warm resolves by slot, not stale account ---------------

def test_lazy_warm_resolves_by_slot_after_move():
    env = _Env()
    try:
        # Move dict says from=B (slot's current account). sessions.log would
        # still say A, so an account_filter would miss — the fix resolves by
        # slot via live_sessions_on_slot.
        called = {}
        class _S:
            transcript_path = "/tmp/x"
            pane = "%1"
        def fake_lsos(slot_name):
            called["slot"] = slot_name
            return [_S()]
        cus.live_sessions_on_slot = fake_lsos
        cus.cache_warm = lambda tp, w: True
        cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {"lazy_swap": {"enabled": True}, "hot_swap": {"enabled": False}})
        move = {"slot": "slot-1", "from": "B", "to": "C", "deferrable": True}
        warm = cus._lazy_warm_slot_sessions(move, cfg)
        assert called.get("slot") == "slot-1", "must query by SLOT, not by stale account"
        assert len(warm) == 1
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
