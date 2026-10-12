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
  (1) the profile probe returns "past_due" for a body with the plan still
      attached and `subscription_status: "past_due"`, and "active" for the
      SAME body with "active" (the issue's fixture pin — driven through
      `_probe_subscription_profile`, which exists on `main`, so the pin
      fails there on BEHAVIOUR, not on a missing name); the ENDED signature
      still wins, and unknown spellings fail open;
  (2) the full 429 poll path marks the AccountUsage `subscription_past_due`
      (not `subscription_disabled`) and still attaches the plan tier;
  (3) update_state flags `subscription_past_due` (+ stamp, + detail,
      alongside rate_limited) and audits the off→on transition ONCE;
  (4) recovery: a successful poll, or an "active" probe while still 429ing,
      clears it; the two subscription flags are exclusive in both directions;
  (5) placement: never a pick_swap_target target (every strategy, even under
      allow_rate_limited_targets); never a pick_launch_account pick at ANY
      of its tiers — the shimmed picker, the raw fallback, the lane-share
      fallback and the least-bad degraded tier, each reached by its own
      fixture;
  (6) NO FORCED MOVES, with a LIVE lane on the account: not a Trigger-0
      eviction, not an evacuation source, not a shared-mount migration
      reason — each with an ENDED/operator-disabled control that DOES move;
      but a past-due account is excluded as an evacuation TARGET and from
      the warm-spare floor;
  (7) `cus sos` warns and names the fix (payment method under claude.ai
      Settings, Billing); its target-availability math counts a past-due
      account as unavailable even under allow_rate_limited_targets; guard
      off ⇒ no line and no exclusion;
  (8) explicit `cus launch <acct>`, `cus slot move`, `cus switch` and
      `cus auto-swap <acct>` refuse with the same fix;
  (9) `cus force-poll` — the command the SOS and refusal texts tell the
      operator to run — records a 429: it SETS the past-due flag (and the
      ENDED flag, which it also never saved before), after which status,
      SOS and the refusals follow; and it CLEARS the flag on a 429 whose
      profile reads active (the recovery case); the 429 is counted once;
 (10) `cus status` shows PAST_DUE (not RATE_LIMITED), the statusline shows
      PAST-DUE, `_session_binding` says blocked, the panes payload marks the
      account `disabled` + `past_due` so the watchdog never picks it as a
      destination, and the header says PAST DUE;
 (11) deep prune never retires a store whose own-token probe says past_due.

SANDBOX: every test runs with every HOME-derived path constant of `cus`
repointed into a throwaway directory (the `_Sandbox` below), and an audit
hook records any open/stat/listdir of the real home's `claude-accounts`,
`.claude` or `.claude.json`; the fixture fails the test if the list is not
empty. That is how "no test here touches the live account directory" is
proved, not asserted.

Fixtures carry no real identifiers: no email, no account name beyond
"alpha/beta/gamma/acct/other", nothing of the billing record beyond the
status string.

Run standalone:  python3 tests/test_subscription_past_due_261.py
Run under pytest: pytest tests/test_subscription_past_due_261.py
"""

from __future__ import annotations

import builtins
import copy
import io
import json
import os
import sys
import tempfile
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

# The home `cus` was imported with. Captured before any repoint so the audit
# guards the directory the module constants actually resolved to.
_IMPORT_HOME = cus.HOME


# ---------------------------------------------------------------------------
# Sandbox + audit hook
# ---------------------------------------------------------------------------

class _Sandbox:
    """Repoint EVERY HOME-derived `Path` constant of `cus` (HOME, CLAUDE_JSON,
    CLAUDE_DIR, CREDS_JSON, ACCOUNTS_DIR, STATE_JSON, …, DAEMON_PID, INBOX_MD,
    SOS_MD, LAST_NOTIFY — found by introspection, so a constant added later
    is covered too) into a throwaway directory, set $HOME there for the few
    `Path.home()` call sites, and record every filesystem access to the real
    home's claude directories. `violations` must be empty at teardown.

    Recording rather than raising: a raise inside cus could be swallowed by a
    broad `except Exception` (e.g. the launch picker's `_try`) and the test
    would pass for the wrong reason."""

    GUARDED = ("claude-accounts", ".claude", ".claude.json")

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._saved_attrs: dict[str, Path] = {}
        for k, v in list(vars(cus).items()):
            if not (isinstance(v, Path) and k.isupper()):
                continue
            if v == _IMPORT_HOME:
                self._saved_attrs[k] = v
                setattr(cus, k, self.root)
            elif _IMPORT_HOME in v.parents:
                self._saved_attrs[k] = v
                setattr(cus, k, self.root / v.relative_to(_IMPORT_HOME))
        assert {"HOME", "ACCOUNTS_DIR", "STATE_JSON", "CLAUDE_DIR", "DAEMON_PID",
                "INBOX_MD", "SOS_MD", "LAST_NOTIFY", "SESSIONS_LOG", "STOPS_LOG"} <= set(self._saved_attrs), \
            sorted(self._saved_attrs)
        self._env_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.root)
        self._guarded = [str(_IMPORT_HOME / g) for g in self.GUARDED]
        self.violations: list[str] = []
        self._orig: dict[tuple, object] = {}
        for mod, name in ((os, "stat"), (os, "lstat"), (os, "open"), (os, "listdir"),
                          (os, "scandir"), (os, "access"), (os, "mkdir"), (os, "makedirs"),
                          (os, "replace"), (os, "rename"), (os, "unlink"), (os, "remove"),
                          (io, "open"), (builtins, "open")):
            orig = getattr(mod, name)
            self._orig[(mod, name)] = orig
            setattr(mod, name, self._wrap(orig))

    def _check(self, path) -> None:
        try:
            s = os.fspath(path)
        except TypeError:
            return  # an fd or a non-path object
        if isinstance(s, bytes):
            s = s.decode(errors="replace")
        for g in self._guarded:
            if s == g or s.startswith(g + os.sep):
                self.violations.append(s)
                return

    def _wrap(self, orig):
        def w(path, *a, **k):
            self._check(path)
            return orig(path, *a, **k)
        w.__wrapped__ = orig
        return w

    def restore(self) -> None:
        for (mod, name), orig in self._orig.items():
            setattr(mod, name, orig)
        for k, v in self._saved_attrs.items():
            setattr(cus, k, v)
        if self._env_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._env_home
        self._tmp.cleanup()


@pytest.fixture(autouse=True)
def _sandboxed():
    sb = _Sandbox()
    try:
        yield sb
    finally:
        violations = list(sb.violations)
        sb.restore()
    assert not violations, f"test touched the live account directory: {sorted(set(violations))}"


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


def _rate_limited_acct(h5, h7) -> dict:
    a = _healthy_acct(h5, h7)
    a["rate_limited"] = True
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


_CREDS = {"claudeAiOauth": {"accessToken": "at-x", "refreshToken": "rt-x",
                            "expiresAt": 2_000_000_000_000}}


class _Net:
    """Stub the two endpoints: usage answers `usage_code` (429 by default),
    the profile answers `profile`. Anything else is an error. Patched by
    hand (not monkeypatch) so the standalone runner can use it too."""

    def __init__(self, profile: dict, usage_code: int = 429):
        self.profile, self.usage_code = profile, usage_code
        self._saved = cus.urllib.request.urlopen
        cus.urllib.request.urlopen = self._urlopen
        cus._reset_subscription_probe_cache()

    def _urlopen(self, req, timeout=None):
        url = req.full_url
        if url == cus.USAGE_API_URL:
            raise urllib.error.HTTPError(
                url, self.usage_code, "Too Many Requests", {},
                io.BytesIO(b'{"error":{"type":"rate_limit_error","message":"Rate limited."}}'))
        if url == cus.PROFILE_API_URL:
            return _FakeProfileResp(self.profile)
        raise AssertionError(f"unexpected URL {url}")

    def restore(self):
        cus.urllib.request.urlopen = self._saved
        cus._reset_subscription_probe_cache()


def _probe_verdict_for(profile: dict) -> str:
    """The profile probe's verdict for `profile`, through the real GET path
    (urlopen stubbed). This entry point exists on `main`."""
    net = _Net(profile)
    try:
        verdict, _detail = cus._probe_subscription_profile("tok")
        return verdict
    finally:
        net.restore()


# ---------------------------------------------------------------------------
# (1) the classifier — the issue's fixture pin, through the probe
# ---------------------------------------------------------------------------

def test_classifier_past_due_with_plan_attached_is_blocked():
    """THE PIN: same body, only the status string differs. Driven through
    `_probe_subscription_profile` so that on `main` it fails because the
    body is classified ACTIVE, not because a new function is missing."""
    verdict = _probe_verdict_for(_PROFILE_PAST_DUE)
    assert verdict == "past_due", (
        f"a past-due profile with the plan still attached classified as {verdict!r} — "
        "this is the 2026-10-11 hole: launches on it wall, cus must block placement")
    assert _probe_verdict_for(_same_body_with_status("active")) == "active"
    # The detail still carries the #237 tier for this verdict.
    _v, detail = cus._classify_subscription_profile(_PROFILE_PAST_DUE)
    assert detail["subscription_status"] == "past_due"
    assert detail["rate_limit_tier"] == "default_claude_max_5x"


def test_classifier_past_due_is_not_ended():
    """The back-compat ENDED view stays False: the plan is attached and can
    recover, so nothing that MOVES lanes (which keys on ENDED) may fire."""
    disabled, _ = cus._profile_says_subscription_disabled(_PROFILE_PAST_DUE)
    assert disabled is False
    assert cus._profile_says_subscription_disabled(_PROFILE_DEAD)[0] is True
    assert cus._profile_says_subscription_disabled(_PROFILE_ACTIVE)[0] is False


def test_classifier_precedence_and_fail_open():
    """Through the probe (exists on main). The seat-tier choice is
    PROVISIONAL: no captured profile of a team seat past due exists; it is
    pinned as active because #191 keeps seats out of scope, not because the
    shape was observed."""
    # ENDED signature outranks a past_due status on the same body.
    dead_and_past_due = copy.deepcopy(_PROFILE_DEAD)
    dead_and_past_due["organization"]["subscription_status"] = "past_due"
    assert _probe_verdict_for(dead_and_past_due) == "disabled"
    # A team/enterprise seat stays out of scope for both signatures (provisional).
    seat = copy.deepcopy(_PROFILE_PAST_DUE)
    seat["organization"]["seat_tier"] = "enterprise"
    assert _probe_verdict_for(seat) == "active"
    # The status string alone is the signal — plan flags are not consulted.
    no_plan = copy.deepcopy(_PROFILE_PAST_DUE)
    no_plan["account"] = {"has_claude_max": False, "has_claude_pro": False}
    no_plan["organization"]["organization_type"] = "something_new"
    assert _probe_verdict_for(no_plan) == "past_due"
    # Any other spelling, or no status at all, fails OPEN to active.
    for status in ("PAST_DUE", "pastdue", "past-due", "unpaid", "", None):
        assert _probe_verdict_for(_same_body_with_status(status)) == "active", status
    for junk in ({}, {"account": "x", "organization": None}, {"organization": {"subscription_status": 7}}):
        assert _probe_verdict_for(junk) == "active", junk


# ---------------------------------------------------------------------------
# (2) the full 429 poll path
# ---------------------------------------------------------------------------

def test_poll_429_with_past_due_profile_sets_past_due_not_disabled():
    env = _Env(_base_state(), accounts_creds={"a": _CREDS})
    net = _Net(_PROFILE_PAST_DUE)
    try:
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
        net.restore()
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


def test_past_due_survives_a_plain_429_and_a_stale_token():
    """Only a success or a probe 'active' may clear: a 429 with no probe
    verdict (probe off/unknown) and a token_stale poll leave the flag."""
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        state["accounts"]["a"].update(_past_due_acct())
        plain = cus.AccountUsage.empty()
        plain.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
                     "subscription_probe": "unknown"}
        cus.update_state_with_usage(state, {"a": plain})
        assert state["accounts"]["a"].get("subscription_past_due") is True
        stale = cus.AccountUsage.empty()
        stale.token_stale = True
        cus.update_state_with_usage(state, {"a": stale})
        assert state["accounts"]["a"].get("subscription_past_due") is True
        assert not [e for e in _audit_lines(env, "subscription-past-due") if "cleared" in e]
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
# (5) placement: never a swap target, never a launch pick at any tier
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


class _Occupancy:
    """Stub live occupancy (no /proc, no mount scan) by hand so the launch
    tier tests run standalone too."""

    def __init__(self, live: set):
        self._m, self._l = cus.mount_in_use, cus._live_slot_accounts
        cus.mount_in_use = lambda d: False
        cus._live_slot_accounts = lambda state: set(live)

    def restore(self):
        cus.mount_in_use, cus._live_slot_accounts = self._m, self._l


def _launch_cfg(gate=False, lane_sharing=False) -> dict:
    cfg = _cfg(strategy="lowest_usage")
    cfg["per_session"] = {"lane_sharing": lane_sharing}
    if gate:
        cfg["per_model_weekly"] = {"gate_enabled": True, "models": ["fable"],
                                   "cap_pct": 97, "target_cap_pct": 95}
    return cfg


def test_launch_raw_fallback_tier_skips_past_due():
    """Reaches the RAW fallback: the shimmed picker returns None because the
    only other account is plain rate-limited (soft-filtered) and the past-due
    one is hard-excluded. The raw tier does not filter rate_limited, so on
    `main` (and on a mutant that drops the exclusion from this tier only) it
    picks beta, the emptier, past-due account."""
    occ = _Occupancy(set())
    try:
        accts = {"beta": _past_due_flag_only(0, 0), "gamma": _rate_limited_acct(50, 50)}
        t = cus.pick_launch_account({"active": None, "accounts": accts, "slots": {}}, _launch_cfg())
        assert t is not None and t.name == "gamma", (
            f"launch placed onto past-due account {t and t.name!r} — the 2026-10-11 hole")
        assert "launch fallback" in t.reason, t.reason
    finally:
        occ.restore()


def test_launch_lane_share_tier_skips_past_due():
    """Reaches the LANE-SHARE fallback: every account is on a live mount
    (shim and raw tier both empty); with lane_sharing on the launch joins the
    lowest-usage JOINABLE lane — which must be gamma (50%), never past-due
    beta (0%)."""
    occ = _Occupancy({"beta", "gamma"})
    try:
        accts = {"beta": _past_due_flag_only(0, 0), "gamma": _healthy_acct(50, 50)}
        state = {"active": None, "accounts": accts,
                 "slots": {"slot-1": {"account": "beta"}, "slot-2": {"account": "gamma"}}}
        t = cus.pick_launch_account(state, _launch_cfg(lane_sharing=True))
        assert t is not None and t.name == "gamma", (
            f"lane-share fallback joined past-due account {t and t.name!r}")
        assert "lane-share fallback" in t.reason, t.reason
    finally:
        occ.restore()


def test_launch_least_bad_tier_skips_past_due():
    """Reaches the LEAST-BAD degraded tier: the per-model gate is on and
    every account is Fable-capped, so the shim and the raw tier are empty.
    The degraded pick is the lowest last-known Fable among NON-excluded
    accounts: gamma (98%), never past-due beta (96%)."""
    occ = _Occupancy(set())
    try:
        beta = _past_due_flag_only(5, 5)
        beta["per_model_weekly_pct"] = {"Fable": 96.0}
        gamma = _healthy_acct(30, 25)
        gamma["per_model_weekly_pct"] = {"Fable": 98.0}
        t = cus.pick_launch_account({"active": None, "accounts": {"beta": beta, "gamma": gamma},
                                     "slots": {}}, _launch_cfg(gate=True))
        assert t is not None and t.name == "gamma", (
            f"degraded tier placed onto past-due account {t and t.name!r}")
        assert "[DEGRADED:" in t.reason, t.reason
    finally:
        occ.restore()


# ---------------------------------------------------------------------------
# (6) no forced moves — with a LIVE lane, and a control that does move
# ---------------------------------------------------------------------------

class _LiveLane:
    """A slot directory that exists and reads as in use, with the move
    primitive's planner stubbed to 'clean install' so a mover that WANTS to
    move can. Hand-patched for the standalone runner."""

    def __init__(self, slot: str):
        cus.slot_path(slot).mkdir(parents=True, exist_ok=True)
        self._m, self._p = cus.mount_in_use, cus._slot_move_plan
        cus.mount_in_use = lambda d: True
        cus._slot_move_plan = lambda state, config, slot, cand: {"plan": "snapshot"}

    def restore(self):
        cus.mount_in_use, cus._slot_move_plan = self._m, self._p


def test_trigger_0_does_not_evict_a_live_lane_off_a_past_due_account():
    """decide_swap's Trigger 0 evicts the ACTIVE account's lane when that
    account is operator-disabled. A past-due active must hold. Control: the
    same state with the account operator-disabled evicts onto gamma."""
    accts = {"alpha": _healthy_acct(95, 40), "beta": _past_due_flag_only(10, 5),
             "gamma": _healthy_acct(30, 20)}
    state = {"active": "beta", "accounts": accts, "slots": {}}
    saved = cus.mount_in_use
    cus.mount_in_use = lambda d: True          # the lane on beta is live
    try:
        d = cus.decide_swap(state, _cfg(), {})
        assert d is None, f"a past-due active account was evicted: {d}"
        ctl_cfg = _cfg()
        ctl_cfg["accounts"] = [{"name": "alpha", "priority": 1},
                               {"name": "beta", "priority": 1, "disabled": True},
                               {"name": "gamma", "priority": 1}]
        ctl = cus.decide_swap(state, ctl_cfg, {})
        assert ctl is not None and ctl.gate == "disabled_evict" and ctl.target == "gamma", ctl
    finally:
        cus.mount_in_use = saved


def test_live_lane_on_past_due_account_is_not_evacuated():
    """`_evacuation_candidates` with a LIVE lane on the past-due account must
    return nothing (and the daemon sweep must have nothing to execute).
    Control: the same live lane on an ENDED account is a candidate."""
    env = _Env(_base_state())
    lane = _LiveLane("slot-1")
    try:
        cfg = _cfg()
        cfg["housekeeping"] = {"auto_evacuate": True}
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(10, 10), "beta": _past_due_acct()},
                 "slots": {"slot-1": {"account": "beta"}}}
        assert cus._evacuation_candidates(state, cfg) == []
        assert cus._sweep_evacuate_subscription_dead(state, cfg, no_execute=True) == []
        # Not a shared-mount migration reason either.
        assert cus._shared_mount_account_unhealthy("beta", state, cfg) is None
        # Control: ENDED on the same live lane IS evacuated / migrated.
        state["accounts"]["beta"] = _sub_dead_acct()
        cands = cus._evacuation_candidates(state, cfg)
        assert cands and cands[0]["slot"] == "slot-1" and cands[0]["target"] == "alpha", cands
        assert "subscription-ended" in (cus._shared_mount_account_unhealthy("beta", state, cfg) or "")
    finally:
        lane.restore()
        env.restore()


def test_past_due_account_is_not_an_evacuation_target():
    """beta is ENDED with a live lane; gamma is past due and emptier than
    alpha — the rescue must land on alpha."""
    env = _Env(_base_state())
    lane = _LiveLane("slot-1")
    try:
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(60, 50), "beta": _sub_dead_acct(),
                              "gamma": _past_due_acct(h5=1, h7=1)},
                 "slots": {"slot-1": {"account": "beta"}}}
        cands = cus._evacuation_candidates(state, _cfg())
        assert cands and cands[0]["account"] == "beta"
        assert cands[0]["target"] == "alpha", cands
    finally:
        lane.restore()
        env.restore()


def test_warm_spare_floor_skips_a_past_due_account():
    """A past-due account needs a payment, not a browser login: no
    'warm spare' nag for it. Control: a healthy account with no families
    does get the nag."""
    env = _Env(_base_state())
    try:
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(10, 10), "beta": _past_due_acct()},
                 "slots": {}}
        cfg = _cfg()
        cfg["housekeeping"] = {"warm_spare_families": 1}
        # Each account has ONE family store, blank-shaped (not claimable), so
        # its warm count is 0 < 1 — the floor fires for any account the
        # check does not skip.
        for name in ("alpha", "beta"):
            p = cus.login_family_creds_path(name, "family-1")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        msgs = cus._warm_spare_warnings(state, cfg)
        assert any(m.startswith("alpha:") for m in msgs), msgs
        assert not any(m.startswith("beta:") for m in msgs), msgs
    finally:
        env.restore()


def test_exclusion_sets_are_split_the_right_way():
    """The mechanism behind (6): placement sets include past due, mover sets
    do not."""
    state = {"active": "beta",
             "accounts": {"alpha": _healthy_acct(10, 10), "beta": _past_due_acct()},
             "slots": {}}
    cfg = _cfg()
    assert "beta" not in cus._disabled_accounts(cfg)                 # Trigger 0's set
    assert "beta" in cus._disabled_accounts(cfg, state)              # pickers' set
    assert "beta" in cus._subscription_blocked_accounts(state, cfg)  # refusals' set
    assert "beta" not in cus._subscription_dead_accounts(state, cfg)  # movers' set


# ---------------------------------------------------------------------------
# (7) SOS names the fix; target math; guard off restores pre-guard behavior
# ---------------------------------------------------------------------------

def _past_due_conds(conds):
    return [c for c in conds if "PAST DUE" in c.summary]


def test_sos_past_due_condition_names_the_billing_fix():
    env = _Env(_base_state())
    try:
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
        env.restore()


def test_sos_target_math_counts_past_due_as_unavailable_even_with_allow_rate_limited():
    """Condition 2: the active account is over its step and the only other
    account is past due. With allow_rate_limited_targets on, a plain 429
    account counts as available (control: no line); a past-due one must not
    (the picker hard-excludes it), so the 'no other account' line fires."""
    env = _Env(_base_state())
    try:
        cfg = {**_cfg(), "mode": "global"}
        cfg["smart_strategy"] = {"allow_rate_limited_targets": True}
        alpha = _healthy_acct(90, 40)                      # ≥ its 80% step
        state = {"active": "alpha", "accounts": {"alpha": alpha, "beta": _past_due_acct()}, "slots": {}}
        hits = [c for c in cus.diagnose(state, cfg) if "no other account available" in c.summary]
        assert len(hits) == 1 and hits[0].affected == "alpha", hits
        state["accounts"]["beta"] = _rate_limited_acct(0, 0)  # control
        assert not [c for c in cus.diagnose(state, cfg) if "no other account available" in c.summary]
    finally:
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
    try:
        assert _past_due_conds(cus.diagnose(state, {**_cfg(guard=off), "mode": "per_session"})) == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) explicit-placement refusals: launch, slot move, switch, auto-swap
# ---------------------------------------------------------------------------

def _refusal_env() -> _Env:
    return _Env({"active": "other",
                 "accounts": {"acct": _past_due_acct(), "dead": _sub_dead_acct(),
                              "other": _healthy_acct(1, 1), "fresh": _healthy_acct(2, 2)},
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


def test_auto_swap_refuses_past_due_and_ended_targets():
    """`cus auto-swap <acct>` used to validate only that the account exists
    (review H2). It must refuse past due AND ended; the control target is
    swapped. The swap primitive is replaced by a recorder."""
    env = _refusal_env()
    calls: list[str] = []
    saved = cus.execute_swap
    cus.execute_swap = lambda name, **k: calls.append(name)
    try:
        r = CliRunner().invoke(cus.cli, ["auto-swap", "acct"])
        joined = "\n".join(env.echoes)
        assert r.exit_code != 0 and "PAST DUE" in joined and "Settings, Billing" in joined, (r.exit_code, joined)
        env.echoes.clear()
        r = CliRunner().invoke(cus.cli, ["auto-swap", "dead"])
        joined = "\n".join(env.echoes)
        assert r.exit_code != 0 and "NO active Claude subscription" in joined, (r.exit_code, joined)
        assert calls == [], f"auto-swap moved onto a blocked account: {calls}"
        env.echoes.clear()
        r = CliRunner().invoke(cus.cli, ["auto-swap", "fresh"])       # control
        assert r.exit_code == 0 and calls == ["fresh"], (r.exit_code, calls, env.echoes)
    finally:
        cus.execute_swap = saved
        env.restore()


# ---------------------------------------------------------------------------
# (9) force-poll — the operator's entry point — records a 429
# ---------------------------------------------------------------------------

def _force_poll_env() -> _Env:
    return _Env({"active": "other",
                 "accounts": {"acct": _healthy_acct(10, 5), "other": _healthy_acct(1, 1)},
                 "slots": {}, "swap_history": []},
                config={"mode": "per_session"},
                accounts_creds={"acct": _CREDS, "other": _CREDS})


def test_force_poll_429_with_past_due_profile_sets_the_flag_and_everything_follows():
    """Review H1 (both seats): force-poll compared the error STRING to
    'rate_limited' and so never saved a 429 — exit 4, nothing written, no
    flag. Now: the flag is saved, counted once, and status, SOS, launch and
    switch all follow from the saved state."""
    env = _force_poll_env()
    net = _Net(_PROFILE_PAST_DUE)
    try:
        r = CliRunner().invoke(cus.cli, ["force-poll", "acct"])
        out = "\n".join(env.echoes) + r.output
        assert r.exit_code == 3, (r.exit_code, out)
        acct = cus.load_state()["accounts"]["acct"]
        assert acct.get("subscription_past_due") is True, (
            f"force-poll did not record the past-due 429 (saved flags: "
            f"{ {k: v for k, v in acct.items() if 'subscription' in k or k == 'rate_limited'} })")
        assert acct.get("rate_limited") is True and acct.get("subscription_past_due_at")
        assert acct.get("poll_backoff_consecutive_429s") == 1, "the 429 must be counted once"
        assert acct["current_5h_pct"] == 10, "prior percentages preserved"
        assert "PAST_DUE" in out and "Billing" in out, out
        assert len(_audit_lines(env, "subscription-past-due")) == 1, env.echoes
        # Everything downstream reads the saved state:
        env.echoes.clear()
        r = CliRunner().invoke(cus.cli, ["status"])
        row = next(line for line in ("\n".join(env.echoes) + r.output).splitlines() if line.startswith("acct"))
        assert "PAST_DUE" in row and "RATE_LIMITED" not in row, row
        assert _past_due_conds(cus.diagnose(cus.load_state(), cus.load_config()))
        with pytest.raises(click.ClickException, match="PAST DUE"):
            cus._launch_prepare("acct", cus.load_state(), cus.load_config())
        env.echoes.clear()
        assert CliRunner().invoke(cus.cli, ["switch", "acct"]).exit_code != 0
        assert "PAST DUE" in "\n".join(env.echoes)
    finally:
        net.restore()
        env.restore()


def test_force_poll_429_with_active_profile_clears_the_flag():
    """The recovery case (Opus M1): payment cleared, usage still throttled.
    force-poll must clear the flag, exactly as `cus poll` does."""
    env = _force_poll_env()
    st = cus.load_state(); st["accounts"]["acct"].update(_past_due_acct(10, 5)); cus.save_state(st)
    net = _Net(_PROFILE_ACTIVE)
    try:
        r = CliRunner().invoke(cus.cli, ["force-poll", "acct"])
        out = "\n".join(env.echoes) + r.output
        assert r.exit_code == 3, (r.exit_code, out)
        acct = cus.load_state()["accounts"]["acct"]
        assert "subscription_past_due" not in acct and "subscription_past_due_at" not in acct, acct
        assert acct.get("rate_limited") is True, "still throttled"
        assert "STILL 429" in out and "cleared" in out, out
        assert [e for e in _audit_lines(env, "subscription-past-due") if "cleared" in e]
    finally:
        net.restore()
        env.restore()


def test_force_poll_429_with_ended_profile_sets_sub_ended():
    """The same dead branch hid the #191 ENDED flag from force-poll since
    2026-08-07; pin that it is recorded now too."""
    env = _force_poll_env()
    net = _Net(_PROFILE_DEAD)
    try:
        r = CliRunner().invoke(cus.cli, ["force-poll", "acct"])
        out = "\n".join(env.echoes) + r.output
        assert r.exit_code == 3, (r.exit_code, out)
        acct = cus.load_state()["accounts"]["acct"]
        assert acct.get("subscription_disabled") is True and "subscription_past_due" not in acct, acct
        assert "SUB_ENDED" in out, out
    finally:
        net.restore()
        env.restore()


# ---------------------------------------------------------------------------
# (10) what the operator sees
# ---------------------------------------------------------------------------

def test_session_binding_blocked_label():
    sev, txt = cus._session_binding(_past_due_acct(), "premium", {})
    assert sev == "blocked" and "PAST DUE" in txt and "Billing" in txt, (sev, txt)


def _status_env() -> _Env:
    return _Env({"active": "other",
                 "accounts": {"acct": _past_due_acct(), "other": _healthy_acct(1, 1)},
                 "slots": {}, "swap_history": []},
                config={"mode": "per_session"})


def test_status_shows_past_due_not_rate_limited():
    env = _status_env()
    try:
        r = CliRunner().invoke(cus.cli, ["status"])
        assert r.exit_code == 0, (r.output, env.echoes)
        joined = "\n".join(env.echoes) + r.output
        row = next(line for line in joined.splitlines() if line.startswith("acct"))
        assert "PAST_DUE" in row and "RATE_LIMITED" not in row and "SUB_ENDED" not in row, row
    finally:
        env.restore()


def test_statusline_shows_past_due_flag():
    """Verbose: the per-account flag reads (PAST-DUE), not (429). Every mode:
    the SOS one-liner names the past-due account (the statusline is where
    the 2026-10-11 account hid as a plain 429 — see the 2026-08-07 note)."""
    env = _status_env()
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
        env.restore()


def test_panes_payload_marks_past_due_as_not_a_destination():
    """Through build_panes_payload (the `cus panes --json` producer): the
    watchdog reads `accounts[a]["disabled"]` to pick a destination, so a
    past-due account must carry disabled=true, and past_due=true for the
    label; a healthy account carries neither. The rendered header says so."""
    saved = {k: getattr(cus, k) for k in ("read_panes_from_reader", "read_peer_registry",
                                          "load_state", "load_config")}
    cus.read_panes_from_reader = lambda include_all=True: ([], None)
    cus.read_peer_registry = lambda: {}
    cus.load_state = lambda: {"active": "gamma", "slots": {},
                              "accounts": {"beta": _past_due_acct(), "gamma": _healthy_acct(5, 5)}}
    cus.load_config = lambda: dict(cus.DEFAULT_CONFIG)
    try:
        payload = cus.build_panes_payload(30)
        beta, gamma = payload["accounts"]["beta"], payload["accounts"]["gamma"]
        assert beta["disabled"] is True and beta["past_due"] is True, beta
        assert gamma["disabled"] is False and gamma["past_due"] is False, gamma
        json.dumps(payload)
        text = cus.render_panes_table(payload)
        assert "== beta" in text and "PAST DUE" in text and "[DISABLED]" not in text, text
        assert "existing panes stay" in text
    finally:
        for k, v in saved.items():
            setattr(cus, k, v)


def test_panes_header_says_past_due_and_excludes_as_destination():
    now = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
    h = {"known": False, "reason": "no reading", "past_due": True, "disabled": True}
    hdr = cus._account_header("beta", h, now)
    assert "PAST DUE" in hdr and "[DISABLED]" not in hdr, hdr
    assert "existing panes stay" in hdr


# ---------------------------------------------------------------------------
# (11) deep prune: a past_due verdict on a store's own token never retires it
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
        sb = _Sandbox()
        try:
            fn()
        finally:
            viol = list(sb.violations)
            sb.restore()
        assert not viol, f"{fn.__name__} touched the live account directory: {viol}"
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed (sandboxed)")
