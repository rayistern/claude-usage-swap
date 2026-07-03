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


def test_saveback_routes_rotated_tokens_to_leased_family():
    """A leased slot's rotated live tokens save back to the FAMILY dir on swap-out,
    never to the account snapshot."""
    env = _Env()
    try:
        env.set_config({"independent_logins": {"use_independent_logins": True}})
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


def _usage(five_h: float, seven_d: float) -> "cus.AccountUsage":
    return cus.AccountUsage(
        five_hour=cus.UsageWindow(utilization=five_h, resets_at=None),
        seven_day=cus.UsageWindow(utilization=seven_d, resets_at=None),
    )


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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
