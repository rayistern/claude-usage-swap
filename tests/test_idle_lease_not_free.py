"""An idle lease is not a free login family.

2026-11-02T21:22:00Z: slot-21 held an idle lease on default/family-15 and was
locked. `cus slot move slot-11 default` claimed family-15. The refresh of
that single-use token logged the idle mount out.

A family is taken when any slot's lease map names it, or when any slot
mount's refresh-token fingerprint matches it. With nothing left, the move
refuses and does not snapshot.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import click  # noqa: E402
import pytest  # noqa: E402
from click.testing import CliRunner  # noqa: E402

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


FAM15 = "rt-default-family-15"
FAM16 = "rt-default-family-16"


class _Env:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-bare")))
        for name in ("default", "beta"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{name}@x"}}))
        cus.write_json(self.accounts_dir / "state.json", {
            "active": "default",
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0}
                         for n in ("default", "beta")},
            "slots": {},
            "swap_history": [],
        })
        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._saved_probe = cus._oauth_refresh_grant
        cus._oauth_refresh_grant = lambda rt: ("unknown", None)

    def set_config(self, cfg: dict) -> None:
        cus.write_yaml(cus.CONFIG_YAML, cfg)

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._oauth_refresh_grant = self._saved_probe
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def _family_rt(account: str, family_id: str) -> str | None:
    return cus._credential_refresh_token(cus.read_json(cus.login_family_creds_path(account, family_id)))


def _named_slot(env: _Env, name: str, account: str, live: bool,
                family_id: str | None = None, refresh: str | None = None) -> None:
    d = cus.slot_path(name)
    cus.scaffold_mount_dir(d)
    (d / ".credentials.json").write_text(json.dumps(_creds(refresh or f"rt-{account}")))
    (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{account}@x"}}))
    state = cus.load_state()
    state["slots"][name] = {"account": account, "created_ts": "2026-11-02T21:17:00Z"}
    if family_id:
        state["slots"][name]["login_family"] = f"{account}/{family_id}"
    cus.save_state(state)
    if live:
        env.live_slots.add(name)
    cus._OCCUPIED_SLOTS_CACHE.clear()


def _setup(extra_family: bool = False, fingerprint_only: bool = False, lease_only: bool = False) -> _Env:
    """Hybrid, active=default, so a move onto default must claim a family.

    slot-21 is idle and locked. slot-11 is the live mover, on beta.
    """
    env = _Env()
    env.set_config({
        "mode": "hybrid",
        "independent_logins": {"use_independent_logins": True},
        "session_locks": {"locked_slots": ["slot-21"]},
    })
    d = cus.login_family_dir("default", "family-15")
    d.mkdir(parents=True, exist_ok=True)
    cus.login_family_creds_path("default", "family-15").write_text(json.dumps(_creds(FAM15)))
    if extra_family:
        d16 = cus.login_family_dir("default", "family-16")
        d16.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("default", "family-16").write_text(json.dumps(_creds(FAM16)))
    _named_slot(env, "slot-11", "beta", live=True)
    if fingerprint_only:
        _named_slot(env, "slot-21", "default", live=False, refresh=FAM15)
    elif lease_only:
        _named_slot(env, "slot-21", "default", live=False, family_id="family-15",
                    refresh="rt-slot-21-other")
    else:
        _named_slot(env, "slot-21", "default", live=False, family_id="family-15", refresh=FAM15)
    return env


def test_move_onto_account_whose_only_family_is_an_idle_lease_refuses():
    env = _setup()
    try:
        before = _family_rt("default", "family-15")
        r = CliRunner().invoke(cus.cli, ["slot", "move", "slot-11", "default"])
        assert r.exit_code != 0, r.output
        assert "no free login family" in r.output
        assert "claimed login family" not in r.output
        assert cus.load_state()["slots"]["slot-11"]["account"] == "beta"
        with pytest.raises(RuntimeError, match="pool exhausted"):
            cus.execute_swap("default", trigger="test", slot="slot-11")
        r2 = CliRunner().invoke(cus.cli, ["slot", "move", "slot-11", "default", "--force"])
        assert r2.exit_code != 0 and "pool exhausted" in r2.output, r2.output
        assert cus.load_state()["slots"]["slot-11"]["account"] == "beta"
        assert cus.load_state()["slots"]["slot-21"]["login_family"] == "default/family-15"
        assert _family_rt("default", "family-15") == before
        installed = cus._credential_refresh_token(
            cus.read_json(cus.slot_path("slot-11") / ".credentials.json"))
        assert installed == "rt-beta"
    finally:
        env.restore()


def test_move_takes_the_next_free_family_when_one_exists():
    env = _setup(extra_family=True)
    try:
        before = _family_rt("default", "family-15")
        r = CliRunner().invoke(cus.cli, ["slot", "move", "slot-11", "default"])
        assert r.exit_code == 0, r.output
        assert "claimed login family: default/family-16" in r.output
        assert "claimed login family: default/family-15" not in r.output
        st = cus.load_state()
        assert st["slots"]["slot-11"]["account"] == "default"
        assert st["slots"]["slot-11"]["login_family"] == "default/family-16"
        assert st["slots"]["slot-21"]["login_family"] == "default/family-15"
        assert _family_rt("default", "family-15") == before
        installed = cus._credential_refresh_token(
            cus.read_json(cus.slot_path("slot-11") / ".credentials.json"))
        assert installed == FAM16
    finally:
        env.restore()


def test_launch_onto_account_whose_only_family_is_an_idle_lease_refuses():
    env = _setup()
    try:
        before = _family_rt("default", "family-15")
        with pytest.raises(click.ClickException, match="no free independent login family"):
            cus._launch_prepare("default", cus.load_state(), cus.load_config(), lane="slot-12")
        assert not cus.slot_path("slot-12").exists()
        assert cus.load_state()["slots"]["slot-21"]["login_family"] == "default/family-15"
        assert _family_rt("default", "family-15") == before
    finally:
        env.restore()


def test_launch_takes_the_next_free_family_when_one_exists():
    env = _setup(extra_family=True)
    try:
        before = _family_rt("default", "family-15")
        got = cus._launch_prepare("default", cus.load_state(), cus.load_config(), lane="slot-12")
        assert got[0] == "slot-12"
        st = cus.load_state()
        assert st["slots"]["slot-12"]["login_family"] == "default/family-16"
        assert st["slots"]["slot-12"]["account"] == "default"
        assert st["slots"]["slot-21"]["login_family"] == "default/family-15"
        assert _family_rt("default", "family-15") == before
        installed = cus._credential_refresh_token(
            cus.read_json(cus.slot_path("slot-12") / ".credentials.json"))
        assert installed == FAM16
    finally:
        env.restore()


def test_status_shows_the_idle_lease_as_held_and_omits_it_from_the_free_count():
    env = _setup(extra_family=True)
    try:
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, r.output
        assert "2 family(ies), 1 free" in r.output
        row15 = next(line for line in r.output.splitlines() if line.strip().startswith("family-15"))
        row16 = next(line for line in r.output.splitlines() if line.strip().startswith("family-16"))
        assert "idle-lease slot-21" in row15
        assert "free" not in row15
        assert "free" in row16
    finally:
        env.restore()


def test_lease_map_alone_takes_the_family():
    env = _setup(lease_only=True)
    try:
        assert cus.leased_families("default", cus.load_state()) == {"family-15"}
        with pytest.raises(RuntimeError, match="pool exhausted"):
            cus.execute_swap("default", trigger="test", slot="slot-11")
        assert cus.load_state()["slots"]["slot-21"]["login_family"] == "default/family-15"
    finally:
        env.restore()


def test_mount_fingerprint_alone_takes_the_family():
    env = _setup(fingerprint_only=True)
    try:
        assert cus.leased_families("default", cus.load_state()) == {"family-15"}
        assert "login_family" not in cus.load_state()["slots"]["slot-21"]
        with pytest.raises(RuntimeError, match="pool exhausted"):
            cus.execute_swap("default", trigger="test", slot="slot-11")
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, r.output
        assert "1 family(ies), 0 free" in r.output
        row = next(line for line in r.output.splitlines() if line.strip().startswith("family-15"))
        assert "held" in row
        assert "free" not in row
    finally:
        env.restore()
