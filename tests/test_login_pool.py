"""Tests for the per-account independent-login POOL (2026-07-03).

Plan: docs/plans/2026-07-03-independent-login-pool.md.

Phase 1 covers the store + lease primitives:
  - list_login_families / next_family_id (usable-only, numeric order)
  - leased_families: only LIVE slots' leases count (idle reclaimable)
  - free_login_family / has_free_login_family: lowest-free, exhaustion

Later phases (provisioning command, swap claim/install, decision + reactive
rescue, SOS) append here.

Run standalone:  python3 tests/test_login_pool.py
Run under pytest: pytest tests/test_login_pool.py
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
    """Throwaway on-disk tree (same monkeypatch pattern as the other suites)."""

    def __init__(self, accounts=("alpha", "beta")) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.accounts_dir = root / "claude-accounts"
        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-bare")))
        for name in accounts:
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{name}@x"}}))
        cus.write_json(self.accounts_dir / "state.json", {
            "active": accounts[0],
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0, "current_7d_pct": 0.0} for n in accounts},
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

        # #127: never let a test hit the real OAuth endpoint. Default verdict
        # "unknown" = fail-open, which is byte-identical to pre-#127 claim
        # behavior, so pre-existing claim tests are unaffected. Tests that
        # exercise the probe itself layer a _Probe on top of this.
        self._saved_probe = cus._oauth_refresh_grant
        cus._oauth_refresh_grant = lambda rt: ("unknown", None)

    def set_config(self, cfg: dict) -> None:
        cus.write_yaml(cus.CONFIG_YAML, cfg)

    def plant_family(self, account: str, family_id: str, refresh: str, usable: bool = True) -> None:
        """Write a pooled family's creds (usable=carries a refresh token)."""
        d = cus.login_family_dir(account, family_id)
        d.mkdir(parents=True, exist_ok=True)
        blob = _creds(refresh) if usable else {"claudeAiOauth": {"accessToken": "only-access"}}
        cus.login_family_creds_path(account, family_id).write_text(json.dumps(blob))

    def make_slot(self, account: str, live: bool, family_id: str | None = None) -> str:
        """Create a slot holding `account` (with live creds in its mount dir),
        optionally leasing a pooled family."""
        state = cus.load_state()
        name, d = cus.create_slot(state)
        # Live mount creds = the account's snapshot family by default (a plain
        # copy), matching a slot that swapped in via the copy path.
        (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{account}")))
        (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"{account}@x"}}))
        state["slots"][name]["account"] = account
        if family_id:
            state["slots"][name]["login_family"] = f"{account}/{family_id}"
        cus.save_state(state)
        if live:
            self.live_slots.add(name)
        cus._OCCUPIED_SLOTS_CACHE.clear()
        return name

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus._OCCUPIED_SLOTS_CACHE.clear()
        self._tmp.cleanup()


def test_list_login_families_usable_only_and_ordered():
    env = _Env()
    try:
        assert cus.list_login_families("alpha") == []  # empty pool
        env.plant_family("alpha", "family-2", "rt-a2")
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-10", "rt-a10")
        env.plant_family("alpha", "family-3", "bad", usable=False)  # no refresh token
        # Numeric order (not lexical: family-10 after family-2), unusable excluded.
        assert cus.list_login_families("alpha") == ["family-1", "family-2", "family-10"]
    finally:
        env.restore()


def test_next_family_id_increments_past_highest():
    env = _Env()
    try:
        assert cus.next_family_id("alpha") == "family-1"  # empty pool starts at 1
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        assert cus.next_family_id("alpha") == "family-3"
        # A half-finished (unusable) dir still bumps the counter — don't reuse it.
        env.plant_family("alpha", "family-3", "bad", usable=False)
        assert cus.next_family_id("alpha") == "family-4"
    finally:
        env.restore()


def test_free_family_skips_live_leases_reclaims_idle():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-1"  # lowest free

        # A LIVE slot leasing family-1 removes it from the free set.
        env.make_slot("alpha", live=True, family_id="family-1")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-2"
        assert cus.has_free_login_family("alpha", state) is True

        # An IDLE slot leasing family-2 does NOT consume it (reclaimable).
        env.make_slot("alpha", live=False, family_id="family-2")
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) == "family-2"
    finally:
        env.restore()


def test_pool_exhaustion_reports_no_free_family():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.make_slot("alpha", live=True, family_id="family-1")  # only family, live-leased
        state = cus.load_state()
        assert cus.free_login_family("alpha", state) is None
        assert cus.has_free_login_family("alpha", state) is False
        # An account with no pool at all is also "no free family".
        assert cus.has_free_login_family("beta", state) is False
    finally:
        env.restore()


# --------------------------------------------------------------------------
# End-to-end: swap claim / lease / save-back (P3) + decision rescue (P4)
# --------------------------------------------------------------------------

def test_execute_swap_claims_free_family_and_records_lease():
    """A slot swapping onto an account HELD by another live mount claims a free
    pooled family — installs THAT family's creds (not a clobbering snapshot copy)
    and records the lease. This is the credential-safety core of the feature."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)          # will swap onto beta
        env.make_slot("beta", live=True)                    # beta already held → held
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        # Live creds are the pooled family's DISTINCT token, not beta's snapshot.
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta-fam1", f"expected pooled family creds, got {live_rt}"
        # Lease recorded so other slots won't claim the same family.
        state = cus.load_state()
        assert state["slots"][mover]["login_family"] == "beta/family-1"
        # beta's account snapshot is untouched (still its own family).
        snap_rt = cus._credential_refresh_token(cus.read_json(env.accounts_dir / "account-beta" / ".credentials.json"))
        assert snap_rt == "rt-beta"
    finally:
        env.restore()


def test_execute_swap_refuses_when_pool_exhausted():
    """Held target + gate on + NO free family ⇒ execute_swap RAISES rather than
    install a clobbering copy. The daemon logs this as a failed move; SOS surfaces
    the exhaustion. Never silently clobber."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)  # beta held, and NO family planted
        raised = False
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised = "pool exhausted" in str(e)
        assert raised, "must refuse to clobber when the pool is exhausted"
        # The mover stayed on alpha (no partial move).
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
    finally:
        env.restore()


def test_second_same_cycle_move_onto_same_target_claims_family():
    """Regression, 2026-07-03 16:10Z slot-2/slot-7 → rayi3 clobber: two moves in
    ONE daemon cycle landed on the same target. The first was a legal snapshot
    copy (target unheld at that instant), but the second move's clobber guard
    read occupied_slot_accounts through its 5s TTL cache — still showing the
    target unheld — so it installed a SECOND snapshot copy of the same token
    family instead of claiming the free pooled family. The next refresh logged
    the first mount out (slot-2's live creds were found with an empty
    refreshToken). The guard must read ground-truth occupancy.

    No cache clear between the two execute_swap calls — that's exactly the
    daemon's same-cycle fan-out sequence (the other suites clear it in setup,
    which is why this never showed up in tests)."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)  # primes the cache
        cus.execute_swap("beta", trigger="auto-ladder", slot=s2)  # must see s1's move
        rt1 = cus._credential_refresh_token(cus.read_json(cus.slot_path(s1) / ".credentials.json"))
        rt2 = cus._credential_refresh_token(cus.read_json(cus.slot_path(s2) / ".credentials.json"))
        assert rt1 != rt2, f"both live mounts ended on the same token family ({rt1}) — #104 clobber"
        assert rt2 == "rt-beta-fam1", rt2
        assert cus.load_state()["slots"][s2]["login_family"] == "beta/family-1"
    finally:
        env.restore()


def test_second_same_cycle_move_refuses_when_no_family():
    """Same-cycle double-landing with NO free family must RAISE (the daemon logs
    a failed move and the slot holds) — never silently install the clobbering
    second copy. Pre-fix the stale cache made this path clobber instead."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)
        s2 = env.make_slot("alpha", live=True)
        cus.execute_swap("beta", trigger="auto-ladder", slot=s1)  # primes the cache
        raised = False
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=s2)
        except RuntimeError as e:
            raised = "pool exhausted" in str(e)
        assert raised, "second same-cycle landing without a family must refuse, not clobber"
        assert cus.load_state()["slots"][s2]["account"] == "alpha"
    finally:
        env.restore()


class _Probe:
    """Swap out the network refresh-grant with a scripted per-token verdict.
    verdicts: {refresh_token: ("alive", resp) | ("dead", None) | ("unknown", None)}.
    Records calls so tests can assert the probe ran (or didn't)."""

    def __init__(self, verdicts):
        self.verdicts, self.calls = verdicts, []
        self._orig = cus._oauth_refresh_grant
        cus._oauth_refresh_grant = self

    def __call__(self, rt):
        self.calls.append(rt)
        return self.verdicts.get(rt, ("unknown", None))

    def restore(self):
        cus._oauth_refresh_grant = self._orig


def test_claim_skips_dead_family_and_claims_next_alive():
    """#127 regression (2026-07-03 second logout): rayi1/family-1's store held a
    dead branch (poisoned by the pre-PR#126 save-back bug); the claim installed
    it and the live session logged out. A claim must PROBE each free family:
    dead → retire store + try next; alive → persist the rotated tokens and
    install THOSE."""
    env = _Env()
    probe = _Probe({"rt-beta-fam1": ("dead", None),
                    "rt-beta-fam2": ("alive", {"access_token": "at-rotated",
                                               "refresh_token": "rt-beta-fam2-rotated",
                                               "expires_in": 28800})})
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)  # beta held → claim path
        env.plant_family("beta", "family-1", "rt-beta-fam1")   # dead branch
        env.plant_family("beta", "family-2", "rt-beta-fam2")   # alive
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        # family-1 retired: creds renamed away, no longer listed as usable.
        assert not cus.login_family_creds_path("beta", "family-1").exists()
        assert cus.list_login_families("beta") == ["family-2"]
        # family-2 claimed, with the ROTATED token both in store and on mount.
        assert cus.load_state()["slots"][mover]["login_family"] == "beta/family-2"
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta-fam2-rotated", live_rt
        store_rt = cus._credential_refresh_token(cus.read_json(cus.login_family_creds_path("beta", "family-2")))
        assert store_rt == "rt-beta-fam2-rotated", store_rt
    finally:
        probe.restore()
        env.restore()


def test_claim_fail_open_when_probe_unverifiable():
    """Network/endpoint trouble must NOT block a claim — verdict 'unknown'
    claims the family unverified, byte-identical to pre-#127 behavior."""
    env = _Env()
    probe = _Probe({})  # everything → unknown
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        assert probe.calls == ["rt-beta-fam1"]
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta-fam1"  # unrotated store install, as before
        assert cus.load_state()["slots"][mover]["login_family"] == "beta/family-1"
    finally:
        probe.restore()
        env.restore()


def test_claim_all_dead_refuses_like_pool_exhausted():
    """Every free family dead ⇒ the claim comes back empty and execute_swap
    refuses (failed move; slot holds) — a dead pool must never fall through to
    the clobbering snapshot copy."""
    env = _Env()
    probe = _Probe({"rt-beta-fam1": ("dead", None), "rt-beta-fam2": ("dead", None)})
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        env.plant_family("beta", "family-2", "rt-beta-fam2")
        raised = False
        try:
            cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        except RuntimeError as e:
            raised = "pool exhausted" in str(e)
        assert raised, "all-dead pool must refuse, not clobber"
        assert cus.load_state()["slots"][mover]["account"] == "alpha"
        assert cus.list_login_families("beta") == []  # both retired
    finally:
        probe.restore()
        env.restore()


def test_claim_verify_gate_off_skips_probe():
    """verify_family_on_claim: false ⇒ plain free_login_family, no probe call —
    the pre-#127 escape hatch."""
    env = _Env()
    probe = _Probe({})
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True,
                                               "verify_family_on_claim": False}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        assert probe.calls == [], "gate off must not probe"
        assert cus.load_state()["slots"][mover]["login_family"] == "beta/family-1"
    finally:
        probe.restore()
        env.restore()


def test_execute_swap_onto_free_account_uses_snapshot_no_lease():
    """When the target is NOT held by another mount, the swap uses the plain
    snapshot (today's path) and records no lease — the pool is only for the
    double-book case."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")  # exists but beta not held
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta", "un-held target should install the snapshot, not a pooled family"
        assert "login_family" not in cus.load_state()["slots"][mover]
    finally:
        env.restore()


def test_double_booked_safe_when_a_mount_holds_a_pool_lease():
    """A correct pool rescue puts two live mounts on one account with DISTINCT
    families (one leases a pooled family, one holds the primary). That must NOT
    be flagged as a #104 double-book — only 2+ UNCOVERED mounts are a clobber."""
    env = _Env(accounts=("alpha",))
    try:
        a = env.make_slot("alpha", live=True)                        # holds primary (rt-alpha)
        b = env.make_slot("alpha", live=True, family_id="family-1")   # leases alpha/family-1
        (cus.slot_path(b) / ".credentials.json").write_text(json.dumps(_creds("rt-alpha-fam1")))
        env.plant_family("alpha", "family-1", "rt-alpha-fam1")
        state = cus.load_state()
        assert cus.double_booked_live_accounts(state) == [], "leased mount is covered — not a clobber"
        # Drop the lease → both mounts uncovered → genuine double-book, flagged.
        state["slots"][b].pop("login_family")
        cus.save_state(state)
        flagged = cus.double_booked_live_accounts(cus.load_state())
        assert flagged and flagged[0]["account"] == "alpha", flagged
    finally:
        env.restore()


def test_periodic_saveback_routes_leased_slot_to_family():
    """Regression, 2026-07-03 ~16:21Z: the PERIODIC save-back (and slot gc)
    share saveback_mount_credentials, which had no lease branch — minutes
    after slot-2 claimed rayi3/family-1, the periodic pass stomped
    account-rayi3's snapshot with the family's rotated tokens (log symptom:
    'save-back slot-2: saved → account-rayi3'). A leased mount's tokens must
    land in the family store; the account snapshot stays untouched."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        slot = env.make_slot("beta", live=True, family_id="family-1")
        # Simulate the live session rotating its (family) token since install:
        # newer expiresAt so the only_if_fresher periodic gate sees fresh work.
        (cus.slot_path(slot) / ".credentials.json").write_text(
            json.dumps(_creds("rt-beta-fam1-rotated", expires_at=3_000_000_000_000)))
        r = cus.saveback_mount_credentials(cus.slot_path(slot), "beta", cus.load_state(),
                                           only_if_fresher=True)
        assert r["action"] == "saved" and r.get("family") == "family-1", r
        fam_rt = cus._credential_refresh_token(cus.read_json(cus.login_family_creds_path("beta", "family-1")))
        assert fam_rt == "rt-beta-fam1-rotated", fam_rt
        snap_rt = cus._credential_refresh_token(cus.read_json(env.accounts_dir / "account-beta" / ".credentials.json"))
        assert snap_rt == "rt-beta", f"account snapshot was stomped with family tokens: {snap_rt}"
    finally:
        env.restore()


def test_saveback_routes_rotated_tokens_to_leased_family():
    """A leased slot's rotated live tokens save back to the FAMILY dir on swap-out,
    never to the account snapshot."""
    env = _Env()
    try:
        # mode=per_session: this exercises the slot save-back path, where the
        # shared ~/.claude mount is observe-only, so a slot may swap back onto the
        # _Env default active account ("alpha") without a shared-mount double-book.
        # In global/hybrid mode that same move is now (correctly) a #141 clobber
        # and would require a distinct family — see the shared-mount tests below.
        env.set_config({"mode": "per_session", "independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cus.execute_swap("beta", trigger="auto-ladder", slot=mover)  # claims beta/family-1
        # Session rotates the family token in the live mount, then swaps away.
        d = cus.slot_path(mover)
        (d / ".credentials.json").write_text(json.dumps(_creds("rt-beta-fam1-rotated", expires_at=3_000_000_000_000)))
        cus.execute_swap("alpha", trigger="auto-ladder", slot=mover)
        # Rotated token saved to the family dir; beta snapshot untouched.
        fam_rt = cus._credential_refresh_token(cus.read_json(cus.login_family_creds_path("beta", "family-1")))
        assert fam_rt == "rt-beta-fam1-rotated"
        snap_rt = cus._credential_refresh_token(cus.read_json(env.accounts_dir / "account-beta" / ".credentials.json"))
        assert snap_rt == "rt-beta"
    finally:
        env.restore()


def test_decide_slot_swaps_pool_rescue_relaxes_exclusion():
    """P4: with the gate on, decide_slot_swaps un-drops a held account that has a
    free pooled family, so a hot lane whose only headroom is behind held accounts
    gets a rescue move (gate off ⇒ holds)."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        mover = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 90.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"current_5h_pct": 15.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(90.0, 20.0), "beta": _usage(15.0, 10.0)}
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        excl = {"beta", "gamma"}  # all headroom held elsewhere
        base = cus.deep_merge(cus.DEFAULT_CONFIG, {
            "mode": "per_session", "strategy": "lowest_usage",
            "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
            "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        })
        # Gate OFF ⇒ no target, holds.
        off = cus.deep_merge(base, {"independent_logins": {"use_independent_logins": False}})
        assert cus.decide_slot_swaps(state, off, usage, exclude_accounts=excl) == []
        # Gate ON ⇒ rescues onto beta (the pooled-family account).
        on = cus.deep_merge(base, {"independent_logins": {"use_independent_logins": True}})
        moves = cus.decide_slot_swaps(state, on, usage, exclude_accounts=excl)
        assert len(moves) == 1 and moves[0]["slot"] == mover and moves[0]["to"] == "beta", moves
    finally:
        env.restore()


def test_pool_double_book_beats_degraded_repick():
    """Regression, 2026-07-03 slot-2 → rayi3@99% incident: the pool rescue
    un-drops a held account for the PICK, but `taken` is seeded with the raw
    exclude set — so the rescued pick died at the taken-veto and the fan-out
    re-pick ran on degraded leftovers. pick_swap_target's headroom_fallback
    then returned a 99%-5h account ("swap somewhere beats sitting on a hot
    active"), which beat the 0%-usage pooled pick and rate-limited the live
    session within a minute of the move.

    Cast: alpha = the hot lane (rayi2@94%), beta = held elsewhere but 0% with
    a free pooled family (rayi1), gamma = unheld but nearly saturated (rayi3).
    The move must double-book beta via the pool, not land on gamma."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        mover = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 94.0, "current_7d_pct": 40.0})
        state["accounts"]["beta"].update({"current_5h_pct": 0.0, "current_7d_pct": 31.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 99.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(94.0, 40.0), "beta": _usage(0.0, 31.0), "gamma": _usage(99.0, 10.0)}
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
            "mode": "per_session", "strategy": "smart",
            "independent_logins": {"use_independent_logins": True},
            "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
            "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        })
        moves = cus.decide_slot_swaps(state, cfg, usage, exclude_accounts={"beta"})
        assert len(moves) == 1 and moves[0]["slot"] == mover, moves
        assert moves[0]["to"] == "beta", f"picked degraded leftover over pooled pick: {moves}"
        # The logged reason must describe the EXECUTED pick (beta, 5h=0%) and
        # flag the double-book — pre-fix it showed the vetoed pick's stats
        # next to the re-picked target (decisions.jsonl said 5h=0% for a 99% move).
        assert "pool double-book" in moves[0]["reason"], moves[0]["reason"]
        assert "5h=0%" in moves[0]["reason"], moves[0]["reason"]
    finally:
        env.restore()


def test_healthy_repick_still_preferred_over_pool_double_book():
    """Fan-out semantics survive the incident fix: when the re-pick finds a
    HEALTHY distinct account (won't re-trip its own ladder on arrival), it
    still wins over double-booking the vetoed pick — load spreads across
    accounts and pool families aren't consumed needlessly. And the move's
    reason now carries the RE-PICK's stats, not the vetoed pick's."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        mover = env.make_slot("alpha", live=True)
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 94.0, "current_7d_pct": 40.0})
        state["accounts"]["beta"].update({"current_5h_pct": 0.0, "current_7d_pct": 31.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 10.0, "current_7d_pct": 12.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(94.0, 40.0), "beta": _usage(0.0, 31.0), "gamma": _usage(10.0, 12.0)}
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
            "mode": "per_session", "strategy": "smart",
            "independent_logins": {"use_independent_logins": True},
            "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
            "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        })
        moves = cus.decide_slot_swaps(state, cfg, usage, exclude_accounts={"beta"})
        assert len(moves) == 1 and moves[0]["slot"] == mover, moves
        assert moves[0]["to"] == "gamma", moves
        assert "5h=10%" in moves[0]["reason"], moves[0]["reason"]
    finally:
        env.restore()


def _usage(five_h: float, seven_d: float) -> "cus.AccountUsage":
    return cus.AccountUsage(
        five_hour=cus.UsageWindow(utilization=five_h, resets_at=None),
        seven_day=cus.UsageWindow(utilization=seven_d, resets_at=None),
    )


# --------------------------------------------------------------------------
# Reactive-429 over-subscribe guard (2026-07-05 rayi1 pile-up, GH #104)
#
# Incident: the daemon piled 6 lanes onto one low-usage account with only 2
# independent-login families, so several live mounts shared one family's
# single-use OAuth refresh token; the next rotation diverged/clobbered the
# losers. Root cause: the reactive-429 path committed a move whenever the picker
# returned a target, with NO accounting of the target's DISTINCT login families —
# it relied on the execution layer to refuse pool exhaustion. These pin the
# decision-layer invariant: a reactive-429 move must never put more concurrent
# live mounts on an account than it has distinct families to cover them; when it
# can't, the lane HOLDS.
# --------------------------------------------------------------------------

def _reactive_cfg(gate: bool) -> dict:
    """per_session config for the reactive picker with the timing/growth gates
    off (so the decision is driven by the picker + family accounting, not
    wall-clock state), independent-logins pool `gate` on/off."""
    return cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session", "strategy": "lowest_usage",
        "swap_hysteresis": {"enabled": False}, "usage_growth_gate": {"enabled": False},
        "defer_swap_near_5h_reset": {"enabled": False}, "lazy_swap": {"enabled": False},
        "independent_logins": {"use_independent_logins": gate},
    })


def _reactive(env, state, cfg, sid_to_slot: dict, exclude: set):
    """Drive check_rate_limit_reactive_per_session with injected 429 entries,
    resolving each session id to a slot via a stubbed session_current_slot
    (there are no real panes/processes in-test). `exclude` mirrors the live-slot
    accounts _per_session_cycle now passes (GH #104)."""
    orig = cus.session_current_slot
    cus.session_current_slot = lambda sid: sid_to_slot.get(sid)
    try:
        entries = [{"session_id": sid} for sid in sid_to_slot]
        return cus.check_rate_limit_reactive_per_session(
            state, cfg, entries=entries, exclude_accounts=exclude)
    finally:
        cus.session_current_slot = orig


def test_reactive_429_holds_when_target_family_exhausted():
    """The incident in miniature: alpha's lane hits a 429, and the only account
    with headroom (beta) is ALREADY live on another mount and has NO free login
    family. The reactive path must HOLD — never install a clobbering second copy
    of beta's token family — even though beta looks like a great target."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)   # the 429'd lane
        env.make_slot("beta", live=True)          # beta held elsewhere, NO family
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        # exclude = every live-slot account (what _per_session_cycle supplies).
        moves = _reactive(env, state, _reactive_cfg(True), {"sA": s1}, {"beta"})
        assert moves == [], f"must HOLD, not double-book family-exhausted beta: {moves}"
    finally:
        env.restore()


def test_reactive_429_rescues_onto_pooled_family():
    """The graceful other side: give beta a FREE pooled family and the same 429
    escape is allowed — it claims a DISTINCT family (no clobber). Proves the guard
    only blocks the exhausted case, it doesn't over-block a legal pool rescue."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")   # one distinct family
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        moves = _reactive(env, state, _reactive_cfg(True), {"sA": s1}, {"beta"})
        assert len(moves) == 1 and moves[0]["slot"] == s1 and moves[0]["to"] == "beta", moves
        assert moves[0]["gate"] == "reactive_429", moves
    finally:
        env.restore()


def test_reactive_429_same_cycle_second_lane_holds_on_single_family():
    """Same-cycle capacity accounting: two lanes 429 in ONE cycle and the only
    headroom (beta, held elsewhere) has exactly ONE free family. The first lane
    rescues onto that family; the SECOND must HOLD, not commit a second move onto
    beta's now-consumed family. Pre-fix both saw the single free family in the
    static snapshot and both committed → the #104 clobber."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        s1 = env.make_slot("alpha", live=True)   # 429'd lane #1
        env.make_slot("beta", live=True)          # beta held, ONE family
        s3 = env.make_slot("gamma", live=True)    # 429'd lane #2
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 96.0, "current_7d_pct": 30.0})
        cus.save_state(state)
        state = cus.load_state()
        # All three accounts are live → the cycle excludes all of them.
        moves = _reactive(env, state, _reactive_cfg(True),
                          {"sA": s1, "sC": s3}, {"alpha", "beta", "gamma"})
        assert len(moves) == 1, f"single family must back exactly one rescue: {moves}"
        assert moves[0]["to"] == "beta" and moves[0]["slot"] == s1, moves
    finally:
        env.restore()


def test_reactive_429_gate_off_never_double_books():
    """Backward-compat: with the pool gate OFF a held account has capacity 0, so
    the reactive escape never lands on an already-live account (the pre-pool #104
    rule, unchanged) — it holds if that's the only option, and takes a FREE
    account when one exists."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        env.set_config({"independent_logins": {"use_independent_logins": False}})
        s1 = env.make_slot("alpha", live=True)   # 429'd lane
        env.make_slot("beta", live=True)          # beta held (live)
        # gamma exists but is NOT live → a legal, distinct escape target.
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 95.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 20.0, "current_7d_pct": 15.0})
        cus.save_state(state)
        state = cus.load_state()
        moves = _reactive(env, state, _reactive_cfg(False), {"sA": s1}, {"beta"})
        assert len(moves) == 1 and moves[0]["to"] == "gamma", \
            f"gate-off escape must take free gamma, never double-book live beta: {moves}"

        # Remove gamma's headroom (saturate it) so beta is the ONLY option: hold.
        state["accounts"]["gamma"].update({"current_5h_pct": 100.0, "current_7d_pct": 100.0})
        cus.save_state(state)
        state = cus.load_state()
        held = _reactive(env, state, _reactive_cfg(False), {"sA": s1}, {"beta"})
        assert held == [], f"gate-off must HOLD rather than double-book live beta: {held}"
    finally:
        env.restore()


def test_proactive_fanout_holds_second_lane_on_single_family():
    """Proactive-ladder twin of the same-cycle capacity guard: two hot lanes on
    one account fan out, but the only headroom (beta, held elsewhere) has exactly
    ONE free family. One lane pool-double-books beta; the SECOND must HOLD, not
    consume the same single family twice in one cycle (the #104 clobber). Pins the
    round_claims capacity accounting in decide_slot_swaps."""
    env = _Env(accounts=("alpha", "beta", "gamma"))
    try:
        s1 = env.make_slot("alpha", live=True)   # two hot lanes on alpha
        s2 = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)          # beta held, ONE family
        env.make_slot("gamma", live=True)         # gamma held + saturated → no escape
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        state = cus.load_state()
        state["accounts"]["alpha"].update({"current_5h_pct": 90.0, "current_7d_pct": 30.0})
        state["accounts"]["beta"].update({"current_5h_pct": 5.0, "current_7d_pct": 10.0})
        state["accounts"]["gamma"].update({"current_5h_pct": 100.0, "current_7d_pct": 100.0})
        cus.save_state(state)
        state = cus.load_state()
        usage = {"alpha": _usage(90.0, 30.0), "beta": _usage(5.0, 10.0), "gamma": _usage(100.0, 100.0)}
        cfg = _reactive_cfg(True)  # gate on; timing gates off
        # exclude = every live-slot account, as _per_session_cycle now supplies.
        moves = cus.decide_slot_swaps(state, cfg, usage,
                                      exclude_accounts={"alpha", "beta", "gamma"})
        assert len(moves) == 1, f"single family must back exactly one fan-out move: {moves}"
        assert moves[0]["to"] == "beta" and moves[0]["slot"] in (s1, s2), moves
    finally:
        env.restore()


# --------------------------------------------------------------------------
# SOS Condition 2b (P5): pool-aware lane target-starvation
# --------------------------------------------------------------------------

def _lane_starvation_summaries(conds) -> list[str]:
    return [c.summary for c in conds if "with no swap target" in c.summary]


def test_sos_starvation_suppressed_when_pool_can_rescue():
    """A hot lane whose only headroom is a held account with a FREE pooled family
    is NOT reported starved (it can rescue). Without the family it IS reported,
    with a pool-provisioning hint."""
    env = _Env(accounts=("alpha", "beta"))
    try:
        env.make_slot("alpha", live=True)   # hot lane
        env.make_slot("beta", live=True)    # beta held, has headroom
        state = cus.load_state()
        state["accounts"]["alpha"].update({"next_swap_at_pct": 80, "current_5h_pct": 100.0, "current_7d_pct": 20.0})
        state["accounts"]["beta"].update({"next_swap_at_pct": 80, "current_5h_pct": 10.0, "current_7d_pct": 10.0})
        cus.save_state(state)
        state = cus.load_state()
        cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
            "mode": "per_session", "strategy": "lowest_usage",
            "independent_logins": {"use_independent_logins": True}})

        # No pooled family for beta ⇒ alpha lane is genuinely starved, WITH hint.
        starved = _lane_starvation_summaries(cus.diagnose(state, cfg))
        assert any("alpha" in s for s in starved), starved
        hinted = [c for c in cus.diagnose(state, cfg)
                  if "alpha" in c.summary and "login-mount" in c.action]
        assert hinted, "gate-on starvation should hint cus login-mount"

        # Provision a free family for beta ⇒ alpha can rescue ⇒ NOT starved.
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        starved2 = _lane_starvation_summaries(cus.diagnose(state, cfg))
        assert not any("alpha" in s for s in starved2), starved2
    finally:
        env.restore()


# --------------------------------------------------------------------------
# Provisioning command (P2): `cus login-mount <account>` account-keyed, repeatable
# --------------------------------------------------------------------------

def test_login_mount_account_provisions_next_family():
    env = _Env()
    try:
        r = CliRunner().invoke(cus.cli, ["login-mount", "alpha"])
        assert r.exit_code == 0, r.output
        assert "alpha/family-1" in r.output and "CLAUDE_CONFIG_DIR" in r.output
        assert cus.login_family_dir("alpha", "family-1").exists()  # scaffolded
        # A second provision (before finishing #1) targets family-2.
        r2 = CliRunner().invoke(cus.cli, ["login-mount", "alpha"])
        assert "alpha/family-2" in r2.output, r2.output
    finally:
        env.restore()


def test_login_mount_finish_records_family_provenance():
    env = _Env()
    try:
        CliRunner().invoke(cus.cli, ["login-mount", "alpha"])  # scaffolds family-1
        # Simulate the completed /login: creds + identity land in the family dir.
        env.plant_family("alpha", "family-1", "rt-a-fam1")
        (cus.login_family_dir("alpha", "family-1") / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "alpha@x"}}))
        r = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--finish"])
        assert r.exit_code == 0, r.output
        assert "alpha/family-1" in r.output and "pool now 1" in r.output
        prov = cus.read_json(cus.login_family_provenance_path("alpha", "family-1"))
        assert prov["account"] == "alpha" and prov["family_id"] == "family-1"
        assert prov["refresh_fp"] == cus._refresh_fingerprint("rt-a-fam1")
    finally:
        env.restore()


def test_login_mount_finish_accepts_same_account_different_userid():
    """Regression (2026-07-03): a fresh independent /login of the SAME account has
    a different top-level userID than the account snapshot. Identity verification
    must key on oauthAccount (accountUuid + email), NOT userID — else --finish
    false-rejects a correct login (the rayi3 onboarding error)."""
    env = _Env()
    try:
        # Give the account snapshot a full oauthAccount identity + a userID.
        (env.accounts_dir / "account-alpha" / ".claude.json").write_text(json.dumps(
            {"userID": "uid-SNAPSHOT",
             "oauthAccount": {"emailAddress": "alpha@x", "accountUuid": "uuid-alpha"}}))
        CliRunner().invoke(cus.cli, ["login-mount", "alpha"])  # scaffold family-1
        env.plant_family("alpha", "family-1", "rt-a-fam1")
        # The family login: SAME account (uuid+email) but a DIFFERENT userID.
        (cus.login_family_dir("alpha", "family-1") / ".claude.json").write_text(json.dumps(
            {"userID": "uid-FRESH-LOGIN",
             "oauthAccount": {"emailAddress": "alpha@x", "accountUuid": "uuid-alpha"}}))
        r = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--finish"])
        assert r.exit_code == 0, r.output
        assert "mismatch" not in r.output.lower(), r.output
        assert cus.read_json(cus.login_family_provenance_path("alpha", "family-1"))["account"] == "alpha"
    finally:
        env.restore()


def test_login_mount_finish_refuses_wrong_account():
    env = _Env()
    try:
        CliRunner().invoke(cus.cli, ["login-mount", "alpha"])
        env.plant_family("alpha", "family-1", "rt-wrong")
        (cus.login_family_dir("alpha", "family-1") / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "someone-else@x"}}))
        r = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--finish"])
        assert r.exit_code != 0 and "mismatch" in r.output.lower(), r.output
        # --force overrides.
        r2 = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--finish", "--force"])
        assert r2.exit_code == 0, r2.output
    finally:
        env.restore()


def test_login_mount_from_existing_seeds_family_1_only():
    env = _Env()
    try:
        r = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--from-existing"])
        assert r.exit_code == 0 and "family-1" in r.output, r.output
        assert cus.list_login_families("alpha") == ["family-1"]
        # A second --from-existing is refused (only seeds the first).
        r2 = CliRunner().invoke(cus.cli, ["login-mount", "alpha", "--from-existing"])
        assert r2.exit_code != 0, r2.output
    finally:
        env.restore()


def test_status_shows_login_pool_section():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, r.output
        assert "Login pools" in r.output
        assert "alpha" in r.output and "2 family(ies), 2 free" in r.output
        assert "gate OFF" in r.output  # gate off by default → nudge shown
    finally:
        env.restore()


def test_login_mount_list_shows_pool_depth():
    env = _Env()
    try:
        env.plant_family("alpha", "family-1", "rt-a1")
        env.plant_family("alpha", "family-2", "rt-a2")
        r = CliRunner().invoke(cus.cli, ["login-mount", "--list"])
        assert r.exit_code == 0
        assert "alpha: 2 family" in r.output, r.output
        assert "pool_size" in r.output  # 2 < default 3 → nudge shown
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# `cus slot move <slot> <account>` — manual in-place lane move (2026-07-05).
#
# The command is a thin operator wrapper around execute_swap(..., slot=...), so
# these tests target the wrapper's own logic (validation, normalization, the
# lock guard, the dry-run preview, and the pre-flight double-book guard) plus a
# few end-to-end paths through the real execute_swap to confirm the inherited
# GH #104 clobber-safety is actually surfaced.
# ---------------------------------------------------------------------------

def _slot_account(name: str) -> str | None:
    return (cus.load_state().get("slots", {}).get(name, {}) or {}).get("account")


def test_slot_move_to_unheld_account_succeeds():
    """(a) Move onto an account NOT live anywhere → plain snapshot install."""
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)   # slot on alpha
        # beta is not held by any live mount → a clean snapshot copy.
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta"])
        assert r.exit_code == 0, r.output
        assert f"moved {mover}: alpha -> beta" in r.output
        assert f"undo: cus slot move {mover} alpha" in r.output
        assert _slot_account(mover) == "beta"
        # Snapshot copy → live creds are beta's own snapshot token (no lease).
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta", live_rt
        assert "login_family" not in cus.load_state()["slots"][mover]
    finally:
        env.restore()


def test_slot_move_onto_held_account_with_free_family_claims_it():
    """(b) Move onto an account a live mount already holds, WITH a free login
    family → execute_swap claims the family (distinct token, no clobber)."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)                 # beta already held
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta"])
        assert r.exit_code == 0, r.output
        assert "claimed login family: beta/family-1" in r.output
        assert _slot_account(mover) == "beta"
        assert cus.load_state()["slots"][mover]["login_family"] == "beta/family-1"
        # Distinct token family installed — NOT a clobbering snapshot copy.
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta-fam1", live_rt
    finally:
        env.restore()


def test_slot_move_onto_held_account_without_family_refuses_no_clobber():
    """(c) Move onto a held account with NO free family → refused; no move, no
    clobber. --force bypasses the pre-flight guard but execute_swap's own pool
    safety STILL refuses (it has no force path) rather than clobber."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)                 # beta held, NO family planted
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta"])
        assert r.exit_code != 0, r.output
        assert "refusing" in r.output and "GH #104" in r.output
        assert _slot_account(mover) == "alpha", "mover must not have moved"
        # --force skips the pre-flight, but execute_swap raises "pool exhausted"
        # (no force path inside it) — still no clobber.
        r2 = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta", "--force"])
        assert r2.exit_code != 0, r2.output
        assert "pool exhausted" in r2.output
        assert _slot_account(mover) == "alpha", "mover must not have moved even under --force"
    finally:
        env.restore()


def test_slot_move_refuses_locked_slot_unless_force():
    """(d) A locked slot is refused; --force overrides the lock (and, target
    being unheld, the move then succeeds)."""
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)
        env.set_config({"session_locks": {"locked_slots": [mover]}})
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta"])
        assert r.exit_code != 0, r.output
        assert "locked" in r.output
        assert _slot_account(mover) == "alpha"
        # --force overrides the lock; beta is unheld so it moves cleanly.
        r2 = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta", "--force"])
        assert r2.exit_code == 0, r2.output
        assert _slot_account(mover) == "beta"
    finally:
        env.restore()


def test_slot_move_accepts_bare_slot_number():
    """(e) Bare `1` normalizes to `slot-1` — same move as the full name."""
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)        # "slot-1"
        assert mover == "slot-1"
        r = CliRunner().invoke(cus.cli, ["slot", "move", "1", "beta"])
        assert r.exit_code == 0, r.output
        assert _slot_account("slot-1") == "beta"
    finally:
        env.restore()


def test_slot_move_dry_run_makes_no_state_change():
    """(f) --dry-run prints the plan and touches nothing."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("alpha", live=True)
        env.make_slot("beta", live=True)
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        before = json.dumps(cus.load_state(), sort_keys=True)
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "beta", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "(dry-run)" in r.output and "CLAIM" in r.output
        after = json.dumps(cus.load_state(), sort_keys=True)
        assert before == after, "dry-run must not change state"
        # Live creds untouched (still alpha's snapshot copy).
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-alpha", live_rt
    finally:
        env.restore()


def test_slot_move_already_on_target_is_noop():
    env = _Env()
    try:
        mover = env.make_slot("alpha", live=True)
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "alpha"])
        assert r.exit_code == 0, r.output
        assert "already on 'alpha'" in r.output
    finally:
        env.restore()


def test_slot_move_validates_unknown_slot_and_account():
    env = _Env()
    try:
        env.make_slot("alpha", live=True)
        r = CliRunner().invoke(cus.cli, ["slot", "move", "slot-1", "nosuch"])
        assert r.exit_code != 0 and "Unknown account 'nosuch'" in r.output, r.output
        r2 = CliRunner().invoke(cus.cli, ["slot", "move", "99", "beta"])
        assert r2.exit_code != 0 and "Unknown slot 'slot-99'" in r2.output, r2.output
    finally:
        env.restore()


def test_slot_move_plan_verdicts():
    """Pure preview helper: the four verdicts, driven directly (no execute_swap)."""
    env = _Env()
    try:
        cfg_on = {"independent_logins": {"use_independent_logins": True}}
        mover = env.make_slot("alpha", live=True)
        state = cus.load_state()
        # noop: target == current.
        assert cus._slot_move_plan(state, cfg_on, mover, "alpha")["plan"] == "noop"
        # snapshot: beta unheld.
        assert cus._slot_move_plan(state, cfg_on, mover, "beta")["plan"] == "snapshot"
        # Make beta held by a second live slot.
        env.make_slot("beta", live=True)
        state = cus.load_state()
        cus._OCCUPIED_SLOTS_CACHE.clear()
        # refuse: held + gate on + no family.
        assert cus._slot_move_plan(state, cfg_on, mover, "beta")["plan"] == "refuse"
        # claim: held + gate on + a free family.
        env.plant_family("beta", "family-1", "rt-beta-fam1")
        assert cus._slot_move_plan(state, cfg_on, mover, "beta")["plan"] == "claim"
        # Gate off: held with a family present is still "refuse" (no pool to use).
        assert cus._slot_move_plan(state, {}, mover, "beta")["plan"] == "refuse"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# `cus slot move` — shared-mount (global/hybrid) double-book clobber (issue #141,
# 2026-07-05). The shared ~/.claude mount holds state["active"] as a live account
# in global/hybrid mode, but bare sessions set no CLAUDE_CONFIG_DIR so
# mount_in_use(CLAUDE_DIR) reads False. The double-book guard must count the
# shared-mount account UNCONDITIONALLY in global/hybrid mode (not via mount_in_use),
# or a slot-move plain-snapshots the shared account onto a second live mount and
# clobbers its token family. In _Env, mount_pids only ever sees slot dirs, so the
# shared mount always reads not-in-use — i.e. the exact bare-undetectable scenario.
# ---------------------------------------------------------------------------

def test_shared_mount_holds_is_mode_aware():
    """_shared_mount_holds: unconditional for global/hybrid state["active"]
    (bare sessions undetectable), detectable-only for per_session."""
    env = _Env(accounts=("merkos", "beta"))          # active = merkos
    try:
        assert cus.mount_in_use(cus.CLAUDE_DIR) is False, "shared mount reads not-in-use in _Env"
        assert cus._shared_mount_holds("merkos", cus.load_state(), {"mode": "hybrid"}) is True
        assert cus._shared_mount_holds("merkos", cus.load_state(), {"mode": "global"}) is True
        # per_session: observe-only mount → not held unless mount_in_use detects it.
        assert cus._shared_mount_holds("merkos", cus.load_state(), {"mode": "per_session"}) is False
        # A non-active account is never the shared-mount holder.
        assert cus._shared_mount_holds("beta", cus.load_state(), {"mode": "hybrid"}) is False
    finally:
        env.restore()


def test_slot_move_dry_run_flags_shared_mount_account_claim():
    """The reported bug (#141): `cus slot move <slot> merkos --dry-run` where
    merkos is the active shared-mount account WITH a free family must print
    CLAIM, not SNAPSHOT."""
    env = _Env(accounts=("merkos", "beta"))
    try:
        env.set_config({"mode": "hybrid", "independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("beta", live=True)
        env.plant_family("merkos", "family-1", "rt-merkos-fam1")
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "merkos", "--dry-run"])
        assert r.exit_code == 0, r.output
        assert "CLAIM" in r.output and "SNAPSHOT" not in r.output, r.output
        assert "shared mount" in r.output, r.output
    finally:
        env.restore()


def test_slot_move_onto_shared_mount_account_with_family_claims_no_clobber():
    """(a) Move a slot onto the shared-mount account (hybrid, active=merkos,
    ~/.claude in use) WITH a free family → claims the family, installs a distinct
    token (NOT a clobbering snapshot copy of merkos's base credentials)."""
    env = _Env(accounts=("merkos", "beta"))
    try:
        env.set_config({"mode": "hybrid", "independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("beta", live=True)
        env.plant_family("merkos", "family-1", "rt-merkos-fam1")
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "merkos"])
        assert r.exit_code == 0, r.output
        assert "claimed login family: merkos/family-1" in r.output, r.output
        assert _slot_account(mover) == "merkos"
        # Distinct family token installed — NOT merkos's base snapshot (rt-merkos).
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-merkos-fam1", live_rt
        # merkos's base snapshot (what the shared mount rides) is untouched.
        snap_rt = cus._credential_refresh_token(cus.read_json(env.accounts_dir / "account-merkos" / ".credentials.json"))
        assert snap_rt == "rt-merkos", snap_rt
    finally:
        env.restore()


def test_slot_move_onto_shared_mount_account_without_family_refuses_no_clobber():
    """(b) Same as (a) but NO free family → refused (no clobber of the shared
    mount). --force skips the pre-flight but execute_swap still refuses the
    pool exhaustion — never a plain snapshot copy of the shared account."""
    env = _Env(accounts=("merkos", "beta"))
    try:
        env.set_config({"mode": "hybrid", "independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("beta", live=True)
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "merkos"])
        assert r.exit_code != 0, r.output
        assert "refusing" in r.output and "GH #104" in r.output, r.output
        assert _slot_account(mover) == "beta", "must not have moved"
        r2 = CliRunner().invoke(cus.cli, ["slot", "move", mover, "merkos", "--force"])
        assert r2.exit_code != 0 and "pool exhausted" in r2.output, r2.output
        assert _slot_account(mover) == "beta", "must not clobber even under --force"
        # The slot's live creds were never overwritten with merkos's base snapshot.
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-beta", live_rt
    finally:
        env.restore()


def test_slot_move_non_shared_unheld_account_still_snapshots_in_hybrid():
    """(c) The fix must not over-fire: in hybrid mode, moving onto a DIFFERENT
    account that is neither the shared-mount account nor held by a slot still
    resolves to a plain snapshot install (unchanged)."""
    env = _Env(accounts=("merkos", "beta", "gamma"))   # active = merkos (shared)
    try:
        env.set_config({"mode": "hybrid", "independent_logins": {"use_independent_logins": True}})
        mover = env.make_slot("beta", live=True)
        plan = cus._slot_move_plan(cus.load_state(), cus.load_config(), mover, "gamma")
        assert plan["plan"] == "snapshot", plan
        r = CliRunner().invoke(cus.cli, ["slot", "move", mover, "gamma"])
        assert r.exit_code == 0, r.output
        assert _slot_account(mover) == "gamma"
        live_rt = cus._credential_refresh_token(cus.read_json(cus.slot_path(mover) / ".credentials.json"))
        assert live_rt == "rt-gamma", live_rt
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
