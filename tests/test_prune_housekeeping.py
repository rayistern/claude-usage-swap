"""Tests for the GH #190 housekeeping scaffold (Mechanism 3): `cus prune`,
the canonical→family reseed, the idle-slot audit, dead-lease release, the
acquire_slot deprioritization, and the opt-in daemon sweep.

Contract proven here:
  (1) free-family probe: a dead FREE family is reported (no rename) by
      default and renamed `.dead-<date>` with execute=True; a LEASED family's
      grant is NEVER called (the #104 invariant);
  (2) an alive free family gets its rotation PERSISTED into the store;
  (3) OOB-relogin detection: fresh canonical + blank family + dead idle-slot
      mount → one warning SOS pointing at `cus prune --reseed`, zero writes;
  (4) --reseed: the new family holds the ROTATED generation (grant called
      exactly once), provenance recorded, pre-existing dead families retired,
      canonical bytes untouched (knowingly dead-branched, never rewritten);
  (5) reseed with a DEAD canonical: scaffold removed, "relogin required";
  (6) idle-slot audit: idle+blank flagged; live-blank and healthy-idle not;
  (7) daemon sweep OFF by default → zero probes / zero writes on a
      default-config call; ON → runs once then respects the interval;
  (8) acquire_slot: a blank-mount preferred slot is skipped for a clean
      same-account slot; a healthy preferred slot is still chosen first
      (regression);
  (9) `cus prune` report-only + --no-probe is zero-network.

Run standalone:  python3 tests/test_prune_housekeeping.py
Run under pytest: pytest tests/test_prune_housekeeping.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# creds shapes (mirrors test_launch_liveness_gate)
# ---------------------------------------------------------------------------

# Far-future expiry: a currently-VALID access token (2033-ish in ms).
_FUTURE = 2_000_000_000_000
# Long-past expiry: EXPIRED but well-shaped — forces past the cheap path.
_PAST = 1_000


def _valid(access: str = "at-live", refresh: str = "rt-live", expires_at: int = _FUTURE) -> dict:
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh, "expiresAt": expires_at}}


def _expired(refresh: str) -> dict:
    """Well-SHAPED but suspect: only the refresh grant can tell dead from
    merely-expired."""
    return {"claudeAiOauth": {"accessToken": "at-expired", "refreshToken": refresh, "expiresAt": _PAST}}


def _blank() -> dict:
    """The #141 blank signature, no refresh token: disk-only dead."""
    return {"claudeAiOauth": {"accessToken": "", "expiresAt": 0}}


def _blank_with_rt(refresh: str) -> dict:
    """Blank-shaped but still carrying a refresh token — the shape a failed
    in-place refresh leaves on a store whose branch an OOB relogin killed."""
    return {"claudeAiOauth": {"accessToken": "", "expiresAt": 0, "refreshToken": refresh}}


class _Env:
    """Throwaway on-disk tree with every cus path constant repointed at it
    (copied from test_launch_liveness_gate._Env: slots + families + echo
    capture + /proc live-mount mock)."""

    def __init__(self, accounts: dict[str, dict], active: str,
                 config: dict | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.root = root
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        self.accounts_dir.mkdir(parents=True)

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
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0,
                             "current_7d_pct": 0.0} for n in accounts},
            "slots": {},
            "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {"mode": "per_session"})
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

        self._saved_mount_pids = cus.mount_pids
        self.live_slots: set[str] = set()
        cus.mount_pids = lambda mount: [1] if Path(mount).name in self.live_slots else []
        cus._OCCUPIED_SLOTS_CACHE.clear()
        cus._reset_blank_tracking()

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")
        self._patches: list[tuple[object, str, object]] = []

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def make_slot(self, account: str | None, live: bool, mount_creds: dict | None,
                  family_id: str | None = None) -> str:
        state = cus.load_state()
        name, d = cus.create_slot(state)
        if mount_creds is not None:
            (d / ".credentials.json").write_text(json.dumps(mount_creds))
        (d / ".claude.json").write_text(json.dumps(
            {"oauthAccount": {"emailAddress": (f"{account}@x" if account else "empty@x")}}))
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


def _grant_map(mapping: dict[str, tuple], calls: list | None = None):
    """Fake `_oauth_refresh_grant(rt)` from {refresh_token: verdict-tuple}.
    Unmapped tokens raise so a test fails loudly on an unexpected probe."""
    def _grant(rt):
        if calls is not None:
            calls.append(rt)
        if rt not in mapping:
            raise AssertionError(f"unexpected refresh-grant probe of {rt!r}")
        return mapping[rt]
    return _grant


def _no_grant():
    """A grant that must never fire (zero-network assertions)."""
    def _grant(rt):
        raise AssertionError(f"refresh-grant probe fired unexpectedly for {rt!r}")
    return _grant


_ILGATE = {"independent_logins": {"use_independent_logins": True}, "mode": "per_session"}


# ---------------------------------------------------------------------------
# (1) free-family probe: dead free reported/retired; LEASED never probed
# ---------------------------------------------------------------------------

def test_free_family_probe_report_then_execute_leased_never_probed():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # family-1 leased by a LIVE slot (its token must never be granted:
        # "rt-leased" is not in the grant map, so a probe would raise).
        env.make_slot("acct", live=True, mount_creds=_valid("at-l", "rt-leased"),
                      family_id="family-1")
        env.plant_family("acct", "family-1", _expired("rt-leased"))
        # family-2 free and DEAD.
        env.plant_family("acct", "family-2", _expired("rt-dead"))
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-dead": ("dead", None)}, calls=grant_calls))
        state, config = cus.load_state(), cus.load_config()

        # Report-only: dead family reported, file NOT renamed.
        rows = cus._prune_free_families(state, config, execute=False, probe=True)
        fam2 = [r for r in rows if r["kind"] == "family" and r["family"] == "family-2"]
        assert fam2 and fam2[0]["dead"] and fam2[0]["retired"] is None, rows
        fam2_path = cus.login_family_creds_path("acct", "family-2")
        assert fam2_path.exists(), "report-only must not rename the store"
        # The leased family produced NO row and NO probe.
        assert not [r for r in rows if r.get("family") == "family-1"], rows

        # Execute: renamed .dead-<date>; the cached verdict means no 2nd grant.
        rows = cus._prune_free_families(state, config, execute=True, probe=True)
        fam2 = [r for r in rows if r["kind"] == "family" and r["family"] == "family-2"]
        assert fam2 and fam2[0]["dead"] and fam2[0]["retired"], rows
        assert not fam2_path.exists()
        assert [p.name for p in fam2_path.parent.iterdir() if ".dead-" in p.name]
        assert grant_calls == ["rt-dead"], (
            f"one probe total (cooldown-cached), leased never probed: {grant_calls}")
        # Pool depth row present (0 live free after the retirement).
        depth = [r for r in rows if r["kind"] == "pool_depth" and r["account"] == "acct"]
        assert depth and depth[0]["live_free"] == 0, rows
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (1b) F-O-1 shield (GH #193 review): a DISTINCT-id FREE family sharing a LIVE
#      mount's refresh generation is NEVER probed/retired — probing rotates the
#      shared single-use token and logs the live holder out (#104; confirmed
#      live logout 2026-08-30). Distinct from (1)'s leased-by-id skip: here the
#      family is NOT leased (no login_family on the slot), so only the fingerprint
#      shield — not leased_families — can protect it.
# ---------------------------------------------------------------------------

def test_free_family_sharing_live_mount_generation_never_probed():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # A LIVE mount holding refresh generation "rt-live-gen". Crucially it is NOT
        # leased to the free family below (no family_id), so leased_families won't
        # shield that family — only the F-O-1 refresh-fingerprint shield can.
        env.make_slot("acct", live=True, mount_creds=_valid("at-live", "rt-live-gen"))
        # A FREE family with a DISTINCT id but the SAME single-use refresh token as
        # the live mount (a duplicate-generation copy, e.g. an unrotated
        # `--from-existing`). It is EXPIRED-shaped, so pre-shield the probe ladder
        # WOULD fire a refresh grant on it — which would rotate "rt-live-gen" and
        # dead-branch the live mount.
        env.plant_family("acct", "family-1", _expired("rt-live-gen"))
        grant_calls: list[str] = []
        # Empty grant map: ANY probe of "rt-live-gen" raises (unmapped) AND we
        # belt-and-braces assert no call was recorded.
        env.patch(cus, "_oauth_refresh_grant", _grant_map({}, calls=grant_calls))

        # execute=True + probe=True is the most aggressive pass; the shield must
        # still refuse to probe OR retire the shared-generation family.
        rows = cus._prune_free_families(cus.load_state(), cus.load_config(),
                                        execute=True, probe=True)

        # No refresh-grant probe fired for the shared generation.
        assert grant_calls == [], (
            f"F-O-1 shield must not probe a live-shared generation: {grant_calls}")
        # The store was NOT retired — still on disk under its live-shape name.
        fam_path = cus.login_family_creds_path("acct", "family-1")
        assert fam_path.exists(), "shield must not retire a live-shared free family"
        assert not [p for p in fam_path.parent.iterdir() if ".dead-" in p.name], \
            "shield must not rename the shared-generation store to .dead-*"
        # It emits no family row (hands off entirely, like the leased-skip) and is
        # not counted toward free-pool depth (a duplicate of a live generation is
        # not a usable free family).
        assert not [r for r in rows if r.get("family") == "family-1"], rows
        depth = [r for r in rows if r["kind"] == "pool_depth" and r["account"] == "acct"]
        assert depth and depth[0]["live_free"] == 0, rows
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) alive free family → rotation persisted into the store
# ---------------------------------------------------------------------------

def test_alive_free_family_rotation_persisted():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _expired("rt-old"))
        path = cus.login_family_creds_path("acct", "family-1")
        before = path.read_bytes()
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-old": ("alive", {"access_token": "at-new",
                                                   "refresh_token": "rt-new",
                                                   "expires_in": 3600})}))
        rows = cus._prune_free_families(cus.load_state(), cus.load_config(),
                                        execute=False, probe=True)
        fam = [r for r in rows if r["kind"] == "family"]
        assert fam == [{"kind": "family", "account": "acct", "family": "family-1",
                        "dead": False, "retired": None}], rows
        after = path.read_bytes()
        assert after != before, "alive probe must persist the rotated generation"
        assert cus._credential_refresh_token(json.loads(after)) == "rt-new"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) OOB-relogin detection: warning SOS, nothing written
# ---------------------------------------------------------------------------

def test_oob_relogin_detected_pure():
    env = _Env({"acct": _valid("at-fresh", "rt-fresh"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # Dependent 1: blank-shaped family store (still carries an rt so it's a
        # real store, not scaffold junk). Dependent 2: dead idle-slot mount.
        env.plant_family("acct", "family-1", _blank_with_rt("rt-stale"))
        env.make_slot("acct", live=False, mount_creds=_blank())
        env.patch(cus, "_oauth_refresh_grant", _no_grant())  # detection is PURE
        state = cus.load_state()
        state_before = json.dumps(state, sort_keys=True)
        fam_before = cus.login_family_creds_path("acct", "family-1").read_bytes()

        conds = cus._detect_oob_relogin(state, cus.load_config())
        assert len(conds) == 1, conds
        c = conds[0]
        assert (c.severity, c.affected) == ("warning", "acct")
        assert "2 stale login families/mounts under freshly-relogged 'acct'" in c.summary
        assert "cus prune --reseed acct" in c.action
        # Pure detection: zero writes.
        assert cus.login_family_creds_path("acct", "family-1").read_bytes() == fam_before
        assert json.dumps(cus.load_state(), sort_keys=True) == state_before
        # Healthy 'other' (no stale dependents) raised nothing.
        assert not [x for x in conds if x.affected == "other"]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (4) --reseed: generation transfer, canonical untouched
# ---------------------------------------------------------------------------

def test_reseed_transfers_generation_canonical_untouched():
    env = _Env({"acct": _valid("at-fresh", "rt-canon"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        # A pre-existing disk-dead family (blank, NO refresh token → dead with
        # no grant) that the reseed's step-1 sweep must retire.
        env.plant_family("acct", "family-1", _blank())
        snap = cus.account_creds_path("acct")
        canon_before = snap.read_bytes()
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-canon": ("alive", {"access_token": "at-xfer",
                                                     "refresh_token": "rt-canon2",
                                                     "expires_in": 3600})},
                             calls=grant_calls))

        fam = cus._reseed_family_from_canonical("acct", cus.load_state(), cus.load_config())
        assert fam == "family-2", fam  # next index after the (retired) family-1

        # New family holds the ROTATED generation; grant fired exactly once.
        new_creds = json.loads(cus.login_family_creds_path("acct", fam).read_text())
        assert cus._credential_refresh_token(new_creds) == "rt-canon2", new_creds
        assert grant_calls == ["rt-canon"], grant_calls
        # Provenance records the transfer.
        prov = json.loads(cus.login_family_provenance_path("acct", fam).read_text())
        assert "generation TRANSFER" in prov["note"] and prov["bootstrapped"] is True, prov
        # Dead family-1 retired.
        f1 = cus.login_family_creds_path("acct", "family-1")
        assert not f1.exists()
        assert [p.name for p in f1.parent.iterdir() if ".dead-" in p.name]
        # Canonical bytes bit-for-bit untouched (knowingly dead-branched).
        assert snap.read_bytes() == canon_before
        # The consequence is stated, not hidden.
        assert any("snapshot_refresh_dead" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) reseed with dead canonical → scaffold removed, relogin required
# ---------------------------------------------------------------------------

def test_reseed_dead_canonical_removes_scaffold():
    env = _Env({"acct": _expired("rt-canon-dead"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-canon-dead": ("dead", None)}))
        fam = cus._reseed_family_from_canonical("acct", cus.load_state(), cus.load_config())
        assert fam is None
        # No half-built family left for the claim path to trust.
        assert not cus.login_family_dir("acct", "family-1").exists()
        assert any("relogin required" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) idle-slot audit: idle+blank flagged; live / healthy-idle not
# ---------------------------------------------------------------------------

def test_idle_slot_audit_flags_only_dead_idle():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        dead_idle = env.make_slot("acct", live=False, mount_creds=_blank())
        env.make_slot("acct", live=True, mount_creds=_blank())        # live: skipped
        env.make_slot("other", live=False, mount_creds=_valid("at-h", "rt-h"))  # healthy idle
        flagged = cus._audit_idle_slots(cus.load_state(), cus.load_config())
        assert {f["slot"] for f in flagged} == {dead_idle}, flagged
        assert flagged[0]["account"] == "acct"
        assert "will fail on next launch" in flagged[0]["problem"]

        # A missing leased-family store is also flagged (second problem shape).
        lease_slot = env.make_slot("acct", live=False, mount_creds=_valid("at-l", "rt-l"),
                                   family_id="family-9")  # store never planted
        flagged = cus._audit_idle_slots(cus.load_state(), cus.load_config())
        by_slot = {f["slot"]: f for f in flagged}
        assert lease_slot in by_slot and "missing/retired" in by_slot[lease_slot]["problem"]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6b) dead-lease release: report vs execute; locked slots skipped
# ---------------------------------------------------------------------------

def test_release_dead_leases_execute_and_locked_skip():
    cfg = {**_ILGATE, "session_locks": {"locked_slots": ["slot-2"]}}
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=cfg)
    try:
        s1 = env.make_slot("acct", live=False, mount_creds=_valid("at-1", "rt-1"),
                           family_id="family-7")  # store missing → dead lease
        env.make_slot("acct", live=False, mount_creds=_valid("at-2", "rt-2"),
                      family_id="family-8")       # slot-2: LOCKED → untouchable
        state, config = cus.load_state(), cus.load_config()

        rel = cus._release_dead_leases(state, config, execute=False)
        assert rel == [f"{s1}: acct/family-7"], rel
        assert cus.load_state()["slots"][s1].get("login_family") == "acct/family-7", \
            "report-only must not pop the lease"

        rel = cus._release_dead_leases(state, config, execute=True)
        assert rel == [f"{s1}: acct/family-7"], rel
        fresh = cus.load_state()
        assert "login_family" not in fresh["slots"][s1]
        assert fresh["slots"]["slot-2"].get("login_family") == "acct/family-8", \
            "locked slot's lease must never be touched"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) daemon sweep: OFF by default = inert; ON = once per interval
# ---------------------------------------------------------------------------

def test_daemon_sweep_off_by_default_is_inert():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)  # no housekeeping section = defaults
    try:
        env.plant_family("acct", "family-1", _expired("rt-dead"))
        before = cus.login_family_creds_path("acct", "family-1").read_bytes()
        env.patch(cus, "_oauth_refresh_grant", _no_grant())  # zero probes
        msgs = cus._sweep_housekeeping(cus.load_state(), cus.load_config())
        assert msgs == [], msgs
        assert cus.login_family_creds_path("acct", "family-1").read_bytes() == before
    finally:
        env.restore()


def test_daemon_sweep_on_runs_once_and_respects_interval():
    cfg = {**_ILGATE, "housekeeping": {"daemon_sweep": True, "sweep_interval_hours": 6}}
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=cfg)
    try:
        env.plant_family("acct", "family-1", _expired("rt-dead"))
        grant_calls: list[str] = []
        env.patch(cus, "_oauth_refresh_grant",
                  _grant_map({"rt-dead": ("dead", None)}, calls=grant_calls))
        state, config = cus.load_state(), cus.load_config()

        msgs = cus._sweep_housekeeping(state, config)
        assert any("retired dead free family acct/family-1" in m for m in msgs), msgs
        assert not cus.login_family_creds_path("acct", "family-1").exists()
        assert grant_calls == ["rt-dead"]

        # Second call inside the interval: pure no-op.
        msgs2 = cus._sweep_housekeeping(state, config)
        assert msgs2 == [], msgs2
        assert grant_calls == ["rt-dead"], "interval must prevent a re-probe"
    finally:
        env.restore()


def test_daemon_sweep_no_execute_threads_through_as_report_only():
    cfg = {**_ILGATE, "housekeeping": {"daemon_sweep": True}}
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=cfg)
    try:
        env.plant_family("acct", "family-1", _expired("rt-dead"))
        env.patch(cus, "_oauth_refresh_grant", _grant_map({"rt-dead": ("dead", None)}))
        msgs = cus._sweep_housekeeping(cus.load_state(), cus.load_config(), no_execute=True)
        assert any("report-only" in m for m in msgs), msgs
        assert cus.login_family_creds_path("acct", "family-1").exists(), \
            "--no-execute daemon must not retire stores"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) acquire_slot: blank preferred skipped; healthy preferred still first
# ---------------------------------------------------------------------------

def _clear_reservations() -> None:
    """create_slot reserves each new slot (launch TOCTOU guard); drop the
    reservations so acquire_slot sees the planted slots as free."""
    state = cus.load_state()
    for entry in state.get("slots", {}).values():
        entry.pop("reserved_until", None)
    cus.save_state(state)


def test_acquire_slot_skips_blank_preferred_for_clean_same_account_slot():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.make_slot("acct", live=False, mount_creds=_blank())               # slot-1 blank
        s2 = env.make_slot("acct", live=False, mount_creds=_valid("at-2", "rt-2"))
        _clear_reservations()
        state = cus.load_state()
        name, _d = cus.acquire_slot(state, prefer_account="acct", config=cus.load_config())
        assert name == s2, f"blank-mount preferred slot must be skipped, got {name}"
    finally:
        env.restore()


def test_acquire_slot_healthy_preferred_still_chosen_first_regression():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.make_slot("other", live=False, mount_creds=_valid("at-1", "rt-1"))  # slot-1
        s2 = env.make_slot("acct", live=False, mount_creds=_valid("at-2", "rt-2"))
        _clear_reservations()
        state = cus.load_state()
        name, _d = cus.acquire_slot(state, prefer_account="acct", config=cus.load_config())
        assert name == s2, f"healthy preferred slot must win over an earlier free slot, got {name}"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (9) `cus prune` default invocation: report-only; --no-probe = zero network
# ---------------------------------------------------------------------------

def test_prune_cmd_report_only_no_probe_zero_network():
    env = _Env({"acct": _valid("at-c", "rt-c"), "other": _valid("at-o", "rt-o")},
               active="other", config=_ILGATE)
    try:
        env.plant_family("acct", "family-1", _expired("rt-suspect"))
        before = cus.login_family_creds_path("acct", "family-1").read_bytes()
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        # Click command body via .callback (no CliRunner needed — echo is captured).
        cus.prune_cmd.callback(execute=False, reseed=None, no_probe=True)
        # Suspect-but-unprobed fails open (reported alive), nothing rewritten.
        assert cus.login_family_creds_path("acct", "family-1").read_bytes() == before
        assert any("Free login families" in e for e in env.echoes), env.echoes
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed")
