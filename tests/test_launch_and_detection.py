"""Tests for Phases 2.2 + 2.3: cus launch preparation and session-side
slot/account detection.

Plan: docs/plans/2026-07-02-per-session-accounts.md.

Run standalone:  python3 tests/test_launch_and_detection.py
Run under pytest: pytest tests/test_launch_and_detection.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


class _Env:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"

        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-gamma")))
        self.claude_json.write_text(json.dumps({**_identity("gamma"), "mcpServers": {"m": {}}}))

        for name in ("alpha", "beta", "gamma"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        cus.write_json(self.accounts_dir / "state.json", {
            "active": "gamma",
            # alpha lightly used, beta heavier — auto-pick should prefer alpha
            "accounts": {
                "alpha": {"next_swap_at_pct": 50, "current_5h_pct": 10.0, "current_7d_pct": 10.0},
                "beta": {"next_swap_at_pct": 50, "current_5h_pct": 60.0, "current_7d_pct": 40.0},
                "gamma": {"next_swap_at_pct": 50, "current_5h_pct": 5.0, "current_7d_pct": 5.0},
            },
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []
        self._saved_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        if self._saved_env is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved_env
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self._tmp.cleanup()


def test_pick_launch_account_spreads_over_occupied():
    env = _Env()
    try:
        config = cus.load_config()
        state = cus.load_state()
        # gamma is global-active (occupied by bare sessions); nothing slotted:
        # gamma would win on usage but is excluded on the spreading pass.
        t = cus.pick_launch_account(state, config)
        assert t is not None and t.name == "alpha"

        # alpha now occupies a slot too → next launch lands on beta.
        state["slots"] = {"slot-1": {"account": "alpha"}}
        t = cus.pick_launch_account(state, config)
        assert t is not None and t.name == "beta"

        # Everything occupied → doubling up allowed (second pass).
        state["slots"]["slot-2"] = {"account": "beta"}
        t = cus.pick_launch_account(state, config)
        assert t is not None, "occupied-everywhere still yields an account"
    finally:
        env.restore()


def test_acquire_slot_prefers_matching_then_free_then_create():
    env = _Env()
    try:
        state = cus.load_state()
        n1, _ = cus.create_slot(state)
        n2, _ = cus.create_slot(state)
        # acquire_slot reloads state from disk under the lock, so assign
        # accounts + clear the just-created reservations ON DISK (the slots are
        # idle in this test — no live launch to protect).
        state = cus.load_state()
        state["slots"][n1].update({"account": "beta"})
        state["slots"][n2].update({"account": "alpha"})
        for e in state["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(state)

        name, _ = cus.acquire_slot(state, prefer_account="alpha")
        assert name == n2, "free slot already holding the account wins (no swap needed)"

        # Occupied slots are skipped; none free → new slot created. (Also clear
        # the reservation acquire just put on n2 so it's not the reason.)
        st = cus.load_state()
        for e in st["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(st)
        cus.mount_pids = lambda mount: [1]
        name, d = cus.acquire_slot(cus.load_state(), prefer_account="alpha")
        assert name == "slot-3"
        cus.mount_pids = lambda mount: []
    finally:
        env.restore()


def test_launch_prepare_full_flow():
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir, account = cus._launch_prepare("alpha", state, config)
        assert account == "alpha"
        assert (slot_dir / "settings.json").is_symlink(), "slot healed/scaffolded"
        cj = json.loads((slot_dir / ".claude.json").read_text())
        assert cj["userID"] == "uid-alpha", "identity installed"
        assert cj["mcpServers"] == {"m": {}}, "canonical non-account keys synced"
        creds = json.loads((slot_dir / ".credentials.json").read_text())
        assert creds["claudeAiOauth"]["refreshToken"] == "rt-alpha"
        st = cus.load_state()
        assert st["slots"][slot_name]["account"] == "alpha"
        assert st["slots"][slot_name].get("last_launch_ts")
        assert st["active"] == "gamma", "global mount untouched"

        # Relaunching the same account reuses the same slot without a swap —
        # but only once the prior launch's reservation has lapsed (a slot
        # claimed <120s ago is deliberately NOT reused, so concurrent launches
        # don't collide). Simulate the reservation expiring (session came and
        # went idle) by clearing it on disk.
        st = cus.load_state()
        for e in st["slots"].values():
            e.pop("reserved_until", None)
        cus.save_state(st)
        slot_name2, _, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name2 == slot_name
    finally:
        env.restore()


def test_launch_prepare_rejects_unknown_account():
    env = _Env()
    try:
        import click
        state = cus.load_state()
        config = cus.load_config()
        try:
            cus._launch_prepare("nope", state, config)
            raise AssertionError("expected ClickException")
        except click.ClickException:
            pass
    finally:
        env.restore()


def _break_slot_projects(slot_dir, shared_projects, divergent: bool):
    """Turn a slot's projects/ into the GH #192 incident shape: a REAL dir
    whose per-project child collides with the shared tree. divergent=True
    plants a same-path file with DIFFERENT bytes (unhealable — operator
    judgment); divergent=False plants only a unique stray transcript
    (healable by the recursive merge)."""
    link = slot_dir / "projects"
    if link.is_symlink():
        link.unlink()
    proj = link / "-home-user-repo"
    proj.mkdir(parents=True)
    (shared_projects / "-home-user-repo").mkdir(exist_ok=True)
    (proj / "sess-stray.jsonl").write_text("stray")
    if divergent:
        (proj / "sess-clash.jsonl").write_text("slot-version")
        (shared_projects / "-home-user-repo" / "sess-clash.jsonl").write_text("shared-version")


def test_launch_prepare_repairs_broken_projects_slot():
    """GH #192 bug 1+2: a slot whose projects/ is a real dir (colliding with
    the shared tree by project-dir NAME) must be actually repaired by the
    launch pre-flight — transcript reunited with the shared tree, dir
    relinked — instead of exec'ing claude against an isolated projects/
    ("No conversation found" on --resume)."""
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)  # slot is idle, make it acquirable
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=False)

        slot_name, slot_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name == name, "healable slot is repaired and USED, not skipped"
        assert (slot_dir / "projects").is_symlink()
        assert (slot_dir / "projects").resolve() == (env.claude_dir / "projects").resolve()
        assert (env.claude_dir / "projects" / "-home-user-repo" / "sess-stray.jsonl").read_text() == "stray", \
            "stray transcript merged into the shared tree"
    finally:
        env.restore()


def test_launch_prepare_skips_unhealable_projects_slot():
    """GH #192: when the heal genuinely cannot complete (divergent same-path
    file content), auto-selection must EXCLUDE the broken slot and land on
    one whose projects/ resolves to the shared tree — never exec onto the
    isolated dir. The broken slot's divergent data is left intact."""
    env = _Env()
    try:
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=True)

        slot_name, slot_dir, _ = cus._launch_prepare("alpha", cus.load_state(), config)
        assert slot_name != name, "unhealable slot must be skipped in auto selection"
        assert (slot_dir / "projects").resolve() == (env.claude_dir / "projects").resolve(), \
            "chosen slot's projects/ verified against the shared tree pre-exec"
        assert (d / "projects" / "-home-user-repo" / "sess-clash.jsonl").read_text() == "slot-version", \
            "divergent content preserved on the abandoned slot (never destroyed)"
    finally:
        env.restore()


def test_launch_prepare_lane_refuses_unhealable_projects():
    """GH #192: an EXPLICIT --lane onto an unhealable slot fails loudly (the
    operator pinned it on purpose — landing elsewhere silently would be
    worse), instead of exec'ing a session forked off the shared tree."""
    env = _Env()
    try:
        import click
        state = cus.load_state()
        config = cus.load_config()
        name, d = cus.create_slot(state)
        st = cus.load_state()
        st["slots"][name].pop("reserved_until", None)
        cus.save_state(st)
        _break_slot_projects(d, env.claude_dir / "projects", divergent=True)

        try:
            cus._launch_prepare("alpha", cus.load_state(), config, lane=name)
            raise AssertionError("expected ClickException for unhealable --lane projects/")
        except click.ClickException as e:
            assert "GH #192" in str(e.message)
    finally:
        env.restore()


def test_mount_account_from_env():
    env = _Env()
    try:
        state = cus.load_state()
        state["slots"] = {"slot-1": {"account": "alpha"}}

        # Unset → bare launch.
        assert cus.mount_account_from_env(state) == (None, None)

        # Slot dir → slot's current occupant (state-resolved, per render).
        os.environ["CLAUDE_CONFIG_DIR"] = str(env.accounts_dir / "slot-1")
        assert cus.mount_account_from_env(state) == ("slot-1", "alpha")
        state["slots"]["slot-1"]["account"] = "beta"  # swap moved the slot
        assert cus.mount_account_from_env(state) == ("slot-1", "beta")

        # Account dir → the account itself (relogin-style launch).
        os.environ["CLAUDE_CONFIG_DIR"] = str(env.accounts_dir / "account-merkos")
        assert cus.mount_account_from_env(state) == ("account-merkos", "merkos")

        # Foreign path → bare.
        os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        assert cus.mount_account_from_env(state) == (None, None)
    finally:
        env.restore()


def test_statusline_shows_slot_hardpin_badge():
    """A slot session's statusline shows the 🔒<slot> hard-pin badge and the
    slot's account; a bare session (no CLAUDE_CONFIG_DIR) shows neither."""
    from click.testing import CliRunner
    env = _Env()
    try:
        state = cus.load_state()
        name, slot_dir = cus.create_slot(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)
        runner = CliRunner()

        # Slot session: badge + slot's account (alpha), color off for asserts.
        r = runner.invoke(cus.cli, ["statusline", "--compact"],
                          env={"CLAUDE_CONFIG_DIR": str(slot_dir), "NO_COLOR": "1"})
        assert r.exit_code == 0, r.output
        assert f"🔒{name}" in r.output, r.output
        assert "alpha" in r.output

        # Bare session: no badge, shows global active (gamma).
        r2 = runner.invoke(cus.cli, ["statusline", "--compact"],
                           env={"CLAUDE_CONFIG_DIR": None, "NO_COLOR": "1"})
        assert r2.exit_code == 0, r2.output
        assert "🔒" not in r2.output, r2.output
    finally:
        env.restore()


def test_pick_launch_account_lane_share_fallback():
    """Saturated regime (every healthy account on a live mount): lane_sharing
    off preserves the #104 refusal (None); lane_sharing on returns the
    lowest-usage live account so _launch_prepare can JOIN its lane
    (2026-07-03 — `cus launch auto` used to be dead whenever slots saturated
    the pool, even with a near-idle account joinable)."""
    env = _Env()
    try:
        state = cus.load_state()
        state["slots"] = {"slot-1": {"account": "alpha"}, "slot-2": {"account": "beta"}}
        for s in ("slot-1", "slot-2"):
            cus.slot_path(s).mkdir(parents=True, exist_ok=True)
        live = {str(cus.slot_path("slot-1")), str(cus.slot_path("slot-2")), str(cus.CLAUDE_DIR)}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        config = cus.load_config()
        assert cus.pick_launch_account(state, config) is None, \
            "lane_sharing off: all-live pool still refuses (#104)"

        cus._OCCUPIED_SLOTS_CACHE.clear()
        config = cus.deep_merge(config, {"per_session": {"lane_sharing": True}})
        t = cus.pick_launch_account(state, config)
        # gamma (5%, the shared-mount active) is the lowest-usage live account.
        assert t is not None and t.name == "gamma", t
        assert "lane-share fallback" in t.reason
    finally:
        env.restore()


def test_launch_prepare_joins_shared_mount():
    """Launching the shared-mount active with live bare sessions: lane_sharing
    on JOINS the global pair (bare session — 'merkos should be a legal
    target'); off keeps the #104 refusal."""
    import click
    env = _Env()
    try:
        live = {str(cus.CLAUDE_DIR)}
        cus.mount_pids = lambda mount: [1] if str(mount) in live else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        state = cus.load_state()

        config = cus.load_config()
        try:
            cus._launch_prepare("gamma", state, config)
            raise AssertionError("expected ClickException with lane_sharing off")
        except click.ClickException:
            pass

        config = cus.deep_merge(config, {"per_session": {"lane_sharing": True}})
        slot_name, slot_dir, account = cus._launch_prepare("gamma", cus.load_state(), config)
        assert slot_name == "shared"
        assert slot_dir == cus.CLAUDE_DIR
        assert account == "gamma"
    finally:
        env.restore()


def test_launch_swap_does_not_arm_ladder_hysteresis():
    """trigger='launch' bumps last_swap_ts (display) but NOT last_auto_swap_ts
    (the ladder cooldown clock) — a launch isn't ladder churn (2026-07-03:
    launches kept re-arming a 50-min cooldown, parking hot slots). A daemon
    trigger arms both."""
    env = _Env()
    try:
        state = cus.load_state()
        name, _slot_dir = cus.create_slot(state)
        cus.execute_swap("alpha", trigger="launch", slot=name)
        st = cus.load_state()
        assert st["accounts"]["alpha"].get("last_swap_ts")
        assert "last_auto_swap_ts" not in st["accounts"]["alpha"]

        cus.execute_swap("beta", trigger="auto-ladder", slot=name)
        st = cus.load_state()
        assert st["accounts"]["beta"].get("last_auto_swap_ts")
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
