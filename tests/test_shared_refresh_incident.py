"""Snapshot rotation must never strand a lane installed from that snapshot."""
import json
import os
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cus


def creds(rt="fake-refresh", expiry=1000):
    return {"claudeAiOauth": {"accessToken": "fake-access", "refreshToken": rt,
                               "expiresAt": expiry}}


@pytest.fixture
def incident(tmp_path, monkeypatch):
    for key, path in {
        "HOME": tmp_path, "ACCOUNTS_DIR": tmp_path / "accounts",
        "STATE_JSON": tmp_path / "state.json", "CONFIG_YAML": tmp_path / "config.yaml",
        "CLAUDE_DIR": tmp_path / "shared", "CREDS_JSON": tmp_path / "shared" / ".credentials.json",
        "CLAUDE_JSON": tmp_path / "shared.json", "INBOX_MD": tmp_path / "inbox.md",
    }.items():
        monkeypatch.setattr(cus, key, path)
    cus.ACCOUNTS_DIR.mkdir()
    cfg = {"mode": "per_session", "poll_only_lane_maintenance": True,
           "independent_logins": {"use_independent_logins": True}}
    cus.write_yaml(cus.CONFIG_YAML, cfg)
    state = {"accounts": {"sample": {"token_stale": True}},
             "slots": {"slot-1": {"account": "sample"}}, "swap_history": []}
    cus.write_json(cus.STATE_JSON, state)
    snap = cus.account_creds_path("sample")
    snap.parent.mkdir()
    cus.write_json(snap, creds())
    mount = cus.mount_creds_path(cus.slot_path("slot-1"))
    mount.parent.mkdir()
    cus.write_json(mount, creds())
    monkeypatch.setattr(cus, "mount_pids", lambda p: [123] if p == mount.parent else [])
    monkeypatch.setattr(cus, "occupied_slot_accounts", lambda *a, **k: {"sample": ["slot-1"]})
    cus._reset_blank_tracking()
    return cfg, state, snap, mount


@pytest.mark.parametrize("path", ["poll", "sweep", "keepalive", "force-poll", "snapshot-probe",
                                  "store-probe", "claim", "reseed"])
@pytest.mark.parametrize("live", [True, False])
def test_incident_snapshot_shared_with_lane_never_spends_token(incident, monkeypatch, capsys, path, live):
    cfg, state, snap, mount = incident
    if not live:
        monkeypatch.setattr(cus, "mount_pids", lambda p: [])
    calls = []
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: calls.append(rt) or ("dead", None))
    def forbidden(*args, **kwargs):
        calls.append("http")
        raise AssertionError("shared refresh reached HTTP")
    monkeypatch.setattr(cus.urllib.request, "urlopen", forbidden)
    before = snap.read_bytes(), mount.read_bytes()
    if path == "poll":
        assert cus.poll_account_usage("sample").token_stale
    elif path == "sweep":
        assert cus._sweep_unstale_idle_accounts(state, cfg) == []
    elif path == "keepalive":
        cus._keepalive_token_for("sample", state["accounts"]["sample"], state, cfg)
    elif path == "force-poll":
        monkeypatch.setattr(cus, "poll_account_usage", lambda a: stale_usage())
        result = CliRunner().invoke(cus.cli, ["force-poll", "sample"])
        assert result.exit_code == 0, result.output
        assert "held by a mount" in result.output
    elif path == "snapshot-probe":
        cus._account_snapshot_dead("sample", cfg, force=True)
    elif path == "store-probe":
        cus._store_creds_dead(snap, "incident", cfg, force=True)
    elif path == "claim":
        monkeypatch.setattr(cus, "list_login_families", lambda a: ["family-1"])
        monkeypatch.setattr(cus, "leased_families", lambda *a: set())
        monkeypatch.setattr(cus, "login_family_creds_path", lambda *a: snap)
        assert cus.claim_verified_login_family("sample", state, cfg) is None
    else:
        monkeypatch.setattr(cus, "_prune_free_families", lambda *a, **k: [])
        assert cus._reseed_family_from_canonical("sample", state, cfg) is None
    assert calls == []
    assert before == (snap.read_bytes(), mount.read_bytes())


def stale_usage():
    u = cus.AccountUsage.empty()
    u.token_stale = True
    u.raw = {}
    return u


@pytest.mark.parametrize("enabled", [False, True])
def test_incident_poll_only_preempt_obeys_maintenance_key(incident, monkeypatch, enabled):
    cfg, state, snap, mount = incident
    cfg["poll_only_lane_maintenance"] = enabled
    cus.write_json(mount, creds(expiry=int((time.time() + 1200) * 1000)))
    os.utime(mount, (time.time() - 7200,) * 2)
    calls = []
    monkeypatch.setattr(cus, "_refresh_account_token", lambda *a, **k: calls.append(k["creds_path"]) or True)
    assert cus._preempt_live_lane_blanks(state, cfg, no_execute=True) == (["slot-1"] if enabled else [])
    assert calls == ([mount] if enabled else [])


@pytest.mark.parametrize("enabled", [False, True])
def test_incident_poll_only_heal_obeys_maintenance_key(incident, monkeypatch, enabled):
    cfg, state, snap, mount = incident
    cfg["poll_only_lane_maintenance"] = enabled
    cus.write_json(mount, {})
    monkeypatch.setattr(cus, "_blanked_live_lanes", lambda *a: [("slot-1", "sample")])
    monkeypatch.setattr(cus, "_lane_heal_source", lambda *a, **k: snap)
    calls = []
    monkeypatch.setattr(cus, "_verified_lane_heal_source", lambda *a: calls.append(True) or snap)
    assert cus._auto_heal_live_lanes(state, cfg, no_execute=True) == (["slot-1"] if enabled else [])
    assert calls == ([True] if enabled else [])
    assert cus.read_json(mount) == (creds() if enabled else {})


@pytest.mark.parametrize("verdict", ["dead", "unknown", "alive"])
def test_incident_fresh_shadow_must_prove_refresh_chain_alive(incident, monkeypatch, verdict):
    cfg, state, snap, mount = incident
    snap.unlink()
    cus.write_json(mount, {})
    shadow = cus._lane_lastvalid_path(mount)
    cus.write_json(shadow, creds(expiry=int((time.time() + 3600) * 1000)))
    calls = []
    def grant(rt):
        calls.append(rt)
        return verdict, {"access_token": "fake-new-access", "refresh_token": "fake-new-refresh", "expires_in": 3600}
    monkeypatch.setattr(cus, "_oauth_refresh_grant", grant)
    source = cus._verified_lane_heal_source("slot-1", "sample", state, cfg)
    assert source == (shadow if verdict == "alive" else None)
    assert calls == ["fake-refresh"]


def test_incident_heal_does_not_spend_token_held_in_another_store(incident, monkeypatch):
    cfg, state, snap, mount = incident
    cus.write_json(mount, {})
    shadow = cus._lane_lastvalid_path(mount)
    cus.write_json(shadow, creds(expiry=int((time.time() + 3600) * 1000)))
    # The lane's own snapshot equal to its shadow is its steady state, not a
    # rival (see the steady-state tests below). A store outside the lane's own
    # chain holding the same token is a rival.
    cus.write_json(snap, creds(rt="fake-snapshot"))
    foreign = cus.account_creds_path("other")
    foreign.parent.mkdir()
    cus.write_json(foreign, creds())
    calls = []
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: calls.append(rt) or ("dead", None))
    assert cus._verified_lane_heal_source("slot-1", "sample", state, cfg) is None
    # The shared token is never presented; only the snapshot's own chain is.
    assert calls == ["fake-snapshot"]
    assert cus.read_json(foreign) == creds()


@pytest.mark.parametrize("other", ["shared", "idle", "unreadable"])
def test_incident_shared_guard_checks_all_mounts_in_memory(incident, other):
    cfg, state, snap, mount = incident
    mount.unlink()
    path = cus.CREDS_JSON if other == "shared" else mount
    path.parent.mkdir(exist_ok=True)
    path.write_text("broken-json" if other == "unreadable" else json.dumps(creds()))
    assert cus._refresh_token_on_mount("fake-refresh")


def test_incident_preempt_refuses_a_second_mount_with_the_same_token(incident):
    cfg, state, snap, mount = incident
    cus.CREDS_JSON.parent.mkdir()
    cus.write_json(cus.CREDS_JSON, creds())
    assert not cus._refresh_account_token("sample", creds_path=mount)


@pytest.mark.parametrize("path", ["account", "unstale", "snapshot", "store", "claim", "reseed"])
def test_incident_each_rotation_writer_emits_redacted_audit(incident, monkeypatch, capsys, path):
    cfg, state, snap, mount = incident
    mount.unlink()
    def grant(rt):
        return "alive", {"access_token": "fake-new-access", "refresh_token": "fake-new-refresh", "expires_in": 3600}
    monkeypatch.setattr(cus, "_oauth_refresh_grant", grant)
    if path == "account":
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return json.dumps(grant("")[1]).encode()
        monkeypatch.setattr(cus.urllib.request, "urlopen", lambda *a, **k: Response())
        assert cus._refresh_account_token("sample")
        op = "account-refresh"
    elif path == "unstale":
        assert cus._unstale_account_snapshot("sample", state, cfg, force=True)
        op = "unstale-refresh"
    elif path == "snapshot":
        assert not cus._account_snapshot_dead("sample", cfg, force=True)
        op = "snapshot-dead-probe"
    elif path == "store":
        assert not cus._store_creds_dead(snap, "test", cfg, force=True)
        op = "store-dead-probe"
    elif path == "claim":
        monkeypatch.setattr(cus, "list_login_families", lambda a: ["family-1"])
        monkeypatch.setattr(cus, "leased_families", lambda *a: set())
        monkeypatch.setattr(cus, "login_family_creds_path", lambda *a: snap)
        assert cus.claim_verified_login_family("sample", state, cfg) == "family-1"
        op = "claim-refresh"
    else:
        monkeypatch.setattr(cus, "_prune_free_families", lambda *a, **k: [])
        monkeypatch.setattr(cus, "_live_surface_fingerprints", lambda *a, **k: {})
        monkeypatch.setattr(cus, "scaffold_login_family_dir", lambda a, f: cus.login_family_dir(a, f).mkdir(parents=True))
        monkeypatch.setattr(cus, "account_canonical_identity", lambda a: {})
        monkeypatch.setattr(cus, "_account_snapshot_age_days", lambda a: None)
        assert cus._reseed_family_from_canonical("sample", state, cfg)
        op = "reseed-refresh"
    out = capsys.readouterr().out
    assert f"CRED-AUDIT op={op}" in out
    assert "token_fp=sha256:" in out
    assert "fake-new-refresh" not in out and "fake-refresh" not in out


@pytest.mark.parametrize("available", [False, True])
def test_incident_blank_reinstall_prefers_verified_free_family(available, monkeypatch):
    from test_login_pool import _Env
    env = _Env(accounts=("sample", "other"))
    try:
        env.set_config({"mode": "per_session", "independent_logins": {"use_independent_logins": True}})
        lane = env.make_slot("sample", live=True)
        cus.write_json(cus.mount_creds_path(cus.slot_path(lane)), {})
        if available:
            env.plant_family("sample", "family-1", "fake-independent")
        monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: ("alive", {
            "access_token": "fake-verified-access", "refresh_token": "fake-verified-refresh", "expires_in": 3600}))
        cus.execute_swap("sample", slot=lane, force_reinstall=True)
        rt = cus._credential_refresh_token(cus.read_json(cus.mount_creds_path(cus.slot_path(lane))))
        assert rt == ("fake-verified-refresh" if available else "rt-sample")
        assert bool(cus.slot_leased_family(cus.load_state(), lane)) == available
    finally:
        env.restore()


def test_incident_preferred_free_family_must_be_verified(incident, monkeypatch):
    cfg, state, snap, mount = incident
    mount.unlink()
    monkeypatch.setattr(cus, "list_login_families", lambda a: ["family-1"])
    monkeypatch.setattr(cus, "leased_families", lambda *a: set())
    monkeypatch.setattr(cus, "login_family_creds_path", lambda *a: snap)
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: ("unknown", None))
    assert cus.claim_verified_login_family("sample", state, cfg, require_verified=True) is None


def test_incident_poll_only_maintenance_does_not_enable_lane_moves(incident, monkeypatch):
    cfg, state, snap, mount = incident
    monkeypatch.setattr(cus, "_lazy_warm_slot_sessions", lambda *a: [])
    monkeypatch.setattr(cus, "_log_decision", lambda *a: None)
    calls = []
    monkeypatch.setattr(cus, "execute_swap", lambda *a, **k: calls.append(a))
    cus._execute_slot_moves([{"slot": "slot-1", "from": "sample", "to": "other",
                             "gate": "ladder", "reason": "fake test", "tier": "standard"}],
                            state, cfg, no_execute=True)
    assert calls == []


def test_incident_heal_rejects_grant_without_a_persisted_new_chain(incident, monkeypatch):
    cfg, state, snap, mount = incident
    cus.write_json(mount, {})
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: ("alive", {
        "access_token": "fake-new-access", "expires_in": 3600}))
    assert cus._verified_lane_heal_source("slot-1", "sample", state, cfg) is None


def test_incident_generation_transfer_refuses_shared_store(incident, monkeypatch):
    cfg, state, snap, mount = incident
    # Pooled recovery cannot consume a canonical copied in a last-valid shadow.
    cus.write_json(snap, creds(expiry=int((time.time() + 3600) * 1000)))
    cus.write_json(mount, {})
    cus.write_json(cus._lane_lastvalid_path(mount), cus.read_json(snap))
    calls = []
    monkeypatch.setattr(cus, "_reseed_family_from_canonical", lambda *a: calls.append(True))
    assert cus._lane_generation_transfer_heal("slot-1", "sample", state, cfg) is None
    assert calls == []


def test_incident_unknown_family_must_not_trigger_generation_transfer(incident, monkeypatch):
    cfg, state, snap, mount = incident
    state["slots"]["slot-1"]["login_family"] = "sample/family-1"
    family = cus.login_family_creds_path("sample", "family-1")
    family.parent.mkdir(parents=True)
    cus.write_json(family, creds(rt="fake-independent"))
    cus.write_json(mount, {})
    calls = []
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: ("unknown", None))
    monkeypatch.setattr(cus, "_lane_generation_transfer_heal", lambda *a: calls.append(True))
    assert cus._verified_lane_heal_source("slot-1", "sample", state, cfg) is None
    assert calls == []


def test_incident_verified_preference_cannot_bypass_probe_via_config(incident, monkeypatch):
    cfg, state, snap, mount = incident
    mount.unlink()
    cfg["independent_logins"]["verify_family_on_claim"] = False
    monkeypatch.setattr(cus, "list_login_families", lambda a: ["family-1"])
    monkeypatch.setattr(cus, "leased_families", lambda *a: set())
    monkeypatch.setattr(cus, "login_family_creds_path", lambda *a: snap)
    monkeypatch.setattr(cus, "free_login_family", lambda *a: "family-1")
    monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: ("unknown", None))
    assert cus.claim_verified_login_family("sample", state, cfg, require_verified=True) is None


@pytest.mark.parametrize("surface", ["mount", "unreadable-store"])
def test_incident_recovery_sharing_check_fails_closed(incident, surface):
    cfg, state, snap, mount = incident
    mount.unlink()
    if surface == "mount":
        cus.CREDS_JSON.parent.mkdir()
        cus.write_json(cus.CREDS_JSON, creds())
    else:
        (cus.ACCOUNTS_DIR / ".credentials.json.bak.fake").write_text("invalid-json")
    assert cus._heal_token_shared("fake-refresh", snap)


def test_incident_mount_scan_failure_cannot_authorize_rotation(incident, monkeypatch):
    def unavailable():
        raise OSError("fake directory failure")
    monkeypatch.setattr(cus, "list_slot_dirs", unavailable)
    assert cus._refresh_token_on_mount("fake-refresh")


# ---------------------------------------------------------------------------
# Steady-state lane heal. Periodic save-back keeps a plain lane's snapshot
# equal to its last-valid shadow, and a leased lane's family store equal to its
# shadow. Those copies are the lane's own source, not a rival holder, so the
# verified heal must still recover the lane. A token held by any other mount or
# store stays refused.
# ---------------------------------------------------------------------------

STEADY_STATE_LANES = ["plain", "plain-backup", "leased", "leased-backup"]
BLANK = {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


def steady_state_lane(incident, kind, expiry):
    """Blank a lane whose shadow equals the store it was installed from."""
    cfg, state, snap, mount = incident
    store = snap
    if kind.startswith("leased"):
        state["slots"]["slot-1"]["login_family"] = "sample/family-1"
        cus.write_json(cus.STATE_JSON, state)
        store = cus.login_family_creds_path("sample", "family-1")
        store.parent.mkdir(parents=True)
    chain = creds(rt="fake-chain", expiry=expiry)
    cus.write_json(store, chain)
    cus.write_json(cus._lane_lastvalid_path(mount), chain)
    if kind.endswith("backup"):
        cus.write_json(store.with_name(store.name + ".bak.20260101T000000.000000Z"), chain)
    cus.write_json(mount, BLANK)
    return store


def rotating_grant(calls):
    def grant(rt):
        calls.append(rt)
        return "alive", {"access_token": "fake-next-access", "refresh_token": rt + "-next",
                         "expires_in": 3600}
    return grant


@pytest.mark.parametrize("fresh", [True, False], ids=["access-fresh", "access-expired"])
@pytest.mark.parametrize("kind", STEADY_STATE_LANES)
def test_incident_heal_recovers_steady_state_lane(incident, monkeypatch, kind, fresh):
    cfg, state, snap, mount = incident
    cfg["poll_only_lane_maintenance"] = False
    expiry = int((time.time() + 3600) * 1000) if fresh else 1000
    store = steady_state_lane(incident, kind, expiry)
    calls = []
    monkeypatch.setattr(cus, "_oauth_refresh_grant", rotating_grant(calls))
    assert cus._auto_heal_live_lanes(state, cfg) == ["slot-1"]
    rt = cus._credential_refresh_token
    assert calls == ["fake-chain"]
    assert rt(cus.read_json(mount)) == "fake-chain-next"
    # The source store carries the new chain, so it never again presents the
    # spent one; the shadow follows the healed mount.
    assert rt(cus.read_json(store)) == "fake-chain-next"
    assert rt(cus.read_json(cus._lane_lastvalid_path(mount))) == "fake-chain-next"


@pytest.mark.parametrize("rival", ["live-slot", "idle-slot", "shared-mount", "other-family"])
@pytest.mark.parametrize("kind", STEADY_STATE_LANES)
def test_incident_heal_still_refuses_token_held_elsewhere(incident, monkeypatch, kind, rival):
    cfg, state, snap, mount = incident
    cfg["poll_only_lane_maintenance"] = False
    steady_state_lane(incident, kind, 1000)
    if rival == "shared-mount":
        holder = cus.CREDS_JSON
    elif rival == "other-family":
        holder = cus.login_family_creds_path("sample", "family-2")
    else:
        holder = cus.mount_creds_path(cus.slot_path("slot-2"))
    holder.parent.mkdir(parents=True, exist_ok=True)
    cus.write_json(holder, creds(rt="fake-chain", expiry=int((time.time() + 3600) * 1000)))
    if rival == "live-slot":
        lane = mount.parent
        other = holder.parent
        monkeypatch.setattr(cus, "mount_pids", lambda p: [123] if p in (lane, other) else [])
    calls = []
    monkeypatch.setattr(cus, "_oauth_refresh_grant", rotating_grant(calls))
    before = holder.read_bytes()
    assert cus._auto_heal_live_lanes(state, cfg) == []
    assert calls == []
    assert holder.read_bytes() == before
    assert cus.read_json(mount) == BLANK


def test_incident_unheld_install_keeps_family_for_second_lane(monkeypatch):
    """A plain install of an account no other mount holds uses the snapshot,
    so the account's one free family is left for a second concurrent lane."""
    from test_login_pool import _Env
    env = _Env(accounts=("sample", "other"))
    try:
        env.set_config({"mode": "per_session", "independent_logins": {"use_independent_logins": True}})
        env.plant_family("sample", "family-1", "fake-independent")
        lane_a = env.make_slot("other", live=True)
        lane_b = env.make_slot("other", live=True)
        calls = []
        monkeypatch.setattr(cus, "_oauth_refresh_grant", lambda rt: calls.append(rt) or ("alive", {
            "access_token": "fake-verified-access", "refresh_token": "fake-verified-refresh",
            "expires_in": 3600}))
        launch_a = cus._launch_install_plan("sample", lane_a, cus.slot_path(lane_a),
                                            cus.load_state(), cus.load_config())
        assert launch_a["kind"] == "snapshot" and not launch_a["pool_exhausted"]
        cus.execute_swap("sample", slot=lane_a)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        rt = cus._credential_refresh_token
        assert rt(cus.read_json(cus.mount_creds_path(cus.slot_path(lane_a)))) == "rt-sample"
        assert cus.slot_leased_family(cus.load_state(), lane_a) is None
        assert calls == []
        # The second lane on the same account still launches, on the family.
        launch_b = cus._launch_install_plan("sample", lane_b, cus.slot_path(lane_b),
                                            cus.load_state(), cus.load_config())
        assert launch_b["kind"] == "pooled-family" and launch_b["family"] == "family-1"
        cus.execute_swap("sample", slot=lane_b)
        assert cus.slot_leased_family(cus.load_state(), lane_b) == ("sample", "family-1")
        assert rt(cus.read_json(cus.mount_creds_path(cus.slot_path(lane_b)))) == "fake-verified-refresh"
    finally:
        env.restore()
