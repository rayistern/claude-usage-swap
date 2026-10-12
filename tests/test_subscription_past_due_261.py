"""GH #261 — a PAST-DUE subscription must not read as active (2026-10-11).

THE HOLE: the #191 guard (`test_subscription_disabled`) classifies a profile
as ENDED only on the full free-org signature (has_claude_max/pro false +
organization_type "claude_free"). On 2026-10-11 an account's Max renewal
payment failed: its profile read `subscription_status: "past_due"` with the
plan STILL attached (has_claude_max true, organization_type "claude_max"),
so the guard said "active" — while its usage poll 429'd and every Claude
Code launch on it walled at the first prompt with "Your organization has
disabled Claude subscription access for Claude Code". Two review sessions
were launched onto it that night. `cus status` showed no SUB flag at all.

The contract pinned here:
  (1) the pure classifier returns "past_due" for a body with the plan still
      attached and `subscription_status: "past_due"`, and "active" for the
      SAME body with "active" (the issue's fixture pin); the ENDED signature
      still wins, and unknown spellings fail open;
  (2) the full 429 poll path marks the AccountUsage `subscription_past_due`
      (not `subscription_disabled`) and still attaches the plan tier;
  (3) update_state flags `subscription_past_due` (+ stamp, + detail,
      alongside rate_limited) and audits the off→on transition ONCE;
  (4) recovery: a successful poll, or an "active" probe while still 429ing,
      clears it; the two subscription flags are exclusive in both directions;
  (5) placement: never a pick_swap_target target (every strategy, even under
      allow_rate_limited_targets), never a pick_launch_account raw fallback;
  (6) NO FORCED MOVES: a past-due account is not a shared-mount migration
      reason, not an evacuation source, not a Trigger-0 eviction — but it is
      excluded as an evacuation TARGET;
  (7) `cus sos` warns and names the fix (payment method under claude.ai
      Settings, Billing); guard off ⇒ no line and no exclusion;
  (8) explicit `cus launch <acct>`, `cus slot move`, `cus switch` refuse
      with the same fix;
  (9) `cus status` shows PAST_DUE (not RATE_LIMITED), the statusline shows
      PAST-DUE, `_session_binding` says blocked, the panes header says
      PAST DUE and excludes the account as a destination;
 (10) deep prune never retires a store whose own-token probe says past_due.

Fixtures carry no real identifiers: no email, no account name beyond
"alpha/beta/gamma", nothing of the billing record beyond the status string.

Run standalone:  python3 tests/test_subscription_past_due_261.py
Run under pytest: pytest tests/test_subscription_past_due_261.py
"""

from __future__ import annotations

import copy
import io
import json
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cus  # noqa: E402
from test_subscription_disabled import (  # noqa: E402
    _Env, _FakeProfileResp, _PROFILE_ACTIVE, _PROFILE_DEAD, _audit_lines,
    _base_state, _cfg, _healthy_acct, _sub_dead_acct,
)


# ---------------------------------------------------------------------------
# The fixture: the live shape of 2026-10-11, identifiers removed.
# Plan still attached, org still a Max org — only the status string differs
# from a healthy account. THIS is what the #191 signature cannot see.
# ---------------------------------------------------------------------------

_PROFILE_PAST_DUE = {
    "account": {"uuid": "u-past-due", "has_claude_max": True, "has_claude_pro": False},
    "organization": {"uuid": "o-past-due", "organization_type": "claude_max",
                     "billing_type": "stripe_subscription",
                     "rate_limit_tier": "default_claude_max_5x",
                     "seat_tier": None, "subscription_status": "past_due"},
}


def _same_body_with_status(status: str) -> dict:
    p = copy.deepcopy(_PROFILE_PAST_DUE)
    p["organization"]["subscription_status"] = status
    return p


def _past_due_acct(h5=0.0, h7=0.0) -> dict:
    """A state-dict account exactly as Branch 1.6 leaves it."""
    return {"current_5h_pct": h5, "current_7d_pct": h7, "next_swap_at_pct": 80,
            "last_swap_ts": None, "subscription_past_due": True,
            "rate_limited": True,
            "subscription_detail": {"subscription_status": "past_due",
                                    "organization_type": "claude_max"}}


def _past_due_flag_only(h5=0.0, h7=0.0) -> dict:
    """The flag WITHOUT rate_limited — isolates the past_due lever itself
    (a rate_limited account is already soft-filtered when alternatives exist,
    so a realistic fixture would pass on main for the wrong reason)."""
    a = _past_due_acct(h5, h7)
    a.pop("rate_limited")
    return a


def _past_due_usage() -> "cus.AccountUsage":
    """AccountUsage as poll_account_usage returns it when the 429's probe
    came back 'past_due'."""
    u = cus.AccountUsage.empty()
    u.subscription_past_due = True
    u.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
             "subscription_probe": "past_due",
             "subscription": {"subscription_status": "past_due",
                              "organization_type": "claude_max"}}
    return u


def _disabled_usage() -> "cus.AccountUsage":
    u = cus.AccountUsage.empty()
    u.subscription_disabled = True
    u.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
             "subscription_probe": "disabled",
             "subscription": {"subscription_status": "canceled",
                              "organization_type": "claude_free"}}
    return u


# ---------------------------------------------------------------------------
# (1) the classifier — the issue's fixture pin
# ---------------------------------------------------------------------------

def test_classifier_past_due_with_plan_attached_is_blocked():
    """THE PIN: same body, only the status string differs."""
    verdict, detail = cus._classify_subscription_profile(_PROFILE_PAST_DUE)
    assert verdict == "past_due", (
        f"a past-due profile with the plan still attached classified as {verdict!r} — "
        "this is the 2026-10-11 hole: launches on it wall, cus must block placement")
    assert detail["subscription_status"] == "past_due"
    assert detail["rate_limit_tier"] == "default_claude_max_5x"   # #237 tier still read
    verdict_ok, _ = cus._classify_subscription_profile(_same_body_with_status("active"))
    assert verdict_ok == "active", verdict_ok


def test_classifier_past_due_is_not_ended():
    """The back-compat ENDED view stays False: the plan is attached and can
    recover, so nothing that MOVES lanes (which keys on ENDED) may fire."""
    disabled, _ = cus._profile_says_subscription_disabled(_PROFILE_PAST_DUE)
    assert disabled is False
    assert cus._profile_says_subscription_disabled(_PROFILE_DEAD)[0] is True
    assert cus._profile_says_subscription_disabled(_PROFILE_ACTIVE)[0] is False


def test_classifier_precedence_and_fail_open():
    # ENDED signature outranks a past_due status on the same body.
    dead_and_past_due = copy.deepcopy(_PROFILE_DEAD)
    dead_and_past_due["organization"]["subscription_status"] = "past_due"
    assert cus._classify_subscription_profile(dead_and_past_due)[0] == "disabled"
    # A team/enterprise seat stays out of scope for both signatures.
    seat = copy.deepcopy(_PROFILE_PAST_DUE)
    seat["organization"]["seat_tier"] = "enterprise"
    assert cus._classify_subscription_profile(seat)[0] == "active"
    # The status string alone is the signal — plan flags are not consulted.
    no_plan = copy.deepcopy(_PROFILE_PAST_DUE)
    no_plan["account"] = {"has_claude_max": False, "has_claude_pro": False}
    no_plan["organization"]["organization_type"] = "something_new"
    assert cus._classify_subscription_profile(no_plan)[0] == "past_due"
    # Any other spelling, or no status at all, fails OPEN to active.
    for status in ("PAST_DUE", "pastdue", "past-due", "unpaid", "", None):
        assert cus._classify_subscription_profile(_same_body_with_status(status))[0] == "active", status
    for junk in ({}, {"account": "x", "organization": None}, {"organization": {"subscription_status": 7}}):
        assert cus._classify_subscription_profile(junk)[0] == "active", junk


# ---------------------------------------------------------------------------
# (2) the full 429 poll path
# ---------------------------------------------------------------------------

def test_poll_429_with_past_due_profile_sets_past_due_not_disabled(monkeypatch):
    env = _Env(_base_state(),
               accounts_creds={"a": {"claudeAiOauth": {
                   "accessToken": "at-a", "refreshToken": "rt-a",
                   "expiresAt": 2_000_000_000_000}}})
    try:
        def _fake_urlopen(req, timeout=None):
            url = req.full_url
            if url == cus.USAGE_API_URL:
                raise urllib.error.HTTPError(
                    url, 429, "Too Many Requests", {},
                    io.BytesIO(b'{"error":{"type":"rate_limit_error","message":"Rate limited."}}'))
            if url == cus.PROFILE_API_URL:
                return _FakeProfileResp(_PROFILE_PAST_DUE)
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_urlopen)
        u = cus.poll_account_usage("a")
        assert getattr(u, "subscription_past_due", False) is True, (
            "a 429 whose profile reads past_due with the plan attached was classified as "
            f"active (probe verdict {u.raw.get('subscription_probe')!r}) — the 2026-10-11 hole")
        assert u.subscription_disabled is False, "past due is not ENDED"
        assert u.raw.get("rate_limited") is True
        assert u.raw.get("subscription_probe") == "past_due"
        assert u.raw["subscription"]["subscription_status"] == "past_due"
        # #237: the probe's detail still feeds the plan tier on this verdict.
        assert (u.plan_tier_profile or {}).get("raw") == "default_claude_max_5x", u.plan_tier_profile
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) + (4) state: flag, audit once, recovery, exclusivity
# ---------------------------------------------------------------------------

def test_update_state_flags_past_due_and_audits_once():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        cus.update_state_with_usage(state, {"a": _past_due_usage()})
        acct = state["accounts"]["a"]
        assert acct["subscription_past_due"] is True
        assert "subscription_disabled" not in acct
        assert acct["rate_limited"] is True, "the 429 symptom must stay visible"
        assert acct.get("subscription_past_due_at"), "transition must be stamped"
        assert acct["subscription_detail"] == {"subscription_status": "past_due",
                                               "organization_type": "claude_max"}
        assert acct["current_5h_pct"] == 10.0, "prior percentages preserved"
        first = _audit_lines(env, "subscription-past-due")
        assert len(first) == 1 and "placement-blocked" in first[0] and "account=a" in first[0], first
        assert "Billing" in first[0], "the audit line names the fix"
        cus.update_state_with_usage(state, {"a": _past_due_usage()})
        assert len(_audit_lines(env, "subscription-past-due")) == 1, env.echoes
    finally:
        env.restore()


def test_past_due_clears_on_successful_poll():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        state["accounts"]["a"].update(_past_due_acct())
        ok = cus.AccountUsage(five_hour=cus.UsageWindow(12.0, None),
                              seven_day=cus.UsageWindow(3.0, None))
        cus.update_state_with_usage(state, {"a": ok})
        acct = state["accounts"]["a"]
        assert "subscription_past_due" not in acct and "subscription_past_due_at" not in acct
        assert "subscription_detail" not in acct and "rate_limited" not in acct
        cleared = [e for e in _audit_lines(env, "subscription-past-due") if "cleared" in e]
        assert len(cleared) == 1, env.echoes
    finally:
        env.restore()


def test_past_due_clears_on_probe_active_while_still_429():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        state["accounts"]["a"].update(_past_due_acct())
        u = cus.AccountUsage.empty()
        u.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
                 "subscription_probe": "active",
                 "subscription": {"subscription_status": "active"}}
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]
        assert "subscription_past_due" not in acct
        assert acct["rate_limited"] is True, "still throttled — rate_limited stays"
        assert [e for e in _audit_lines(env, "subscription-past-due") if "cleared" in e]
    finally:
        env.restore()


def test_past_due_and_ended_are_exclusive_both_directions():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        # past due, then the plan lapses to a free org: ENDED replaces it.
        cus.update_state_with_usage(state, {"a": _past_due_usage()})
        cus.update_state_with_usage(state, {"a": _disabled_usage()})
        acct = state["accounts"]["a"]
        assert acct.get("subscription_disabled") is True
        assert "subscription_past_due" not in acct and "subscription_past_due_at" not in acct
        assert acct["subscription_detail"]["subscription_status"] == "canceled"
        # ... and back: the probe now reads past_due (plan attached again).
        cus.update_state_with_usage(state, {"a": _past_due_usage()})
        acct = state["accounts"]["a"]
        assert acct.get("subscription_past_due") is True
        assert "subscription_disabled" not in acct and "subscription_disabled_at" not in acct
        assert acct["subscription_detail"]["subscription_status"] == "past_due"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (5) placement: never a swap target, never a launch fallback pick
# ---------------------------------------------------------------------------

def test_pick_swap_target_excludes_past_due_even_when_best():
    accts = {"alpha": _healthy_acct(95, 40), "beta": _past_due_flag_only(),
             "gamma": _healthy_acct(30, 20)}
    for strategy in ("smart", "headroom", "lowest_usage", "drain",
                     "round_robin", "strict_priority"):
        t = cus.pick_swap_target({"active": "alpha", "accounts": accts},
                                 _cfg(strategy=strategy))
        assert t is not None and t.name == "gamma", (
            f"{strategy}: rotated onto past-due account {t and t.name!r}")


def test_pick_swap_target_past_due_only_candidate_holds():
    accts = {"alpha": _healthy_acct(95, 40), "beta": _past_due_acct()}
    cfg = _cfg()
    cfg["smart_strategy"] = {"allow_rate_limited_targets": True}
    t = cus.pick_swap_target({"active": "alpha", "accounts": accts}, cfg)
    assert t is None, f"allow_rate_limited_targets re-admitted a past-due account: {t}"


def test_launch_raw_fallbacks_skip_past_due(monkeypatch):
    """The exact tier that placed the two 2026-10-11 sessions: the raw
    fallbacks bypass pick_swap_target and never filtered rate_limited."""
    monkeypatch.setattr(cus, "mount_in_use", lambda d: False)
    monkeypatch.setattr(cus, "_live_slot_accounts", lambda state: set())
    accts = {"beta": _past_due_flag_only(h5=99, h7=95), "gamma": _healthy_acct(99, 96)}
    t = cus.pick_launch_account({"active": None, "accounts": accts, "slots": {}}, _cfg())
    assert t is not None and t.name == "gamma", (
        f"launch placed onto past-due account {t and t.name!r} — the 2026-10-11 hole")


# ---------------------------------------------------------------------------
# (6) no forced moves — and not a landing spot for someone else's rescue
# ---------------------------------------------------------------------------

def test_lanes_on_a_past_due_account_are_left_alone():
    state = {"active": "beta",
             "accounts": {"alpha": _healthy_acct(10, 10), "beta": _past_due_acct()},
             "slots": {"slot-1": {"account": "beta"}}}
    cfg = _cfg()
    # Not a shared-mount migration reason (the default-ON mover).
    assert cus._shared_mount_account_unhealthy("beta", state, cfg) is None
    # Not an evacuation source (no "dead" account ⇒ no candidates at all).
    assert cus._evacuation_candidates(state, cfg) == []
    # Not a Trigger-0 eviction: that trigger reads the state-less operator set.
    assert "beta" not in cus._disabled_accounts(cfg)
    # But it IS excluded from every placement set.
    assert "beta" in cus._disabled_accounts(cfg, state)
    assert "beta" in cus._subscription_blocked_accounts(state, cfg)
    assert "beta" not in cus._subscription_dead_accounts(state, cfg)


def test_past_due_account_is_not_an_evacuation_target(monkeypatch):
    """beta is ENDED with a live lane; gamma is past due and emptier than
    alpha — the rescue must land on alpha."""
    env = _Env(_base_state())
    try:
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(60, 50), "beta": _sub_dead_acct(),
                              "gamma": _past_due_acct(h5=1, h7=1)},
                 "slots": {"slot-1": {"account": "beta"}}}
        cus.slot_path("slot-1").mkdir()
        monkeypatch.setattr(cus, "mount_in_use", lambda d: True)
        monkeypatch.setattr(cus, "_slot_move_plan",
                            lambda state, config, slot, cand: {"plan": "snapshot"})
        cands = cus._evacuation_candidates(state, _cfg())
        assert cands and cands[0]["account"] == "beta"
        assert cands[0]["target"] == "alpha", cands
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (7) SOS names the fix; guard off restores pre-guard behavior
# ---------------------------------------------------------------------------

def _past_due_conds(conds):
    return [c for c in conds if "PAST DUE" in c.summary]


class _NoLiveDaemonPid:
    """`diagnose` (and so `cus status` / the statusline) reads DAEMON_PID, a
    module constant computed at import from the REAL accounts dir — _Env does
    not repoint it. Point it into the sandbox so these tests never read the
    live machine's daemon.pid (and never see its state in their output)."""

    def __init__(self, env: _Env):
        self._saved = cus.DAEMON_PID
        cus.DAEMON_PID = env.accounts_dir / "daemon.pid"

    def restore(self):
        cus.DAEMON_PID = self._saved


def test_sos_past_due_condition_names_the_billing_fix():
    env = _Env(_base_state())
    try:
        pid = _NoLiveDaemonPid(env)
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(10, 10), "beta": _past_due_acct()},
                 "slots": {}}
        conds = _past_due_conds(cus.diagnose(state, {**_cfg(), "mode": "per_session"}))
        assert len(conds) == 1, conds
        c = conds[0]
        assert c.affected == "beta" and c.severity == "warning"
        assert "Settings, Billing" in c.action and "force-poll beta" in c.action, c.action
        assert "left in place" in c.action, "the line must say existing lanes are not moved"
        # No ENDED line for a past-due account.
        assert not [x for x in cus.diagnose(state, {**_cfg(), "mode": "per_session"})
                    if "subscription ENDED" in x.summary]
    finally:
        pid.restore()
        env.restore()


def test_guard_off_ignores_a_past_due_flag_everywhere():
    off = {"enabled": False}
    beta = {"current_5h_pct": 0.0, "current_7d_pct": 0.0, "next_swap_at_pct": 80,
            "last_swap_ts": None, "subscription_past_due": True}
    accts = {"alpha": _healthy_acct(95, 40), "beta": beta, "gamma": _healthy_acct(30, 20)}
    state = {"active": "alpha", "accounts": accts, "slots": {}}
    assert cus.pick_swap_target(state, _cfg()).name == "gamma"
    assert cus.pick_swap_target(state, _cfg(guard=off)).name == "beta"
    env = _Env(_base_state())
    pid = _NoLiveDaemonPid(env)
    try:
        assert _past_due_conds(cus.diagnose(state, {**_cfg(guard=off), "mode": "per_session"})) == []
    finally:
        pid.restore()
        env.restore()


# ---------------------------------------------------------------------------
# (8) explicit-placement refusals
# ---------------------------------------------------------------------------

def _refusal_env() -> _Env:
    return _Env({"active": "other",
                 "accounts": {"acct": _past_due_acct(), "other": _healthy_acct(1, 1)},
                 "slots": {}, "swap_history": []},
                config={"mode": "per_session"})


def test_launch_prepare_refuses_explicit_past_due():
    env = _refusal_env()
    try:
        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("acct", cus.load_state(), cus.load_config())
        msg = str(ei.value)
        assert "PAST DUE" in msg and "Settings, Billing" in msg and "force-poll acct" in msg, msg
    finally:
        env.restore()


def test_slot_move_refuses_past_due_target():
    env = _refusal_env()
    try:
        result = CliRunner().invoke(cus.cli, ["slot", "move", "slot-1", "acct"])
        assert result.exit_code != 0
        joined = "\n".join(env.echoes)
        assert "PAST DUE" in joined and "Settings, Billing" in joined, joined
    finally:
        env.restore()


def test_switch_refuses_past_due_target():
    env = _refusal_env()
    try:
        result = CliRunner().invoke(cus.cli, ["switch", "acct"])
        assert result.exit_code != 0
        joined = "\n".join(env.echoes)
        assert "PAST DUE" in joined and "Settings, Billing" in joined, joined
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (9) what the operator sees
# ---------------------------------------------------------------------------

def test_session_binding_blocked_label():
    sev, txt = cus._session_binding(_past_due_acct(), "premium", {})
    assert sev == "blocked" and "PAST DUE" in txt and "Billing" in txt, (sev, txt)


def test_status_shows_past_due_not_rate_limited():
    env = _Env({"active": "other",
                "accounts": {"acct": _past_due_acct(), "other": _healthy_acct(1, 1)},
                "slots": {}, "swap_history": []},
               config={"mode": "per_session"})
    pid = _NoLiveDaemonPid(env)
    try:
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, (r.output, env.echoes)
        joined = "\n".join(env.echoes) + r.output
        row = next(line for line in joined.splitlines() if line.startswith("acct"))
        assert "PAST_DUE" in row and "RATE_LIMITED" not in row and "SUB_ENDED" not in row, row
    finally:
        pid.restore()
        env.restore()


def test_statusline_shows_past_due_flag():
    """Verbose: the per-account flag reads (PAST-DUE), not (429). Every mode:
    the SOS one-liner names the past-due account (the statusline is where
    the 2026-10-11 account hid as a plain 429 — see the 2026-08-07 note)."""
    env = _Env({"active": "other",
                "accounts": {"acct": _past_due_acct(), "other": _healthy_acct(1, 1)},
                "slots": {}, "swap_history": []},
               config={"mode": "per_session"})
    pid = _NoLiveDaemonPid(env)
    try:
        r = CliRunner().invoke(cus.cli, ["statusline", "--verbose"],
                               env={"CLAUDE_CONFIG_DIR": None, "NO_COLOR": "1"})
        assert r.exit_code == 0, (r.output, env.echoes)
        joined = "\n".join(env.echoes) + r.output
        assert "(PAST-DUE)" in joined and "(429)" not in joined and "SUB-END" not in joined, joined
        env.echoes.clear()
        r = CliRunner().invoke(cus.cli, ["statusline", "--compact"],
                               env={"CLAUDE_CONFIG_DIR": None, "NO_COLOR": "1"})
        joined = "\n".join(env.echoes) + r.output
        assert "acct: Claude subscription PAST DUE" in joined, joined
    finally:
        pid.restore()
        env.restore()


def test_panes_header_says_past_due_and_excludes_as_destination():
    now = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    h = {"known": False, "reason": "no reading", "past_due": True, "disabled": True}
    hdr = cus._account_header("beta", h, now)
    assert "PAST DUE" in hdr and "[DISABLED]" not in hdr, hdr
    assert "existing panes stay" in hdr


# ---------------------------------------------------------------------------
# (10) deep prune: a past_due verdict on a store's own token never retires it
# ---------------------------------------------------------------------------

def test_deep_prune_keeps_a_past_due_store():
    from test_family_prune_identity_mismatch import (
        _Env as _PruneEnv, _ILGATE, _fresh_caches, _no_grant, _plant_family_full, _valid)
    env = _PruneEnv({"aa": _valid("at-a", "rt-a"), "bb": _valid("at-b", "rt-b")},
                    active="bb", config=_ILGATE)
    try:
        _fresh_caches()
        path = _plant_family_full(env, "aa", "family-1", _valid("at-x", "rt-x"), identity_of="aa")
        env.patch(cus, "_oauth_refresh_grant", _no_grant())
        env.patch(cus, "_probe_subscription_profile",
                  lambda token: ("past_due", {"subscription_status": "past_due"}))
        rows = cus._deep_prune_stores(cus.load_state(), cus.load_config(), execute=True, probe=True)
        sub = [r for r in rows if r["check"] == "subscription-past-due"]
        assert len(sub) == 1 and sub[0]["retired"] is None and not sub[0]["would_retire"], rows
        assert "Billing" in sub[0]["detail"]
        assert not [r for r in rows if r["check"] == "subscription-disabled"], rows
        assert path.exists(), "a past-due store is live credential material — never retired"
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and "monkeypatch" not in v.__code__.co_varnames]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed (monkeypatch-dependent tests run under pytest)")
