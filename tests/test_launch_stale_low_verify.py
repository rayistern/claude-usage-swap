"""Launch-pick fresh-reading verify-and-repick guard (2026-07-07).

Regression tests for the rayi5-98%-twice incident: `cus launch auto` twice
launched a new session onto rayi5 while rayi5 was really at 5h 98% (nearly
capped). Root cause: pick_launch_account scores accounts off their CACHED
state readings, and an IDLE account's cached 5h% goes STALE-LOW (it's polled
rarely) — worse, the launch SPREAD preference actively FAVORS an idle account
as a "fresh" target. So the launch landed on a stale-low-actually-capped
account; the daemon then force-polled it, saw 98%, and bounced the new session
straight off.

The fix wraps pick_launch_account with a fresh-reading verification inside
_launch_prepare: force-poll the chosen candidate, re-check the LIVE number
against the picker's own HARD saturation lines, and re-pick (candidate
excluded) if it was stale-low. This exercises that loop with poll_account_usage
mocked to return controlled fresh readings (no real network).

Run standalone:  python3 tests/test_launch_stale_low_verify.py
Run under pytest: pytest tests/test_launch_stale_low_verify.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, expires_at: int = 2_000_000_000_000) -> dict:
    return {"claudeAiOauth": {"accessToken": f"at-{refresh}", "refreshToken": refresh, "expiresAt": expires_at}}


def _identity(name: str) -> dict:
    return {"userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x", "accountUuid": f"uuid-{name}"}}


def _usage(five: float = 0.0, seven: float = 0.0, *, stale: bool = False,
           expired: bool = False, error: str | None = None) -> "cus.AccountUsage":
    """Build a controlled AccountUsage for the poll mock. A success reading sets
    the 5h/7d windows (so update_state_with_usage writes current_*_pct); an
    error variant leaves them None and sets the flag so the guard degrades."""
    u = cus.AccountUsage.empty()
    if not (stale or expired or error):
        u.five_hour = cus.UsageWindow(utilization=five, resets_at=None)
        u.seven_day = cus.UsageWindow(utilization=seven, resets_at=None)
    u.token_stale = stale
    u.token_expired = expired
    if error:
        u.raw = {"error": error}
    return u


class _Env:
    """Temp HOME with three accounts (alpha/beta/gamma), gamma the shared-mount
    active. Mirrors tests/test_launch_and_detection.py's _Env so _launch_prepare
    runs its full slot-acquire/install flow, plus a swappable poll_account_usage
    hook the verify loop calls."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.claude_dir = root / ".claude"
        self.claude_json = root / ".claude.json"
        self.accounts_dir = root / "claude-accounts"

        (self.claude_dir / "projects").mkdir(parents=True)
        (self.claude_dir / "settings.json").write_text("{}")
        (self.claude_dir / ".credentials.json").write_text(json.dumps(_creds("rt-gamma")))
        self.claude_json.write_text(json.dumps({**_identity("gamma"), "mcpServers": {"m": {}}}))

        for name in ("alpha", "beta", "gamma"):
            d = self.accounts_dir / f"account-{name}"
            d.mkdir(parents=True)
            (d / ".credentials.json").write_text(json.dumps(_creds(f"rt-{name}")))
            (d / ".claude.json").write_text(json.dumps(_identity(name)))

        # Cached readings are deliberately STALE-LOW for alpha/beta — the whole
        # point is that the cached number the picker scores is NOT the truth.
        cus.write_json(self.accounts_dir / "state.json", {
            "active": "gamma",
            "accounts": {
                "alpha": {"next_swap_at_pct": 90, "current_5h_pct": 10.0, "current_7d_pct": 10.0},
                "beta": {"next_swap_at_pct": 90, "current_5h_pct": 20.0, "current_7d_pct": 15.0},
                "gamma": {"next_swap_at_pct": 90, "current_5h_pct": 5.0, "current_7d_pct": 5.0},
            },
            "swap_history": [],
        })

        self._saved = {k: getattr(cus, k) for k in
                       ("HOME", "CLAUDE_DIR", "CLAUDE_JSON", "CREDS_JSON", "ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.HOME = root
        cus.CLAUDE_DIR = self.claude_dir
        cus.CLAUDE_JSON = self.claude_json
        cus.CREDS_JSON = self.claude_dir / ".credentials.json"
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.accounts_dir / "state.json"
        cus.CONFIG_YAML = self.accounts_dir / "config.yaml"
        self._saved_mount_pids = cus.mount_pids
        cus.mount_pids = lambda mount: []  # nothing live → no #104 blocks
        self._saved_poll = cus.poll_account_usage
        self._saved_env = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        cus._OCCUPIED_SLOTS_CACHE.clear()

        # Per-account FRESH readings the verify loop will observe; the poll mock
        # records every account it was asked to poll so tests can assert the
        # loop is bounded / did exactly one verify poll on the happy path.
        self.fresh: dict[str, "cus.AccountUsage"] = {}
        self.poll_calls: list[str] = []

        def _fake_poll(name: str) -> "cus.AccountUsage":
            self.poll_calls.append(name)
            return self.fresh.get(name, _usage(0.0, 0.0))

        cus.poll_account_usage = _fake_poll

    def restore(self) -> None:
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus.mount_pids = self._saved_mount_pids
        cus.poll_account_usage = self._saved_poll
        if self._saved_env is not None:
            os.environ["CLAUDE_CONFIG_DIR"] = self._saved_env
        else:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self._tmp.cleanup()


def test_stale_low_candidate_rejected_and_clean_chosen():
    """(a) The picker's top choice reads low in cache but FRESH-polls over the
    step → it is rejected and a genuinely-clean account is launched instead."""
    env = _Env()
    try:
        # alpha is the picker's top pick (lowest cached), but its LIVE 5h is 98%
        # (the rayi5 situation). beta is genuinely clean (30% < step 50). gamma
        # (active) is 4% but excluded on the spread pass.
        env.fresh = {
            "alpha": _usage(98.0, 12.0),
            "beta": _usage(30.0, 20.0),
            "gamma": _usage(4.0, 4.0),
        }
        state = cus.load_state()
        config = cus.load_config()
        slot_name, slot_dir, account = cus._launch_prepare("auto", state, config)

        assert account == "beta", f"stale-low alpha must be rejected, got {account}"
        assert "alpha" in env.poll_calls, "the stale-low candidate was force-polled"
        assert "beta" in env.poll_calls, "the re-pick was force-polled too"

        # The rejected candidate's fresh 98% was persisted (proves the force-poll
        # wrote through, so the daemon inherits the real number).
        st = cus.load_state()
        assert st["accounts"]["alpha"]["current_5h_pct"] == 98.0
        assert st["slots"][slot_name]["account"] == "beta"
    finally:
        env.restore()


def test_fresh_clean_candidate_launched_with_one_verify_poll():
    """(b) A candidate that is fresh-clean is launched as-is after exactly one
    verify poll — the guard adds no churn on the happy path."""
    env = _Env()
    try:
        env.fresh = {
            "alpha": _usage(12.0, 12.0),  # cached 10 → fresh 12, still clean
            "beta": _usage(60.0, 20.0),
            "gamma": _usage(4.0, 4.0),
        }
        state = cus.load_state()
        config = cus.load_config()
        _slot, _dir, account = cus._launch_prepare("auto", state, config)

        assert account == "alpha", f"clean top pick launched as-is, got {account}"
        assert env.poll_calls == ["alpha"], f"exactly one verify poll, got {env.poll_calls}"
    finally:
        env.restore()


def test_saturated_fleet_falls_back_to_least_bad_no_hard_fail():
    """(c) Every account fresh-polls over the step (genuinely saturated fleet):
    the launch does NOT hard-fail — it lands on the picker's least-bad choice."""
    env = _Env()
    try:
        env.fresh = {
            "alpha": _usage(95.0, 30.0),
            "beta": _usage(97.0, 30.0),
            "gamma": _usage(99.0, 30.0),
        }
        state = cus.load_state()
        config = cus.load_config()
        # Must not raise despite every candidate being over the step.
        _slot, _dir, account = cus._launch_prepare("auto", state, config)

        assert account in {"alpha", "beta", "gamma"}, account
        # least-bad = lowest fresh effective %, i.e. alpha (95).
        assert account == "alpha", f"least-bad should be the lowest (alpha), got {account}"
        # Bounded: at most one poll per account (no infinite loop).
        assert len(env.poll_calls) <= len(state["accounts"]), env.poll_calls
    finally:
        env.restore()


def test_verify_loop_is_bounded():
    """(d) The verify loop terminates and polls each account at most once even
    when every account is saturated (bounded-convergence guarantee)."""
    env = _Env()
    try:
        env.fresh = {
            "alpha": _usage(96.0, 40.0),
            "beta": _usage(96.0, 40.0),
            "gamma": _usage(96.0, 40.0),
        }
        state = cus.load_state()
        config = cus.load_config()
        _slot, _dir, account = cus._launch_prepare("auto", state, config)

        assert account is not None
        # Never poll the same account twice, and never exceed the account count
        # — this is what makes the loop provably non-infinite.
        assert len(env.poll_calls) == len(set(env.poll_calls)), \
            f"no account polled twice: {env.poll_calls}"
        assert len(env.poll_calls) <= len(state["accounts"]), env.poll_calls
    finally:
        env.restore()


def test_poll_error_degrades_to_cached_pick():
    """A verify poll that errors (network/stale/429) must NOT block the launch —
    the guard degrades to the pre-fix cached pick rather than hard-failing."""
    env = _Env()
    try:
        # alpha is the top pick; its verify poll errors out. The guard should
        # trust the cached pick and launch alpha anyway (degrade-to-safe).
        env.fresh = {
            "alpha": _usage(error="network boom"),
            "beta": _usage(60.0, 20.0),
            "gamma": _usage(4.0, 4.0),
        }
        state = cus.load_state()
        config = cus.load_config()
        _slot, _dir, account = cus._launch_prepare("auto", state, config)

        assert account == "alpha", f"poll error → keep cached pick, got {account}"
        assert env.poll_calls == ["alpha"], env.poll_calls
    finally:
        env.restore()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ✓ {fn.__name__}")
    print(f"{len(fns)} passed")
