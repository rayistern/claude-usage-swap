"""Tests for the per-(slot, account) independent-login store (GH #109, Phase 1).

Plan: docs/plans/2026-07-02-seamless-swap-independent-logins.md (Phase 1).

Phase 1 is ADDITIVE — it populates a new store and surfaces it, but does NOT
change the swap path (that is the flag-gated Phase 2). These tests therefore
cover the store primitives, the `cus login-mount` provisioning command, and the
status/sos surfacing — and assert the swap path is untouched by checking the
default gate is off.

Covers:
  - store path helpers + has_independent_login (present / absent / invalid)
  - scaffold_login_store_dir: minimal login dir, idempotent
  - write/read provenance + refresh-token fingerprint (stable + distinct)
  - login_expiry_state: ok / near / expired / unknown
  - _slots_missing_logins: only occupied slots without a login
  - login_mount_cmd: provision print, --finish (match / mismatch / --force),
    --list, unknown account, bad slot
  - diagnose(): expired + mismatch + (gate-on) missing conditions; gate-off silence

Run standalone:  python3 tests/test_independent_logins.py
Run under pytest: pytest tests/test_independent_logins.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


class _Env:
    """Throwaway on-disk tree mirroring production layout (same monkeypatch
    pattern as test_slots_doctor_sync._Env so this file stays standalone)."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"
        self.state_json = self.accounts_dir / "state.json"

        for sub in ("projects", "plugins", "commands", "session-env"):
            (self.claude_dir / sub).mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text(json.dumps({"statusLine": {"type": "command"}}))
        self.claude_json.write_text(json.dumps({"userID": "uid-live", "oauthAccount": {"emailAddress": "live@x"}}))

        # Account dirs with authoritative oauthAccount identities.
        for name, rt, email in (("alpha", "rt-alpha", "alpha@x"), ("beta", "rt-beta", "beta@x")):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(rt)))
            (d / ".claude.json").write_text(json.dumps(
                {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": email}}))

        cus.write_json(self.state_json, {
            "active": "alpha",
            "accounts": {"alpha": {"next_swap_at_pct": 50}, "beta": {"next_swap_at_pct": 50}},
            "slots": {
                "slot-1": {"account": "alpha"},
                "slot-2": {"account": "beta"},
                "slot-3": {"account": None},  # empty slot — nothing to host yet
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
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []

    def set_config(self, cfg: dict) -> None:
        """Write a config.yaml so load_config() picks up overrides."""
        cus.write_yaml(cus.CONFIG_YAML, cfg)

    # --- helpers to plant store state directly (bypassing the interactive login) ---
    def plant_login(self, account: str, slot: str, refresh: str, email: str,
                    minted_ts: str | None = None) -> None:
        """Simulate a completed /login into the store dir for (account, slot)."""
        d = cus.login_store_dir(account, slot)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".credentials.json").write_text(json.dumps(_creds(refresh)))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": email}}))
        prov = {"account": account, "slot": slot,
                "minted_ts": minted_ts or cus.now_iso(),
                "source_email": email,
                "refresh_fp": cus._refresh_fingerprint(refresh)}
        cus.write_json(cus.login_store_provenance_path(account, slot), prov)

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        self._tmp.cleanup()


def _iso_days_ago(days: float) -> str:
    from datetime import datetime, timedelta, timezone
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Store primitives
# --------------------------------------------------------------------------

def test_paths_are_under_logins_root():
    env = _Env()
    try:
        d = cus.login_store_dir("alpha", "slot-1")
        assert d == env.accounts_dir / "logins" / "alpha" / "slot-1"
        assert cus.login_store_creds_path("alpha", "slot-1") == d / ".credentials.json"
        assert cus.login_store_provenance_path("alpha", "slot-1") == d / "provenance.json"
    finally:
        env.restore()


def test_has_independent_login_present_absent_invalid():
    env = _Env()
    try:
        assert cus.has_independent_login("alpha", "slot-1") is False  # nothing planted
        env.plant_login("alpha", "slot-1", "rt-indep-a1", "alpha@x")
        assert cus.has_independent_login("alpha", "slot-1") is True
        # A logout-shaped / tokenless creds file is NOT a usable login.
        bad = cus.login_store_creds_path("beta", "slot-2")
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps({"claudeAiOauth": {"accessToken": "only-access"}}))
        assert cus.has_independent_login("beta", "slot-2") is False
        # Unparseable JSON is treated as absent, not a crash.
        bad.write_text("{ not json")
        assert cus.has_independent_login("beta", "slot-2") is False
    finally:
        env.restore()


def test_scaffold_login_store_dir_idempotent():
    env = _Env()
    try:
        d = cus.scaffold_login_store_dir("alpha", "slot-1")
        assert (d / ".claude.json").exists()
        assert (d / "projects").is_symlink()  # shared subdir linked
        # Second call must not raise and must not clobber .claude.json.
        (d / ".claude.json").write_text(json.dumps({"marker": 1}))
        cus.scaffold_login_store_dir("alpha", "slot-1")
        assert json.loads((d / ".claude.json").read_text()) == {"marker": 1}
    finally:
        env.restore()


def test_fingerprint_stable_and_distinct():
    assert cus._refresh_fingerprint("tok-A") == cus._refresh_fingerprint("tok-A")
    assert cus._refresh_fingerprint("tok-A") != cus._refresh_fingerprint("tok-B")
    assert cus._refresh_fingerprint("tok-A").startswith("sha256:")


def test_write_read_provenance_roundtrip():
    env = _Env()
    try:
        env.plant_login("alpha", "slot-1", "rt-indep", "alpha@x")  # seeds creds
        # Overwrite provenance via the real writer so we exercise it.
        prov = cus.write_login_provenance("alpha", "slot-1", "alpha@x")
        assert prov["account"] == "alpha" and prov["slot"] == "slot-1"
        assert prov["refresh_fp"] == cus._refresh_fingerprint("rt-indep")
        assert cus.read_login_provenance("alpha", "slot-1")["source_email"] == "alpha@x"
    finally:
        env.restore()


def test_login_expiry_state_transitions():
    env = _Env()
    try:
        cfg = {"independent_logins": {"refresh_token_ttl_days": 30, "warn_expiry_within_days": 5}}
        env.plant_login("alpha", "slot-1", "rt-fresh", "alpha@x", minted_ts=_iso_days_ago(1))
        env.plant_login("beta", "slot-2", "rt-near", "beta@x", minted_ts=_iso_days_ago(27))
        env.plant_login("alpha", "slot-9", "rt-old", "alpha@x", minted_ts=_iso_days_ago(40))
        assert cus.login_expiry_state("alpha", "slot-1", cfg)[0] == "ok"
        assert cus.login_expiry_state("beta", "slot-2", cfg)[0] == "near"
        assert cus.login_expiry_state("alpha", "slot-9", cfg)[0] == "expired"
        # No provenance timestamp => unknown.
        cus.write_json(cus.login_store_provenance_path("alpha", "slot-1"), {"account": "alpha"})
        assert cus.login_expiry_state("alpha", "slot-1", cfg)[0] == "unknown"
    finally:
        env.restore()


def test_slots_missing_logins_only_occupied_without_login():
    env = _Env()
    try:
        state = cus.load_state()
        # No logins planted: slot-1(alpha) + slot-2(beta) missing; slot-3 empty skipped.
        assert cus._slots_missing_logins(state) == [("slot-1", "alpha"), ("slot-2", "beta")]
        env.plant_login("alpha", "slot-1", "rt-a1", "alpha@x")
        assert cus._slots_missing_logins(state) == [("slot-2", "beta")]
    finally:
        env.restore()


def test_list_provisioned_logins():
    env = _Env()
    try:
        assert cus.list_provisioned_logins() == []
        env.plant_login("beta", "slot-2", "rt-b2", "beta@x")
        recs = cus.list_provisioned_logins()
        assert len(recs) == 1
        assert recs[0]["account"] == "beta" and recs[0]["has_login"] is True
        assert recs[0]["identity_email"] == "beta@x"
    finally:
        env.restore()


# --------------------------------------------------------------------------
# cus login-mount command
# --------------------------------------------------------------------------

def test_login_mount_provision_prints_command_and_scaffolds():
    env = _Env()
    try:
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha"])
        assert r.exit_code == 0, r.output
        assert "CLAUDE_CONFIG_DIR=" in r.output and "--finish" in r.output
        assert cus.login_store_dir("alpha", "slot-1").exists()  # scaffolded
    finally:
        env.restore()


def test_login_mount_unknown_account_and_bad_slot():
    env = _Env()
    try:
        r1 = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "nope"])
        assert r1.exit_code != 0 and "Unknown account" in r1.output
        r2 = CliRunner().invoke(cus.cli, ["login-mount", "banana", "alpha"])
        assert r2.exit_code != 0 and "Bad slot" in r2.output
    finally:
        env.restore()


def test_login_mount_finish_without_login_errors():
    env = _Env()
    try:
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--finish"])
        assert r.exit_code != 0 and "No usable login" in r.output
    finally:
        env.restore()


def test_login_mount_finish_records_when_identity_matches():
    env = _Env()
    try:
        env.plant_login("alpha", "slot-1", "rt-indep-a1", "alpha@x")  # right account
        # Remove provenance so --finish is what writes it.
        cus.login_store_provenance_path("alpha", "slot-1").unlink()
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--finish"])
        assert r.exit_code == 0, r.output
        assert "Recorded independent login" in r.output
        assert cus.read_login_provenance("alpha", "slot-1")["source_email"] == "alpha@x"
    finally:
        env.restore()


def test_login_mount_finish_refuses_wrong_account():
    env = _Env()
    try:
        # Logged into beta@x but provisioning for account alpha — the wrong-account trap.
        env.plant_login("alpha", "slot-1", "rt-wrong", "beta@x")
        cus.login_store_provenance_path("alpha", "slot-1").unlink()
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--finish"])
        assert r.exit_code != 0 and "mismatch" in r.output.lower()
        # --force overrides.
        r2 = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--finish", "--force"])
        assert r2.exit_code == 0, r2.output
    finally:
        env.restore()


def test_login_mount_list():
    env = _Env()
    try:
        r0 = CliRunner().invoke(cus.cli, ["login-mount", "--list"])
        assert r0.exit_code == 0 and "No independent logins" in r0.output
        env.plant_login("alpha", "slot-1", "rt-a1", "alpha@x", minted_ts=_iso_days_ago(1))
        r1 = CliRunner().invoke(cus.cli, ["login-mount", "--list"])
        assert r1.exit_code == 0 and "alpha" in r1.output and "slot-1" in r1.output
    finally:
        env.restore()


# --------------------------------------------------------------------------
# SOS surfacing
# --------------------------------------------------------------------------

def test_sos_flags_expired_and_mismatched_provisioned_logins():
    env = _Env()
    try:
        env.set_config({"independent_logins": {"refresh_token_ttl_days": 30, "warn_expiry_within_days": 5}})
        env.plant_login("alpha", "slot-1", "rt-old", "alpha@x", minted_ts=_iso_days_ago(40))   # expired
        env.plant_login("beta", "slot-2", "rt-wrong", "someone-else@x", minted_ts=_iso_days_ago(1))  # mismatch
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        summaries = " | ".join(c.summary for c in conds)
        assert "past assumed refresh-token lifetime" in summaries
        assert "wrong account" in summaries.lower()
        assert any(c.severity == "critical" for c in conds)
    finally:
        env.restore()


def test_sos_missing_condition_only_when_gate_on():
    env = _Env()
    try:
        # Gate OFF (default): no "missing login" nag even though slots lack logins.
        conds_off = cus.diagnose(cus.load_state(), cus.load_config())
        assert not any("lack a login" in c.summary for c in conds_off)
        # Gate ON: the occupied-but-unprovisioned slots are flagged.
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        conds_on = cus.diagnose(cus.load_state(), cus.load_config())
        assert any("lack a login" in c.summary for c in conds_on)
    finally:
        env.restore()


def test_gate_off_by_default_swap_path_untouched():
    """Phase 1 must not flip the swap path: the gate defaults off."""
    env = _Env()
    try:
        assert cus.load_config()["independent_logins"]["use_independent_logins"] is False
    finally:
        env.restore()


# --------------------------------------------------------------------------
# Phase 2 (install source) + Phase 3a (store-aware save-back)
# --------------------------------------------------------------------------

def test_swap_install_source_gate_off_uses_snapshot():
    env = _Env()
    try:
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"
        env.plant_login("alpha", "slot-1", "rt-indep", "alpha@x")
        # Gate off (default): snapshot even when a login exists.
        path, used = cus.swap_install_source("alpha", "slot-1", snap, {"independent_logins": {"use_independent_logins": False}})
        assert path == snap and used is False
    finally:
        env.restore()


def test_swap_install_source_gate_on_prefers_login_then_falls_back():
    env = _Env()
    try:
        snap = env.accounts_dir / "account-alpha" / ".credentials.json"
        cfg = {"independent_logins": {"use_independent_logins": True}}
        # No login provisioned → snapshot fallback.
        path, used = cus.swap_install_source("alpha", "slot-1", snap, cfg)
        assert path == snap and used is False
        # Login provisioned → its store creds.
        env.plant_login("alpha", "slot-1", "rt-indep", "alpha@x")
        path, used = cus.swap_install_source("alpha", "slot-1", snap, cfg)
        assert path == cus.login_store_creds_path("alpha", "slot-1") and used is True
        # No slot (global mount) → always snapshot, never a store login.
        path, used = cus.swap_install_source("alpha", None, snap, cfg)
        assert path == snap and used is False
    finally:
        env.restore()


def test_saveback_to_login_store_writes_store_and_guards_freshness():
    env = _Env()
    try:
        env.plant_login("alpha", "slot-1", "rt-indep", "alpha@x")
        fresh = _creds("rt-indep-rotated", expires_at=3_000_000_000_000)
        r = cus.saveback_to_login_store("alpha", "slot-1", fresh, json.dumps(fresh).encode())
        assert r == "saved"
        assert cus._credential_refresh_token(cus.read_json(cus.login_store_creds_path("alpha", "slot-1"))) == "rt-indep-rotated"
        # A staler live file must NOT overwrite the newer store entry.
        stale = _creds("rt-indep-old", expires_at=1_000_000_000_000)
        r2 = cus.saveback_to_login_store("alpha", "slot-1", stale, json.dumps(stale).encode())
        assert r2 == "skipped-stale"
        assert cus._credential_refresh_token(cus.read_json(cus.login_store_creds_path("alpha", "slot-1"))) == "rt-indep-rotated"
    finally:
        env.restore()


def test_gate_on_swap_installs_independent_login_and_saveback_protects_snapshot():
    """End-to-end: with the gate on, a slot swap installs the independent login,
    and swapping out saves the rotated family to the STORE — the account snapshot
    is never clobbered (the whole point of #109)."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        state = cus.load_state()
        state["slots"] = {}  # start clean so create_slot picks slot-1 fresh
        cus.save_state(state)
        state = cus.load_state()
        name, d = cus.create_slot(state)
        cus.save_state(state)

        # Independent login for (alpha, slot) with a DISTINCT refresh family.
        env.plant_login("alpha", name, "rt-indep-alpha", "alpha@x")
        cus.execute_swap("alpha", trigger="launch", slot=name)
        # The mount got the independent creds, NOT the snapshot copy (rt-alpha).
        assert cus._credential_refresh_token(cus.read_json(d / ".credentials.json")) == "rt-indep-alpha"

        # Session rotates the independent family in the mount, then we swap out.
        rotated = _creds("rt-indep-alpha-rotated", expires_at=3_000_000_000_000)
        (d / ".credentials.json").write_text(json.dumps(rotated))
        cus.execute_swap("beta", trigger="threshold", slot=name)

        # Rotated family saved back to the STORE, not the account snapshot.
        store_rt = cus._credential_refresh_token(cus.read_json(cus.login_store_creds_path("alpha", name)))
        assert store_rt == "rt-indep-alpha-rotated"
        # alpha's account snapshot is UNTOUCHED (still its own family).
        snap_rt = cus._credential_refresh_token(cus.read_json(env.accounts_dir / "account-alpha" / ".credentials.json"))
        assert snap_rt == "rt-alpha", "account snapshot must NOT be clobbered by the independent family"
        # beta has no independent login → snapshot copy installed (fallback).
        assert cus._credential_refresh_token(cus.read_json(d / ".credentials.json")) == "rt-beta"
    finally:
        env.restore()


# --------------------------------------------------------------------------
# --from-existing bootstrap + duplicate-family detection
# --------------------------------------------------------------------------

def test_from_existing_bootstrap_seeds_from_snapshot():
    env = _Env()
    try:
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--from-existing"])
        assert r.exit_code == 0, r.output
        assert "Bootstrapped" in r.output
        # Store creds match the snapshot family; provenance marked bootstrapped.
        assert cus._credential_refresh_token(cus.read_json(cus.login_store_creds_path("alpha", "slot-1"))) == "rt-alpha"
        prov = cus.read_login_provenance("alpha", "slot-1")
        assert prov["bootstrapped"] is True
        assert prov["refresh_fp"] == cus._refresh_fingerprint("rt-alpha")
    finally:
        env.restore()


def test_duplicate_family_detected_and_warned_and_sos():
    env = _Env()
    try:
        # Bootstrap alpha onto TWO slots → same family on two mounts (the clobber).
        CliRunner().invoke(cus.cli, ["login-mount", "slot-1", "alpha", "--from-existing"])
        r = CliRunner().invoke(cus.cli, ["login-mount", "slot-2", "alpha", "--from-existing"])
        assert r.exit_code == 0
        assert "multiple mounts" in r.output  # command warns at provision time
        dups = cus.duplicate_login_families()
        assert dups and sorted(dups[0]["pairs"]) == ["slot-1->alpha", "slot-2->alpha"]
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        assert any("collide across mounts" in c.summary and c.severity == "critical" for c in conds)
    finally:
        env.restore()


# --------------------------------------------------------------------------
# Track A — lane sharing (multiple sessions per mount, no extra login)
# --------------------------------------------------------------------------

def _make_live_lane(env: "_Env", slot: str, account: str) -> None:
    """Scaffold a slot dir holding `account` and make it appear live (one pid)."""
    state = cus.load_state()
    state["slots"] = {slot: {"account": account}}
    cus.save_state(state)
    cus.scaffold_mount_dir(cus.slot_path(slot))
    live = cus.slot_path(slot)
    cus.mount_pids = lambda m: [999] if str(m) == str(live) else []
    cus._OCCUPIED_SLOTS_CACHE.clear()


def test_lane_sharing_joins_existing_lane_no_swap():
    env = _Env()
    try:
        env.set_config({"per_session": {"lane_sharing": True}})
        _make_live_lane(env, "slot-1", "alpha")
        name, d, acct = cus._launch_prepare("alpha", cus.load_state(), cus.load_config())
        # Joined the existing lane — same slot, no new one allocated.
        assert (name, acct) == ("slot-1", "alpha")
        assert d == cus.slot_path("slot-1")
        assert set(cus.load_state()["slots"]) == {"slot-1"}, "no second mount created"
    finally:
        env.restore()


def test_lane_sharing_off_still_refuses_double_book():
    env = _Env()
    try:
        env.set_config({"per_session": {"lane_sharing": False}})
        _make_live_lane(env, "slot-1", "alpha")
        # Default behavior (gate off, no sharing): #104 refuses a 2nd mount.
        import pytest as _pt
        with _pt.raises(Exception) as ei:
            cus._launch_prepare("alpha", cus.load_state(), cus.load_config())
        assert "already running on a live mount" in str(ei.value) or "GH #104" in str(ei.value)
    finally:
        env.restore()


# --------------------------------------------------------------------------
# Track B Phase 3b — #104 lift for a provably-independent 2nd lane
# --------------------------------------------------------------------------

def test_phase3b_second_independent_lane_allowed_and_installs_login():
    env = _Env()
    try:
        # alpha is live on slot-1; gate on; a distinct independent login exists
        # for (alpha, slot-8). Launching alpha --lane slot-8 must be ALLOWED and
        # install that independent family, not alpha's snapshot copy.
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        _make_live_lane(env, "slot-1", "alpha")
        env.plant_login("alpha", "slot-8", "rt-indep-alpha", "alpha@x")
        name, d, acct = cus._launch_prepare("alpha", cus.load_state(), cus.load_config(), lane="slot-8")
        assert (name, acct) == ("slot-8", "alpha")
        # slot-8 got the INDEPENDENT family, not the snapshot's rt-alpha.
        assert cus._credential_refresh_token(cus.read_json(d / ".credentials.json")) == "rt-indep-alpha"
    finally:
        env.restore()


def test_phase3b_second_lane_refused_without_independent_login():
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        _make_live_lane(env, "slot-1", "alpha")
        # No login planted for (alpha, slot-8) → still refused (would be a copy).
        import pytest as _pt
        with _pt.raises(Exception) as ei:
            cus._launch_prepare("alpha", cus.load_state(), cus.load_config(), lane="slot-8")
        msg = str(ei.value)
        assert "GH #104" in msg and "login-mount" in msg
    finally:
        env.restore()


def test_phase3b_gate_off_second_lane_still_refused_even_with_login():
    env = _Env()
    try:
        # Login exists but gate is OFF → the escape hatch is inert, #104 holds.
        env.set_config({"independent_logins": {"use_independent_logins": False}})
        _make_live_lane(env, "slot-1", "alpha")
        env.plant_login("alpha", "slot-8", "rt-indep-alpha", "alpha@x")
        import pytest as _pt
        with _pt.raises(Exception):
            cus._launch_prepare("alpha", cus.load_state(), cus.load_config(), lane="slot-8")
    finally:
        env.restore()


def test_launch_bad_lane_shape_rejected():
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        import pytest as _pt
        with _pt.raises(Exception) as ei:
            cus._launch_prepare("alpha", cus.load_state(), cus.load_config(), lane="banana")
        assert "bad --lane" in str(ei.value)
    finally:
        env.restore()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
