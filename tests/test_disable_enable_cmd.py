"""Tests for the `cus disable` / `cus enable` commands and the disabled-account
SOS downgrade (2026-07-07).

Feature: a first-class, reversible way to take an account OUT of rotation. The
motivating case is a POISONED account (rayi1) whose canonical store was clobbered
with another account's identity, so polling it returns the WRONG account's usage
and it can't be trusted until a browser relogin. `cus disable rayi1` parks it
cleanly; `cus enable rayi1` restores it.

Pins:
  - disable writes `disabled: true` (+ reason/at) to config.yaml; enable clears it.
  - both are idempotent friendly no-ops; unknown account errors (non-zero exit).
  - a disabled account is dropped from the premium-headroom valid-target count
    (and is NOT named as "lost capacity").
  - a disabled account with a poisoned/expired condition yields ONE soft INFO
    SOS line, not URGENT/WARNING (per-account alarms suppressed while disabled).
  - disable does NOT move an existing lane and does NOT touch a locked slot.
  - `force-poll` still works on a disabled account.

On-disk cases reuse test_login_pool's `_Env` (monkeypatched throwaway tree).

Run standalone:  python3 tests/test_disable_enable_cmd.py
Run under pytest: pytest tests/test_disable_enable_cmd.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402
from click.testing import CliRunner  # noqa: E402
from test_login_pool import _Env  # noqa: E402  (reuse the on-disk fixture)


def _entry(name: str) -> dict:
    """The config.yaml accounts-list entry for `name` (or {} if absent)."""
    cfg = cus.read_yaml(cus.CONFIG_YAML)
    for a in cfg.get("accounts", []):
        if isinstance(a, dict) and a.get("name") == name:
            return a
    return {}


# --------------------------------------------------------------------------
# disable / enable persistence + idempotency + validation
# --------------------------------------------------------------------------

def test_disable_writes_flag_enable_clears_it():
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        # No config.yaml accounts entries yet — disable must self-register.
        r = CliRunner().invoke(cus.cli, ["disable", "beta", "--reason", "poisoned store"])
        assert r.exit_code == 0, r.output
        e = _entry("beta")
        assert e.get("disabled") is True, e
        assert e.get("disabled_reason") == "poisoned store", e
        assert e.get("disabled_at"), e
        # It really lands in the picker's exclusion set.
        assert "beta" in cus._disabled_accounts(cus.load_config())

        r2 = CliRunner().invoke(cus.cli, ["enable", "beta"])
        assert r2.exit_code == 0, r2.output
        e2 = _entry("beta")
        assert "disabled" not in e2 and "disabled_reason" not in e2 and "disabled_at" not in e2, e2
        assert "beta" not in cus._disabled_accounts(cus.load_config())
    finally:
        env.restore()


def test_disable_and_enable_are_idempotent():
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        CliRunner().invoke(cus.cli, ["disable", "beta"])
        r = CliRunner().invoke(cus.cli, ["disable", "beta"])
        assert r.exit_code == 0 and "already disabled" in r.output, r.output
        # Enabling an already-enabled account is also a friendly no-op.
        r2 = CliRunner().invoke(cus.cli, ["enable", "alpha"])
        assert r2.exit_code == 0 and "already enabled" in r2.output, r2.output
    finally:
        env.restore()


def test_unknown_account_errors():
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        r = CliRunner().invoke(cus.cli, ["disable", "nope"])
        assert r.exit_code != 0 and "Unknown account" in r.output, r.output
        r2 = CliRunner().invoke(cus.cli, ["enable", "nope"])
        assert r2.exit_code != 0 and "Unknown account" in r2.output, r2.output
    finally:
        env.restore()


def test_disable_reason_preserved_on_redisable():
    """Re-disabling must not clobber the original reason/timestamp (history)."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        CliRunner().invoke(cus.cli, ["disable", "beta", "--reason", "first"])
        at1 = _entry("beta").get("disabled_at")
        CliRunner().invoke(cus.cli, ["disable", "beta", "--reason", "second"])
        e = _entry("beta")
        assert e.get("disabled_reason") == "first", e
        assert e.get("disabled_at") == at1, e
    finally:
        env.restore()


# --------------------------------------------------------------------------
# effect on rotation / launch / live lanes / locks
# --------------------------------------------------------------------------

def test_disable_does_not_move_existing_lane_or_touch_lock():
    """A lane already on the account keeps its slot; a locked slot is untouched.
    Disable governs FUTURE rotation only — no eviction."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        slot = env.make_slot("beta", live=True)          # beta hosts a live lane
        env.set_config({"session_locks": {"locked_slots": [slot]}})
        r = CliRunner().invoke(cus.cli, ["disable", "beta"])
        assert r.exit_code == 0, r.output
        # Live-lane warning surfaced.
        assert "CURRENTLY live" in r.output, r.output
        # The slot still holds beta — disable did not move/evict it.
        state = cus.load_state()
        assert state["slots"][slot]["account"] == "beta", state["slots"]
        # And the lock is still in place.
        cfg = cus.read_yaml(cus.CONFIG_YAML)
        assert slot in cfg["session_locks"]["locked_slots"], cfg
    finally:
        env.restore()


def test_force_poll_still_works_on_disabled(monkeypatch):
    """force-poll must bypass the disabled flag — the operator checks the account
    after a relogin before re-enabling."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        CliRunner().invoke(cus.cli, ["disable", "beta"])

        # A clean, healthy poll result (real dataclasses so update_state accepts it).
        usage = cus.AccountUsage(
            five_hour=cus.UsageWindow(utilization=12.0, resets_at=None),
            seven_day=cus.UsageWindow(utilization=8.0, resets_at=None),
        )
        monkeypatch.setattr(cus, "poll_account_usage", lambda name: usage)
        r = CliRunner().invoke(cus.cli, ["force-poll", "beta"])
        assert r.exit_code == 0, r.output
        assert "5h: 12.0%" in r.output, r.output
    finally:
        env.restore()


# --------------------------------------------------------------------------
# premium-headroom valid-target count excludes disabled
# --------------------------------------------------------------------------

def _acct(five_h=0.0, seven_d=0.0, next_at=50, **flags):
    a = {"current_5h_pct": five_h, "current_7d_pct": seven_d, "next_swap_at_pct": next_at}
    a.update(flags)
    return a


def test_disabled_account_not_counted_as_premium_target():
    """One live premium lane + two idle spares, but ONE spare is disabled: the
    disabled spare must not count as a valid target (would falsely suppress the
    scarcity SOS) and must not be named as lost capacity."""
    cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session",
        "accounts": [{"name": "alpha", "priority": 1},
                     {"name": "beta", "priority": 1, "disabled": True},
                     {"name": "gamma", "priority": 1}],
    })
    accounts = {
        "alpha": _acct(five_h=95.0, seven_d=20.0),   # hot live premium lane
        "beta": _acct(five_h=0.0, seven_d=0.0),      # clean spare BUT disabled
        "gamma": _acct(five_h=5.0, seven_d=5.0),     # the one real target
    }
    state = {"active": "alpha", "accounts": accounts,
             "slots": {"slot-1": {"account": "alpha", "pool": "premium"}}, "swap_history": []}
    occupied = {"alpha": ["slot-1"]}
    cond = cus._diagnose_premium_headroom(state, cfg, occupied, set())
    # Only gamma counts → exactly 1 valid target, hot lane → WARNING (not None,
    # which is what would happen if beta were wrongly counted as a 2nd target).
    assert cond is not None and cond.severity == "warning", cond
    assert "beta" not in (cond.action or ""), cond.action  # not named as lost


def test_disabling_last_target_fires_urgent_not_suppressed():
    """If disabling the only remaining spare drops valid targets to zero, the
    SYSTEM-level scarcity URGENT must still fire (disable silences the ACCOUNT's
    own alarm, never a real capacity problem it causes)."""
    cfg = cus.deep_merge(cus.DEFAULT_CONFIG, {
        "mode": "per_session",
        "accounts": [{"name": "alpha", "priority": 1},
                     {"name": "beta", "priority": 1, "disabled": True}],
    })
    accounts = {"alpha": _acct(five_h=60.0, seven_d=20.0),   # live premium lane
                "beta": _acct(five_h=0.0, seven_d=0.0)}       # only spare, disabled
    state = {"active": "alpha", "accounts": accounts,
             "slots": {"slot-1": {"account": "alpha", "pool": "premium"}}, "swap_history": []}
    cond = cus._diagnose_premium_headroom(state, cfg, {"alpha": ["slot-1"]}, set())
    assert cond is not None and cond.severity == "urgent", cond
    assert "0 valid swap targets" in cond.summary, cond.summary


# --------------------------------------------------------------------------
# SOS downgrade: disabled account's URGENT/WARNING → single soft INFO line
# --------------------------------------------------------------------------

def test_disabled_account_token_expired_downgraded_to_info():
    """A disabled account that is token_expired must yield ONE info line, not the
    URGENT 'OAuth token expired' klaxon."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        CliRunner().invoke(cus.cli, ["disable", "beta", "--reason", "parked for relogin"])
        state = cus.load_state()
        state["accounts"]["beta"]["token_expired"] = True
        cus.save_state(state)
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        beta_conds = [c for c in conds if c.affected == "beta"]
        assert len(beta_conds) == 1, beta_conds
        c = beta_conds[0]
        assert c.severity == "info", c
        assert "disabled" in c.summary and "parked for relogin" in c.summary, c.summary
        assert "cus enable beta" in c.action, c.action
        # No URGENT/WARNING anywhere names beta.
        assert not any(x.affected == "beta" and x.severity in ("urgent", "warning") for x in conds)
    finally:
        env.restore()


def test_enabled_account_token_expired_still_urgent():
    """Control: with the account ENABLED, the token-expired URGENT is unchanged."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        state = cus.load_state()
        state["accounts"]["beta"]["token_expired"] = True
        cus.save_state(state)
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        beta_urgent = [c for c in conds if c.affected == "beta" and c.severity == "urgent"]
        assert beta_urgent, [(_c.severity, _c.summary) for _c in conds]
    finally:
        env.restore()


def test_healthy_disabled_account_adds_no_sos_noise():
    """A disabled account with NO outstanding condition must not add an info
    line (downgrade only fires when there was a loud condition to silence)."""
    env = _Env(accounts=("alpha", "beta"))
    env.set_config({})  # seed config.yaml (init prerequisite)
    try:
        CliRunner().invoke(cus.cli, ["disable", "beta"])
        conds = cus.diagnose(cus.load_state(), cus.load_config())
        assert not any(c.affected == "beta" for c in conds), \
            [(c.severity, c.summary) for c in conds]
    finally:
        env.restore()


if __name__ == "__main__":
    # Non-monkeypatch tests are runnable standalone; the rest need pytest.
    test_disabled_account_not_counted_as_premium_target()
    test_disabling_last_target_fires_urgent_not_suppressed()
    print("ok")
