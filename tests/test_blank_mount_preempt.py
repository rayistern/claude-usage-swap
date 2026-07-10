"""Tests for the 2026-07-07 blank-mount durable fixes (three layers).

Context — the "classic blank-mount failure": a live lane's mount
`.credentials.json` gets BLANKED (accessToken:"" / expiresAt:0) because Claude
Code's OWN in-place OAuth refresh, attempted as the token nears expiry, fails
mid-write. The session then shows "Not logged in · Run /login". The existing
reactive auto-heal (GH #141 follow-up) restores the FILE, but that is
insufficient: (1) Claude Code re-blanks on its next failed refresh (heal→blank→
heal), and (2) a file-heal cannot un-stick a live process that already cached
the logged-out state — it needs a RELAUNCH. Net: on 2026-07-07 tabby-5/slot-7
(rayi6) sat "not logged in" until a human noticed.

This suite proves the three fixes:

  (Fix 1) PREEMPT — `_preempt_live_lane_blanks`: when a live lane is near-expiry
    AND idle (the state that PRECEDES the blank), cus proactively refreshes the
    mount token via its OWN refresh grant and writes fresh creds into the mount,
    so Claude Code never needs its fragile in-place refresh. Gated, cooldown-
    backed, degrades to warn+heal on failure.
  (Fix 2) STUCK escalation — `_diagnose_stuck_lanes`: a lane healed repeatedly
    (heal→blank→heal within the window) emits a distinct URGENT telling the
    operator a file-heal won't help and naming the relaunch command.
  (Fix 3) FORENSICS — `_auto_heal_live_lanes` emits `op=blank-detected` /
    `op=blank-heal` / `op=blank-preempt` CRED-AUDIT lines (mtime, staleness,
    prior-valid fingerprint vs the now-blank state, triggering expiry) and keeps
    a per-lane `.credentials.json.lastvalid` shadow for instant heal + diffing.

Run standalone:  python3 tests/test_blank_mount_preempt.py
Run under pytest: pytest tests/test_blank_mount_preempt.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _valid(access: str = "at-live", refresh: str = "rt-live",
           expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _blank() -> dict:
    """The exact blank-mount signature: epoch expiresAt + empty token."""
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it, so the
    lane preempt + heal + forensics run for real against temp files. Mirrors the
    harness in test_lane_mount_blank_heal.py, plus echo capture (for CRED-AUDIT
    assertions), mount-mtime backdating (to exercise the not-recently-written
    gate), and generic monkeypatch-with-restore."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 mode: str = "per_session", config: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        # Shared mount stays HEALTHY so its check never adds noise — lanes only.
        self.creds_json = self.claude_dir / ".credentials.json"
        self.creds_json.write_text(json.dumps(_valid("at-shared", "rt-shared")))
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(
            {"userID": f"uid-{active}",
             "oauthAccount": {"accountUuid": f"uuid-{active}", "emailAddress": f"{active}@x"}}))

        for name, creds in accounts.items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(
                {"userID": f"uid-{name}",
                 "oauthAccount": {"accountUuid": f"uuid-{name}", "emailAddress": f"{name}@x"}}))

        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50,
                             "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {"mode": mode})
        self.inbox_md = self.accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "HOME", "CLAUDE_DIR", "CREDS_JSON", "CLAUDE_JSON", "ACCOUNTS_DIR",
            "STATE_JSON", "CONFIG_YAML", "INBOX_MD")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.INBOX_MD = self.inbox_md

        # /proc mock: a slot dir is "in use" iff its name is in live_slots.
        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # In-process blank tracking is module-global — start every test clean.
        cus._reset_blank_tracking()

        # Capture click.echo so tests can assert on CRED-AUDIT lines.
        self.echoes: list[str] = []
        self._patches: list[tuple[object, str, object]] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")

    # -- helpers --------------------------------------------------------------

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def make_slot(self, account: str, live: bool, mount_creds: dict | None,
                  family_id: str | None = None) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        if mount_creds is not None:
            (d / ".credentials.json").write_text(json.dumps(mount_creds))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{account}@x"}}))
        state["slots"][name]["account"] = account
        if family_id:
            state["slots"][name]["login_family"] = f"{account}/{family_id}"
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def set_live(self, slot: str, live: bool) -> None:
        (self.live_slots.add if live else self.live_slots.discard)(slot)
        cus._OCCUPIED_SLOTS_CACHE.clear()

    def write_mount(self, slot: str, creds: dict) -> None:
        (cus.slot_path(slot) / ".credentials.json").write_text(json.dumps(creds))

    def backdate_mount(self, slot: str, seconds: float) -> None:
        """Age the mount's mtime so it reads as NOT recently written."""
        p = cus.slot_path(slot) / ".credentials.json"
        t = time.time() - seconds
        os.utime(p, (t, t))

    def slot_creds(self, slot: str) -> dict | None:
        p = cus.slot_path(slot) / ".credentials.json"
        return json.loads(p.read_text()) if p.exists() else None

    def shadow_path(self, slot: str) -> Path:
        return cus._lane_lastvalid_path(cus.mount_creds_path(cus.slot_path(slot)))

    def audit_lines(self, op: str) -> list[str]:
        return [e for e in self.echoes
                if e.startswith(f"{cus.CRED_AUDIT_PREFIX} op={op} ")
                or e.startswith(f"{cus.CRED_AUDIT_PREFIX} op={op}")]

    def restore(self) -> None:
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        cus._reset_blank_tracking()
        self._tmp.cleanup()


def _near_expiry_ms(minutes: float) -> int:
    return int(time.time() * 1000) + int(minutes * 60_000)


# ---------------------------------------------------------------------------
# (a) PREEMPT — refreshes + reinstalls a near-expiry, idle lane
# ---------------------------------------------------------------------------

def test_a_preempt_refreshes_near_expiry_idle_lane():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        # Mount valid but token 20 min from expiry (within the 45-min window) and
        # NOT recently written → the near-expiry-AND-idle preempt trigger.
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        env.backdate_mount(slot, seconds=3600)

        # Stub the refresh grant to write a fresh token into the passed creds_path
        # and prove the preempt targets the MOUNT (creds_path=mount), not the snap.
        calls: list[tuple[str, Path]] = []

        def fake_refresh(account, creds_path=None):
            calls.append((account, Path(creds_path)))
            c = json.loads(Path(creds_path).read_text())
            c["claudeAiOauth"] = {"accessToken": "at-fresh", "refreshToken": "rt-fresh",
                                  "expiresAt": _near_expiry_ms(480)}  # 8h out
            Path(creds_path).write_text(json.dumps(c))
            return True

        env.patch(cus, "_refresh_account_token", fake_refresh)

        preempted = cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config())
        assert preempted == [slot]
        # Refreshed the MOUNT file, not the snapshot.
        assert calls == [("rayi", cus.mount_creds_path(cus.slot_path(slot)))]
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-fresh"
        # Emitted a blank-preempt audit line naming slot/account + fingerprints.
        lines = env.audit_lines("blank-preempt")
        assert len(lines) == 1
        assert "decision=wrote" in lines[0] and f"slot={slot}" in lines[0] and "account=rayi" in lines[0]
        assert "prev_token_fp=" in lines[0] and "new_expiry=" in lines[0]
        # Fix 3: a last-valid shadow now exists for the (fresh) mount.
        assert env.shadow_path(slot).exists()
    finally:
        env.restore()


def test_a_preempt_skips_healthy_far_expiry_lane_but_updates_shadow():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        # Token hours from expiry → healthy, no preempt. Shadow still refreshed.
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-ok", "rt-ok", _near_expiry_ms(600)))
        env.backdate_mount(slot, seconds=3600)

        def fake_refresh(account, creds_path=None):
            raise AssertionError("must not refresh a healthy lane")

        env.patch(cus, "_refresh_account_token", fake_refresh)
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
        assert env.shadow_path(slot).exists()  # valid mount → shadow taken
        assert env.audit_lines("blank-preempt") == []
    finally:
        env.restore()


def test_a_preempt_skips_recently_written_lane():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        # Near expiry BUT just written (fresh mtime) → self-maintaining, no preempt.
        env.make_slot("rayi", live=True,
                      mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        # (no backdate → mtime is now)
        env.patch(cus, "_refresh_account_token",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh")))
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_a_preempt_skips_already_blank_lane():
    # An already-blank mount is the reactive heal's job, never the preempt's.
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        env.backdate_mount(slot, seconds=3600)
        env.patch(cus, "_refresh_account_token",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("no refresh on blank")))
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_a_preempt_cooldown_blocks_second_attempt():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        env.backdate_mount(slot, seconds=3600)
        n = {"c": 0}

        def fake_refresh(account, creds_path=None):
            n["c"] += 1
            c = json.loads(Path(creds_path).read_text())
            # Re-arm the trigger: keep it near-expiry + backdate so ONLY the
            # cooldown (not the recently-written gate) can suppress the 2nd call.
            c["claudeAiOauth"]["expiresAt"] = _near_expiry_ms(20)
            Path(creds_path).write_text(json.dumps(c))
            t = time.time() - 3600
            os.utime(Path(creds_path), (t, t))
            return True

        env.patch(cus, "_refresh_account_token", fake_refresh)
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == [slot]
        # Immediate second cycle → cooldown (default 15 min) suppresses it.
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
        assert n["c"] == 1
    finally:
        env.restore()


def test_a_preempt_degrades_on_refresh_failure():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        env.backdate_mount(slot, seconds=3600)
        before = env.slot_creds(slot)
        env.patch(cus, "_refresh_account_token", lambda *a, **k: False)  # grant fails
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
        # Mount untouched → left for the warn+heal path (degrade, don't worsen).
        assert env.slot_creds(slot) == before
        lines = env.audit_lines("blank-preempt")
        assert len(lines) == 1 and "decision=skipped-refresh-failed" in lines[0]
    finally:
        env.restore()


def test_a_preempt_disabled_by_config_is_noop():
    cfg = {"mode": "per_session", "creds_warn": {"preempt": {"enabled": False}}}
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", config=cfg)
    try:
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        env.backdate_mount(slot, seconds=3600)
        env.patch(cus, "_refresh_account_token",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("disabled")))
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_a_preempt_global_mode_noop():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="global")
    try:
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-old", "rt-old", _near_expiry_ms(20)))
        env.backdate_mount(slot, seconds=3600)
        env.patch(cus, "_refresh_account_token",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("global no-op")))
        assert cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) STUCK-SESSION — repeated heals cross the threshold → URGENT relaunch SOS
# ---------------------------------------------------------------------------

def test_b_repeated_blank_heals_emit_stuck_sos():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())

        # Heal #1.
        assert cus._auto_heal_live_lanes(cus.load_state(), cus.load_config()) == [slot]
        # One heal is below the default threshold (2) → no stuck SOS yet.
        assert cus._diagnose_stuck_lanes(cus.load_state(), cus.load_config()) == []

        # Re-blank (Claude Code's next failed refresh) then heal #2.
        env.write_mount(slot, _blank())
        assert cus._auto_heal_live_lanes(cus.load_state(), cus.load_config()) == [slot]

        conds = cus._diagnose_stuck_lanes(cus.load_state(), cus.load_config())
        assert len(conds) == 1
        c = conds[0]
        assert c.severity == "urgent"
        assert "keeps blanking" in c.summary and "RELAUNCH" in c.summary
        # Names the exact in-place relaunch command.
        assert f"cus launch rayi --lane {slot} -- --continue" in c.action
    finally:
        env.restore()


def test_b_stuck_sos_clears_when_session_exits():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        env.write_mount(slot, _blank())
        cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert len(cus._diagnose_stuck_lanes(cus.load_state(), cus.load_config())) == 1
        # Session exits → lane no longer live → not stuck anymore.
        env.set_live(slot, False)
        assert cus._diagnose_stuck_lanes(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_b_stuck_window_prunes_old_heals():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_valid("at-lane", "rt-lane"))
        key = (slot, "rayi")
        # Two heals but BOTH older than the 30-min window → pruned, no SOS.
        old = time.time() - 40 * 60
        cus._LANE_HEAL_HISTORY[key] = [old, old + 5]
        assert cus._diagnose_stuck_lanes(cus.load_state(), cus.load_config()) == []
        assert key not in cus._LANE_HEAL_HISTORY  # fully aged out → forgotten
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) FORENSICS — blank-detect / blank-heal CRED-AUDIT lines + shadow copy
# ---------------------------------------------------------------------------

def test_c_blank_detect_and_heal_audit_lines():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        # Start valid so a last-valid shadow gets taken (via a preempt-scan pass),
        # then blank the mount so the heal can diff blank-vs-lastvalid.
        slot = env.make_slot("rayi", live=True, mount_creds=_valid("at-good", "rt-good"))
        cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config())  # takes shadow
        assert env.shadow_path(slot).exists()

        env.write_mount(slot, _blank())
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [slot]

        det = env.audit_lines("blank-detected")
        assert len(det) == 1
        d = det[0]
        for field in ("decision=detected", f"slot={slot}", "account=rayi",
                      "mount_mtime=", "last_valid_age=", "blank_expiry=",
                      "prev_valid_fp=sha256:", "prev_valid_expiry="):
            assert field in d, f"missing {field} in blank-detected: {d}"
        # The now-blank state has no usable token → token_fp=none; the prior-valid
        # fingerprint is the shadow's real fingerprint (the two differ = evidence).
        assert "token_fp=none" in d
        assert "blank_expiry=0(" in d  # epoch signature logged verbatim

        heal = env.audit_lines("blank-heal")
        assert len(heal) == 1
        h = heal[0]
        for field in ("decision=wrote", f"slot={slot}", "account=rayi",
                      "source=", "restored_expiry=", "token_fp=sha256:"):
            assert field in h, f"missing {field} in blank-heal: {h}"
    finally:
        env.restore()


def test_c_never_logs_raw_tokens():
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("SECRET-ACCESS", "SECRET-REFRESH"))
        cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config())
        env.write_mount(slot, _blank())
        cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        blob = "\n".join(env.echoes)
        assert "SECRET-ACCESS" not in blob and "SECRET-REFRESH" not in blob
    finally:
        env.restore()


def test_c_shadow_is_instant_heal_source_for_plain_lane():
    # After a valid scan takes a shadow, a blanked plain lane heals FROM the shadow
    # even though the account snapshot differs — proving the shadow is preferred.
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_valid("at-mount", "rt-mount"))
        cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config())  # shadow = at-mount
        env.write_mount(slot, _blank())
        assert cus._auto_heal_live_lanes(cus.load_state(), cus.load_config()) == [slot]
        # Healed from the shadow (at-mount), NOT the account snapshot (at-snap).
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-mount"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) unchanged: pooled lane never heals from the shadow (strict #104 family)
# ---------------------------------------------------------------------------

def test_d_pooled_lane_ignores_shadow_uses_family():
    cfg = {"mode": "per_session", "independent_logins": {"use_independent_logins": True}}
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", config=cfg)
    try:
        d = cus.login_family_dir("rayi", "family-1")
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path("rayi", "family-1").write_text(json.dumps(_valid("at-fam", "rt-fam")))
        slot = env.make_slot("rayi", live=True,
                             mount_creds=_valid("at-mount", "rt-mount"), family_id="family-1")
        # Take a shadow (at-mount) via a scan, then blank the mount.
        cus._preempt_live_lane_blanks(cus.load_state(), cus.load_config())
        env.write_mount(slot, _blank())
        assert cus._auto_heal_live_lanes(cus.load_state(), cus.load_config()) == [slot]
        # A pooled lane MUST heal from its leased family, never the shadow (#104).
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-fam"
    finally:
        env.restore()


if __name__ == "__main__":
    import traceback

    funcs = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
