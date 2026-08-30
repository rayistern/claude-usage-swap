"""Tests for subscription-ended (org-disabled) auto-detection + auto-exclusion
(2026-08-07, the rayi5/rayi6 cancellation incident).

THE HOLE: when a Claude subscription is cancelled, the usage endpoint starts
answering polls with a GENERIC HTTP 429 `rate_limit_error` — byte-identical to
a real transient rate limit (verified live 2026-08-07: rayi5's dead token and
rayi1's healthy one, same endpoint). cus therefore classified dead-subscription
accounts as merely `rate_limited` — a SOFT filter that pick_launch_account's
raw fallbacks don't apply at all — so new lanes kept landing on them, each
opening straight into "Your organization has disabled Claude subscription
access for Claude Code". The #190 liveness gate can't catch it either: the
account's OAuth still authenticates (profile 200s, refresh grants pass).

The guard's contract, pinned here:
  (1) the pure profile classifier requires the FULL observed dead signature
      (has_claude_max/pro false + organization_type "claude_free") and fails
      OPEN on anything ambiguous;
  (2) a 429 poll whose profile probe says "disabled" flags the account
      `subscription_disabled` in state (alongside rate_limited) and emits ONE
      CRED-AUDIT line on the off→on transition — not every cycle;
  (3) a flagged account is NEVER picked by pick_swap_target (hard, no
      fallback, even under allow_rate_limited_targets) nor by
      pick_launch_account's raw fallback tiers;
  (4) `cus sos` (diagnose) surfaces a warning naming the account + the
      renewal remediation;
  (5) recovery self-heals: a successful poll — or an "active" probe verdict
      while still 429ing — clears the flag;
  (6) subscription_guard.enabled=False restores pre-guard behavior at BOTH
      the probe point and every read point (stale state flags ignored);
  (7) the probe is cooldown-cached (one profile GET per account per window);
  (8) explicit `cus launch <sub-dead account>` is refused with the
      remediation (--force overrides); `cus slot move` refuses the same way.

Run standalone:  python3 tests/test_subscription_disabled.py
Run under pytest: pytest tests/test_subscription_disabled.py
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


# ---------------------------------------------------------------------------
# Captured live profile shapes (2026-08-07 probes — the load-bearing evidence)
# ---------------------------------------------------------------------------

# rayi5 the day after its subscription was cancelled: profile still 200s.
_PROFILE_DEAD = {
    "account": {"uuid": "u5", "email": "rayi5@x", "has_claude_max": False,
                "has_claude_pro": False},
    "organization": {"uuid": "o5", "organization_type": "claude_free",
                     "billing_type": "none", "rate_limit_tier": "default_claude_ai",
                     "seat_tier": None, "subscription_status": "canceled"},
}

# rayi1 with an active Max subscription, same probe.
_PROFILE_ACTIVE = {
    "account": {"uuid": "u1", "email": "rayi1@x", "has_claude_max": True,
                "has_claude_pro": False},
    "organization": {"uuid": "o1", "organization_type": "claude_max",
                     "billing_type": "stripe_subscription",
                     "rate_limit_tier": "default_claude_max_5x",
                     "seat_tier": None, "subscription_status": "active"},
}


def _sub_dead_acct(h5=0.0, h7=0.0):
    """A state-dict account exactly as Branch 1.5 leaves it: flagged AND
    rate_limited (the 429 is the symptom of the ended subscription)."""
    return {"current_5h_pct": h5, "current_7d_pct": h7, "next_swap_at_pct": 80,
            "last_swap_ts": None, "subscription_disabled": True,
            "rate_limited": True,
            "subscription_detail": {"subscription_status": "canceled",
                                    "organization_type": "claude_free"}}


def _healthy_acct(h5, h7):
    return {"current_5h_pct": h5, "current_7d_pct": h7,
            "next_swap_at_pct": 80, "last_swap_ts": None}


def _cfg(strategy="smart", guard=None):
    cfg = {
        "strategy": strategy,
        "accounts": [{"name": "alpha", "priority": 1},
                     {"name": "beta", "priority": 1},
                     {"name": "gamma", "priority": 1}],
        "thresholds": {"five_hour": True, "seven_day": True, "steps": [80, 90]},
        "swap_hysteresis": {"enabled": False},
        "usage_growth_gate": {"enabled": False},
    }
    if guard is not None:
        cfg["subscription_guard"] = guard
    return cfg


# ---------------------------------------------------------------------------
# Env: repointed paths + click.echo capture (pattern from test_poll_token_stale)
# ---------------------------------------------------------------------------

class _Env:
    """Repoint STATE_JSON / CONFIG_YAML / ACCOUNTS_DIR at a throwaway tree and
    capture click.echo, so update_state_with_usage (which calls load_config())
    and _cred_audit run against the sandbox, never the real machine."""

    def __init__(self, state: dict, config: dict | None = None,
                 accounts_creds: dict[str, dict] | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.accounts_dir = root / "claude-accounts"
        self.accounts_dir.mkdir()
        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps(state))
        self.config_yaml = self.accounts_dir / "config.yaml"
        if config is not None:
            cus.write_yaml(self.config_yaml, config)
        for name, creds in (accounts_creds or {}).items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))

        self._saved = {k: getattr(cus, k) for k in
                       ("STATE_JSON", "CONFIG_YAML", "ACCOUNTS_DIR", "HOME", "CLAUDE_DIR")}
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.HOME = root
        cus.CLAUDE_DIR = root / ".claude"

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")
        cus._reset_subscription_probe_cache()

    def restore(self):
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus._reset_subscription_probe_cache()
        self._tmp.cleanup()


def _audit_lines(env: _Env, op: str) -> list[str]:
    return [e for e in env.echoes if f"op={op}" in e]


# ---------------------------------------------------------------------------
# (1) pure classifier: only the full dead signature flags; ambiguity fails open
# ---------------------------------------------------------------------------

def test_classifier_dead_signature_flags():
    disabled, detail = cus._profile_says_subscription_disabled(_PROFILE_DEAD)
    assert disabled is True
    assert detail["subscription_status"] == "canceled"
    assert detail["organization_type"] == "claude_free"


def test_classifier_active_max_not_flagged():
    disabled, _ = cus._profile_says_subscription_disabled(_PROFILE_ACTIVE)
    assert disabled is False


def test_classifier_fails_open_on_ambiguity():
    """Missing keys, empty payloads, unknown org types, enterprise seats: all
    must classify NOT-disabled — schema drift may only ever fail open."""
    cases = [
        {},                                                     # empty
        {"account": {}, "organization": {}},                    # keys absent
        {"account": {"has_claude_max": False},                  # pro key absent
         "organization": {"organization_type": "claude_free"}},
        {"account": {"has_claude_max": False, "has_claude_pro": False},
         "organization": {"organization_type": "mystery_new_type"}},  # unknown org
        {"account": {"has_claude_max": False, "has_claude_pro": False},
         "organization": {"organization_type": "claude_free",
                          "seat_tier": "enterprise"}},          # team/enterprise seat
        {"account": "garbage", "organization": None},           # malformed types
    ]
    for profile in cases:
        disabled, _ = cus._profile_says_subscription_disabled(profile)
        assert disabled is False, f"must fail open on {profile!r}"


# ---------------------------------------------------------------------------
# (2) poll → state: flag set alongside rate_limited; audit line fires ONCE
# ---------------------------------------------------------------------------

def _disabled_usage() -> "cus.AccountUsage":
    """AccountUsage exactly as poll_account_usage returns it when the 429's
    profile probe came back 'disabled'."""
    u = cus.AccountUsage.empty()
    u.subscription_disabled = True
    u.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
             "subscription_probe": "disabled",
             "subscription": {"subscription_status": "canceled",
                              "organization_type": "claude_free"}}
    return u


def _base_state():
    return {"active": "other",
            "accounts": {"a": {"current_5h_pct": 10.0, "current_7d_pct": 5.0},
                         "other": {"current_5h_pct": 1.0, "current_7d_pct": 1.0}},
            "swap_history": []}


def test_update_state_flags_and_audits_once():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        cus.update_state_with_usage(state, {"a": _disabled_usage()})
        acct = state["accounts"]["a"]
        assert acct["subscription_disabled"] is True
        assert acct["rate_limited"] is True, "the 429 symptom must stay visible"
        assert acct.get("subscription_disabled_at"), "transition must be stamped"
        assert acct["subscription_detail"]["subscription_status"] == "canceled"
        # Prior percentages preserved (same contract as every error branch).
        assert acct["current_5h_pct"] == 10.0
        first = _audit_lines(env, "subscription-disabled")
        assert len(first) == 1 and "auto-excluded" in first[0] and "account=a" in first[0], first
        # Second poll cycle with the same verdict: flag persists, NO re-shout.
        cus.update_state_with_usage(state, {"a": _disabled_usage()})
        assert len(_audit_lines(env, "subscription-disabled")) == 1, env.echoes
    finally:
        env.restore()


def test_successful_poll_clears_flag():
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        state["accounts"]["a"].update(_sub_dead_acct())
        ok = cus.AccountUsage(five_hour=cus.UsageWindow(12.0, None),
                              seven_day=cus.UsageWindow(3.0, None))
        cus.update_state_with_usage(state, {"a": ok})
        acct = state["accounts"]["a"]
        assert "subscription_disabled" not in acct
        assert "subscription_detail" not in acct
        cleared = [e for e in _audit_lines(env, "subscription-disabled") if "cleared" in e]
        assert cleared, env.echoes
    finally:
        env.restore()


def test_still_429_but_probe_active_clears_flag():
    """Renewed-but-throttled: usage still 429s, probe says 'active' — the flag
    must clear so the account re-enters rotation when the throttle lifts."""
    env = _Env(_base_state())
    try:
        state = json.loads(env.state_json.read_text())
        state["accounts"]["a"].update(_sub_dead_acct())
        u = cus.AccountUsage.empty()
        u.raw = {"error": "HTTP 429 (rate_limited): ...", "rate_limited": True,
                 "subscription_probe": "active",
                 "subscription": {"subscription_status": "active"}}
        cus.update_state_with_usage(state, {"a": u})
        acct = state["accounts"]["a"]
        assert "subscription_disabled" not in acct
        assert acct["rate_limited"] is True, "still throttled — rate_limited stays"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (3) exclusion: never a swap target, never a launch fallback pick
# ---------------------------------------------------------------------------

def test_pick_swap_target_excludes_sub_dead_even_when_best():
    """beta is the obvious winner (0% everywhere) but subscription-dead — the
    pick must land on gamma, for every strategy."""
    accts = {"alpha": _healthy_acct(95, 40), "beta": _sub_dead_acct(),
             "gamma": _healthy_acct(30, 20)}
    for strategy in ("smart", "headroom", "lowest_usage", "drain",
                     "round_robin", "strict_priority"):
        t = cus.pick_swap_target({"active": "alpha", "accounts": accts},
                                 _cfg(strategy=strategy))
        assert t is not None and t.name == "gamma", f"{strategy}: {t and t.name}"


def test_pick_swap_target_sub_dead_only_candidate_holds():
    """HARD exclusion, no fallback — even allow_rate_limited_targets (which
    would re-admit a plain-429 account) must not re-admit a sub-dead one."""
    accts = {"alpha": _healthy_acct(95, 40), "beta": _sub_dead_acct()}
    cfg = _cfg()
    cfg["smart_strategy"] = {"allow_rate_limited_targets": True}
    t = cus.pick_swap_target({"active": "alpha", "accounts": accts}, cfg)
    assert t is None, t


def test_launch_raw_fallbacks_skip_sub_dead(monkeypatch):
    """pick_launch_account's raw fallbacks bypass pick_swap_target — the exact
    tier that kept landing lanes on rayi5/rayi6 (they don't filter
    rate_limited at all). Force the fallback path by saturating everything."""
    monkeypatch.setattr(cus, "mount_in_use", lambda d: False)
    monkeypatch.setattr(cus, "_live_slot_accounts", lambda state: set())
    accts = {"beta": _sub_dead_acct(h5=99, h7=95), "gamma": _healthy_acct(99, 96)}
    t = cus.pick_launch_account({"active": None, "accounts": accts, "slots": {}}, _cfg())
    # beta has the lower estimated usage but its subscription is dead → gamma.
    assert t is not None and t.name == "gamma", t and t.name


# ---------------------------------------------------------------------------
# (4) SOS: warning names the account + the renewal remediation
# ---------------------------------------------------------------------------

def _sub_conds(conds):
    return [c for c in conds if "subscription ENDED" in c.summary]


def test_sos_condition_fires_with_remediation():
    env = _Env(_base_state())
    try:
        state = {"active": "alpha",
                 "accounts": {"alpha": _healthy_acct(10, 10), "beta": _sub_dead_acct()},
                 "slots": {}}
        conds = _sub_conds(cus.diagnose(state, {**_cfg(), "mode": "per_session"}))
        assert len(conds) == 1, conds
        c = conds[0]
        assert c.affected == "beta" and c.severity == "warning"
        assert "beta" in c.summary
        assert "force-poll beta" in c.action and "Anthropic console" in c.action
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (6) config gate off = pre-guard behavior everywhere (stale flags ignored)
# ---------------------------------------------------------------------------

def test_guard_off_restores_old_behavior():
    off = {"enabled": False}
    # Flag only (no rate_limited) isolates the exclusion lever itself.
    beta = {"current_5h_pct": 0.0, "current_7d_pct": 0.0, "next_swap_at_pct": 80,
            "last_swap_ts": None, "subscription_disabled": True}
    accts = {"alpha": _healthy_acct(95, 40), "beta": beta,
             "gamma": _healthy_acct(30, 20)}
    state = {"active": "alpha", "accounts": accts, "slots": {}}
    # Guard ON: excluded. Guard OFF: beta wins again (best headroom).
    assert cus.pick_swap_target(state, _cfg()).name == "gamma"
    assert cus.pick_swap_target(state, _cfg(guard=off)).name == "beta"
    # SOS quiet with the guard off, even with the stale flag present.
    env = _Env(_base_state())
    try:
        cfg = {**_cfg(guard=off), "mode": "per_session"}
        assert _sub_conds(cus.diagnose(state, cfg)) == []
    finally:
        env.restore()


def test_probe_verdict_off_never_probes():
    def _boom(token):
        raise AssertionError("profile probe must not fire with the guard off")
    saved = cus._probe_subscription_profile
    cus._probe_subscription_profile = _boom
    try:
        cus._reset_subscription_probe_cache()
        verdict, detail = cus._subscription_probe_verdict(
            "a", "tok", {"subscription_guard": {"enabled": False}})
        assert (verdict, detail) == ("off", {})
    finally:
        cus._probe_subscription_profile = saved


# ---------------------------------------------------------------------------
# (7) probe cooldown: one profile GET per account per window
# ---------------------------------------------------------------------------

def test_probe_cooldown_caches_verdict():
    calls: list[str] = []

    def _fake(token):
        calls.append(token)
        return "disabled", {"subscription_status": "canceled"}

    saved = cus._probe_subscription_profile
    cus._probe_subscription_profile = _fake
    try:
        cus._reset_subscription_probe_cache()
        cfg = {"subscription_guard": {"enabled": True, "probe_cooldown_minutes": 30}}
        for _ in range(3):
            verdict, _detail = cus._subscription_probe_verdict("a", "tok", cfg)
            assert verdict == "disabled"
        assert calls == ["tok"], f"expected one probe inside the cooldown, got {calls}"
        # A different account is its own cache row.
        cus._subscription_probe_verdict("b", "tok-b", cfg)
        assert calls == ["tok", "tok-b"]
        # Reset (≈ cooldown expiry) re-probes.
        cus._reset_subscription_probe_cache()
        cus._subscription_probe_verdict("a", "tok", cfg)
        assert len(calls) == 3
    finally:
        cus._probe_subscription_profile = saved
        cus._reset_subscription_probe_cache()


# ---------------------------------------------------------------------------
# (2b) end-to-end poll: 429 + disabled-profile → AccountUsage.subscription_disabled
# ---------------------------------------------------------------------------

class _FakeProfileResp:
    """Minimal urlopen context-manager for the profile GET."""

    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode()
        self.status = 200

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_poll_429_with_dead_profile_sets_flag(monkeypatch):
    """Full poll_account_usage path: usage 429s (generic body), profile probe
    answers with the dead signature → the returned AccountUsage carries
    subscription_disabled=True AND the plain rate_limited raw flag."""
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
                    io.BytesIO(b'{"error":{"type":"rate_limit_error",'
                               b'"message":"Rate limited."}}'))
            if url == cus.PROFILE_API_URL:
                return _FakeProfileResp(_PROFILE_DEAD)
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_urlopen)
        u = cus.poll_account_usage("a")
        assert u.subscription_disabled is True
        assert u.raw.get("rate_limited") is True
        assert u.raw.get("subscription_probe") == "disabled"
        assert u.raw["subscription"]["subscription_status"] == "canceled"
    finally:
        env.restore()


def test_poll_429_with_active_profile_stays_plain_rate_limited(monkeypatch):
    """A REAL rate limit on a healthy subscription must classify exactly as
    before the guard: rate_limited only, no subscription flag."""
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
                    io.BytesIO(b'{"error":{"type":"rate_limit_error"}}'))
            if url == cus.PROFILE_API_URL:
                return _FakeProfileResp(_PROFILE_ACTIVE)
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr(cus.urllib.request, "urlopen", _fake_urlopen)
        u = cus.poll_account_usage("a")
        assert u.subscription_disabled is False
        assert u.raw.get("rate_limited") is True
        assert u.raw.get("subscription_probe") == "active"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (8) explicit-placement refusals: launch + slot move (--force overrides)
# ---------------------------------------------------------------------------

def test_launch_prepare_refuses_explicit_sub_dead():
    env = _Env({"active": "other",
                "accounts": {"acct": _sub_dead_acct(), "other": _healthy_acct(1, 1)},
                "slots": {}, "swap_history": []},
               config={"mode": "per_session"})
    try:
        with pytest.raises(click.ClickException) as ei:
            cus._launch_prepare("acct", cus.load_state(), cus.load_config())
        msg = str(ei.value)
        assert "NO active Claude subscription" in msg and "force-poll acct" in msg, msg
    finally:
        env.restore()


def test_session_binding_blocked_label():
    sev, txt = cus._session_binding(_sub_dead_acct(), "premium", {})
    assert sev == "blocked" and "subscription ENDED" in txt, (sev, txt)


def test_slot_move_refuses_sub_dead_target():
    env = _Env({"active": "other",
                "accounts": {"acct": _sub_dead_acct(), "other": _healthy_acct(1, 1)},
                "slots": {}, "swap_history": []},
               config={"mode": "per_session"})
    try:
        from click.testing import CliRunner
        result = CliRunner().invoke(cus.cli, ["slot", "move", "slot-1", "acct"])
        assert result.exit_code != 0
        joined = "\n".join(env.echoes)
        assert "NO active Claude subscription" in joined, joined
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and "monkeypatch" not in v.__code__.co_varnames]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"{len(fns)} tests passed (monkeypatch-dependent tests run under pytest)")
