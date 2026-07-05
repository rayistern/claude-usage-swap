"""Tests for GH #141 FOLLOW-UP — detect + auto-heal a blanked LIVE LANE mount.

The GH #141 fix healed only the SHARED mount (~/.claude/.credentials.json); the
runtime detection (`_diagnose_live_mount_creds`) and heal (`_auto_heal_live_mount`)
never looked at per-slot LANE mounts. Incident (2026-07-05): slot-2's own mount
creds (slot-N/.credentials.json — the file that lane's session reads) went BLANK
(empty accessToken, expiresAt: 0) via an external writer (Claude's own logout / a
crash mid-write). `cus sos` reported all-clear and the daemon never healed it, so
it had to be fixed by hand. This suite proves the lane analogue of #141:

  Detection (`_blanked_live_lanes` / diagnose Condition 0b): a LIVE lane with
    blanked mount creds surfaces an URGENT SOS; an idle/observe-only slot or a
    healthy lane does not.
  Auto-heal (`_auto_heal_live_lanes`): the daemon reinstalls the lane account's
    newest USABLE creds into the blanked lane mount — from the account
    snapshot/backup for a plain lane, or from the LEASED login family for a pooled
    lane (never cross-contaminating token families, GH #104) — and only falls
    through to the URGENT relogin SOS when no usable source exists.

Run standalone:  python3 tests/test_lane_mount_blank_heal.py
Run under pytest: pytest tests/test_lane_mount_blank_heal.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _valid(access: str = "at-live", refresh: str = "rt-live",
           expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _blank() -> dict:
    """The exact 2026-07-05 incident signature: epoch expiresAt + empty token."""
    return {"claudeAiOauth": {"accessToken": "", "refreshToken": "", "expiresAt": 0}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it, so the
    lane detection + heal + diagnose run for real against temp files (no live-machine
    mutation). Mirrors the shared-mount suite (test_auto_heal_blank_live_mount) plus
    the login-pool suite's slot/family/live-mount mocking (test_login_pool)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 mode: str = "per_session", config: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

        # Shared mount stays HEALTHY so the shared-mount check never adds noise —
        # this suite is only about lanes.
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

        # /proc mock: a slot dir counts as "in use" iff its name is in live_slots
        # (same pattern as test_login_pool). mount_in_use → mount_pids → this.
        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()

    # -- slot / family helpers ------------------------------------------------

    def make_slot(self, account: str, live: bool, mount_creds: dict | None,
                  family_id: str | None = None) -> str:
        """Create a slot holding `account` with `mount_creds` in its own mount
        `.credentials.json` (None → no creds file at all). Optionally lease a
        pooled family. Returns the slot name."""
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

    def plant_family(self, account: str, family_id: str, creds: dict) -> None:
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(creds))

    def make_backup(self, name: str, creds: dict) -> None:
        """Write `creds` as a rotated backup generation for `name` via the real
        backup primitive (so timestamps sort correctly), leaving the snapshot as-is."""
        p = self.accounts_dir / f"account-{name}" / ".credentials.json"
        saved = p.read_text()
        p.write_text(json.dumps(creds))
        cus.backup_credentials_file(p)
        p.write_text(saved)

    # -- readback helpers -----------------------------------------------------

    def slot_creds(self, slot: str) -> dict | None:
        p = cus.slot_path(slot) / ".credentials.json"
        return json.loads(p.read_text()) if p.exists() else None

    def state(self) -> dict:
        return json.loads(self.state_json.read_text())

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def _lane_conditions(conditions):
    return [c for c in conditions if "mount creds are blanked" in c.summary]


# ---------------------------------------------------------------------------
# (a) live lane with blanked mount + usable account snapshot → auto-restored,
#     no SOS
# ---------------------------------------------------------------------------

def test_a_live_lane_blank_heals_from_snapshot_and_clears_sos():
    env = _Env({"rayi": _valid("at-snap", "rt-rayi")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        # Precondition: the blanked live lane is diagnosed before healing.
        conds = _lane_conditions(cus.diagnose())
        assert len(conds) == 1 and conds[0].severity == "urgent"
        assert slot in conds[0].summary and "rayi" in conds[0].summary

        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [slot]
        # Lane mount now valid and carries the account snapshot's tokens.
        assert cus._live_mount_creds_invalid(env.slot_creds(slot)) is False
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-snap"
        # SOS gone after the heal.
        assert _lane_conditions(cus.diagnose()) == []
        # The blanked mount was preserved as a backup → heal is reversible.
        assert sorted((cus.slot_path(slot)).glob(".credentials.json.bak.*")), "expected a lane backup"
    finally:
        env.restore()


def test_a_heals_from_newest_backup_when_snapshot_also_blank():
    # Account snapshot ALSO blank, but a valid backup generation exists → heal
    # from the newest usable backup (the _newest_usable_creds_source fallback).
    env = _Env({"rayi": _blank()}, active="rayi", mode="per_session")
    try:
        env.make_backup("rayi", _valid("at-backup", "rt-backup"))
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [slot]
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-backup"
        assert _lane_conditions(cus.diagnose()) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) blanked + no usable source → no heal, URGENT SOS remains (needs relogin)
# ---------------------------------------------------------------------------

def test_b_no_usable_source_leaves_urgent_sos():
    # Both the lane mount AND the account snapshot are blank, no backups → nothing
    # usable to install; heal is a no-op and the URGENT relogin SOS must fire.
    env = _Env({"rayi": _blank()}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == []
        assert cus._live_mount_creds_invalid(env.slot_creds(slot)) is True  # still blank
        conds = _lane_conditions(cus.diagnose())
        assert len(conds) == 1 and conds[0].severity == "urgent"
        assert "relogin rayi" in conds[0].action
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) idle / observe-only slot with blank creds → NOT flagged and NOT healed
# ---------------------------------------------------------------------------

def test_c_idle_slot_blank_is_not_flagged_or_healed():
    env = _Env({"rayi": _valid("at-snap", "rt-rayi")}, active="rayi", mode="per_session")
    try:
        # live=False → no PID on the mount → not a live lane.
        slot = env.make_slot("rayi", live=False, mount_creds=_blank())
        assert cus._blanked_live_lanes(cus.load_state(), cus.load_config()) == []
        assert _lane_conditions(cus.diagnose()) == []
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == []
        # The idle slot's blank mount is left exactly as-is (not "healed").
        assert cus._live_mount_creds_invalid(env.slot_creds(slot)) is True
    finally:
        env.restore()


def test_c_slot_without_registered_account_is_ignored():
    # A slot mid-swap with no committed account must not be treated as a live lane
    # (occupied_slot_accounts skips account-less slots).
    env = _Env({"rayi": _valid("at-snap", "rt-rayi")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_blank())
        state = cus.load_state()
        state["slots"][slot].pop("account", None)  # simulate not-yet-committed slot
        cus.save_state(state)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        assert cus._blanked_live_lanes(cus.load_state(), cus.load_config()) == []
    finally:
        env.restore()


def test_c_global_mode_does_not_scan_lanes():
    # Lane heal/detection is scoped to lane-serving modes (per_session/hybrid),
    # the complement of the shared-mount check (global/hybrid).
    env = _Env({"rayi": _valid("at-snap", "rt-rayi")}, active="rayi", mode="global")
    try:
        env.make_slot("rayi", live=True, mount_creds=_blank())
        assert cus._blanked_live_lanes(cus.load_state(), cus.load_config()) == []
        assert cus._auto_heal_live_lanes(cus.load_state(), cus.load_config()) == []
        assert _lane_conditions(cus.diagnose()) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) healthy live lane → no-op (byte-for-byte untouched, no backup)
# ---------------------------------------------------------------------------

def test_d_healthy_lane_is_noop():
    env = _Env({"rayi": _valid("at-snap", "rt-rayi")}, active="rayi", mode="per_session")
    try:
        slot = env.make_slot("rayi", live=True, mount_creds=_valid("at-lane", "rt-rayi"))
        before = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == []
        after = (cus.slot_path(slot) / ".credentials.json").read_bytes()
        assert after == before  # untouched
        assert list((cus.slot_path(slot)).glob(".credentials.json.bak.*")) == []
        assert _lane_conditions(cus.diagnose()) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (e) a lane on a pooled family heals from THAT family's creds (not the snapshot)
# ---------------------------------------------------------------------------

def test_e_pooled_lane_heals_from_leased_family():
    cfg = {"mode": "per_session", "independent_logins": {"use_independent_logins": True}}
    # Account snapshot is deliberately a DIFFERENT token than the family, so we can
    # prove the heal used the family store (not the snapshot).
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", config=cfg)
    try:
        env.plant_family("rayi", "family-1", _valid("at-fam", "rt-fam"))
        slot = env.make_slot("rayi", live=True, mount_creds=_blank(), family_id="family-1")
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == [slot]
        # Healed FROM the leased family, honoring #104 login-family discipline.
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == "at-fam"
        assert _lane_conditions(cus.diagnose()) == []
    finally:
        env.restore()


def test_e_pooled_lane_no_cross_family_fallback_to_snapshot():
    # The leased family store is itself blanked. Even though the account snapshot
    # is healthy, the pooled lane must NOT heal from it (that would cross-
    # contaminate token families, #104) → no heal, URGENT relogin SOS instead.
    cfg = {"mode": "per_session", "independent_logins": {"use_independent_logins": True}}
    env = _Env({"rayi": _valid("at-snap", "rt-snap")}, active="rayi", config=cfg)
    try:
        env.plant_family("rayi", "family-1", _blank())  # family store also blanked
        slot = env.make_slot("rayi", live=True, mount_creds=_blank(), family_id="family-1")
        healed = cus._auto_heal_live_lanes(cus.load_state(), cus.load_config())
        assert healed == []
        assert cus._live_mount_creds_invalid(env.slot_creds(slot)) is True  # left blank
        # Did NOT borrow the healthy snapshot's token.
        assert env.slot_creds(slot)["claudeAiOauth"]["accessToken"] == ""
        conds = _lane_conditions(cus.diagnose())
        assert len(conds) == 1 and conds[0].severity == "urgent"
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
