"""Tests for clock_keepalive (2026-07-07 operator directive) — keep every
account's fixed 5-hour usage window TICKING by pinging DORMANT accounts.

Mechanic: Claude's 5h window is a FIXED window that starts on an account's first
model request after a dormant period and resets ~5h later. An idle account
(nothing exercising it) has a DORMANT clock — its window isn't running, so no
"fresh in ~5h" capacity is coming. `_sweep_clock_keepalive` sends a cheap "hi"
ping to each dormant, eligible account to (re)start its window, cooldown-bounded
and skipping live/disabled/backoff/relogin/dead-creds accounts.

The HTTP ping (`_keepalive_ping`) is MOCKED here — these tests make NO real
network calls.

Run standalone:  python3 tests/test_clock_keepalive.py
Run under pytest: pytest tests/test_clock_keepalive.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _snap(access: str = "at-fresh", refresh: str = "rt-good",
          expires_at: int | None = None) -> dict:
    """Account snapshot with a NON-expired access token (fresh far into the
    future) so `_keepalive_token_for` uses it directly without a refresh grant."""
    if expires_at is None:
        expires_at = int((datetime.now(timezone.utc).timestamp() + 3600) * 1000)
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


class _Env:
    """Throwaway tree with cus's account/state/config paths repointed, plus
    click.echo capture (for CRED-AUDIT assertions) and monkeypatch-with-restore.
    Mirrors tests/test_unstale_idle_account.py::_Env."""

    def __init__(self, accounts: dict[str, dict], flags: dict[str, dict] | None = None,
                 config: dict | None = None, active: str | None = None) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.accounts_dir = root / "claude-accounts"
        self.accounts_dir.mkdir(parents=True)
        for name, creds in accounts.items():
            d = self.accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))

        flags = flags or {}
        self.state_json = self.accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active if active is not None else next(iter(accounts)),
            "accounts": {n: {"next_swap_at_pct": 50, "current_5h_pct": 0.0,
                             "current_7d_pct": 0.0, **flags.get(n, {})} for n in accounts},
            "slots": {}, "swap_history": [],
        }))
        self.config_yaml = self.accounts_dir / "config.yaml"
        cus.write_yaml(self.config_yaml, config if config is not None else {})

        self._saved = {k: getattr(cus, k) for k in ("ACCOUNTS_DIR", "STATE_JSON", "CONFIG_YAML")}
        cus.ACCOUNTS_DIR = self.accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CONFIG_YAML = self.config_yaml
        cus._reset_blank_tracking()

        self.echoes: list[str] = []
        self._saved_echo = cus.click.echo
        cus.click.echo = lambda *a, **k: self.echoes.append(
            " ".join(str(x) for x in a) if a else "")
        self._patches: list[tuple[object, str, object]] = []

    def patch(self, obj: object, name: str, value: object) -> None:
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def audit_lines(self, op: str) -> list[str]:
        return [e for e in self.echoes if e.startswith(f"{cus.CRED_AUDIT_PREFIX} op={op}")]

    def restore(self) -> None:
        for obj, name, value in reversed(self._patches):
            setattr(obj, name, value)
        cus.click.echo = self._saved_echo
        for k, v in self._saved.items():
            setattr(cus, k, v)
        cus._reset_blank_tracking()
        self._tmp.cleanup()


def _ok_usage():
    """A successful usage poll with a fresh 5h window (used for the post-ping
    re-poll so `five_hour_resets_at` updates)."""
    u = cus.AccountUsage.empty()
    reset = _iso(datetime.now(timezone.utc) + timedelta(hours=5))
    u.five_hour = cus.UsageWindow(0.0, reset)
    u.seven_day = cus.UsageWindow(5.0, None)
    u.raw = {}
    return u


class _PingSpy:
    """Records `_keepalive_ping` calls and returns a canned result."""

    def __init__(self, result=(True, "HTTP 200")):
        self.calls: list[tuple[str, str]] = []
        self.result = result

    def __call__(self, token: str, model: str):
        self.calls.append((token, model))
        return self.result


# ---------------------------------------------------------------------------
# (a) dormant enabled account past the cooldown → pinged + ts recorded
# ---------------------------------------------------------------------------

def test_a_dormant_account_is_pinged_and_ts_recorded():
    # 'active' is the shared-active account (skipped as live); 'idle' is a
    # non-active DORMANT account (no five_hour_resets_at) → the ping target.
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active")
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        env.patch(cus, "poll_account_usage", lambda name: _ok_usage())
        state = cus.load_state()
        pinged = cus._sweep_clock_keepalive(state, cus.load_config())
        assert pinged == ["idle"], pinged
        assert len(spy.calls) == 1 and spy.calls[0][0] == "at-idle"
        # cheapest-model default was used for the ping
        assert spy.calls[0][1] == "claude-haiku-4-5-20251001"
        # cooldown timestamp recorded
        assert state["accounts"]["idle"].get("last_keepalive_ping_ts")
        # post-ping re-poll updated the now-ticking window
        assert state["accounts"]["idle"].get("five_hour_resets_at")
        line = env.audit_lines("clock-keepalive-ping")
        assert any("decision=pinged" in ln and "account=idle" in ln for ln in line)
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) account within the cooldown is NOT re-pinged
# ---------------------------------------------------------------------------

def test_b_within_cooldown_not_repinged():
    recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active",
               flags={"idle": {"last_keepalive_ping_ts": recent}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        env.patch(cus, "poll_account_usage",
                  lambda name: (_ for _ in ()).throw(AssertionError("no re-poll")))
        state = cus.load_state()
        pinged = cus._sweep_clock_keepalive(state, cus.load_config())
        assert pinged == []
        assert spy.calls == []  # cooldown suppressed the ping
        assert any("decision=skipped" in ln and "cooldown" in ln
                   for ln in env.audit_lines("clock-keepalive-ping"))
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) a disabled account is skipped
# ---------------------------------------------------------------------------

def test_c_disabled_account_skipped():
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active",
               flags={"idle": {"disabled": True}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        state = cus.load_state()
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == []
        assert spy.calls == []
        assert state["accounts"]["idle"].get("last_keepalive_ping_ts") is None
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (d) an account whose 5h window is actively ticking is skipped
# ---------------------------------------------------------------------------

def test_d_live_ticking_window_skipped():
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=3))
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active",
               flags={"idle": {"five_hour_resets_at": future}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        state = cus.load_state()
        # 'idle' has a future reset → window is running → NOT dormant → skipped.
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == []
        assert spy.calls == []
        # And the pure predicate agrees.
        assert cus._5h_clock_dormant(state["accounts"]["idle"],
                                     datetime.now(timezone.utc)) is False
        # A lapsed (past) reset IS dormant.
        past = {"five_hour_resets_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1))}
        assert cus._5h_clock_dormant(past, datetime.now(timezone.utc)) is True
        assert cus._5h_clock_dormant({}, datetime.now(timezone.utc)) is True
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (e) a dead-snapshot account with no valid family is skipped (never pinged with
#     dead creds)
# ---------------------------------------------------------------------------

def test_e_dead_snapshot_no_family_skipped():
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active",
               flags={"idle": {"snapshot_refresh_dead": True}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        # Guard: no refresh grant / family claim should even be attempted for a
        # dead snapshot with no pooled login family.
        env.patch(cus, "claim_verified_login_family",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("no family to claim")))
        state = cus.load_state()
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == []
        assert spy.calls == []  # never pinged with dead creds
        assert any("decision=skipped" in ln and "refresh-dead" in ln
                   for ln in env.audit_lines("clock-keepalive-ping"))
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (f) master toggle off → sweep is a no-op
# ---------------------------------------------------------------------------

def test_f_disabled_by_config():
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active",
               config={"clock_keepalive": {"enabled": False}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        state = cus.load_state()
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == []
        assert spy.calls == []
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (g) allowlist / denylist scoping
# ---------------------------------------------------------------------------

def test_g_allowlist_and_denylist_scope():
    env = _Env({"active": _snap(), "a": _snap("at-a"), "b": _snap("at-b")}, active="active",
               config={"clock_keepalive": {"accounts": ["a"]}})
    try:
        spy = _PingSpy()
        env.patch(cus, "_keepalive_ping", spy)
        env.patch(cus, "poll_account_usage", lambda name: _ok_usage())
        state = cus.load_state()
        # allowlist ['a'] → only 'a' eligible ('b' excluded by omission).
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == ["a"]
        assert [c[0] for c in spy.calls] == ["at-a"]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (h) a failed ping only logs — no crash, no timestamp recorded, no re-poll
# ---------------------------------------------------------------------------

def test_h_failed_ping_logs_and_moves_on():
    env = _Env({"active": _snap(), "idle": _snap("at-idle")}, active="active")
    try:
        spy = _PingSpy(result=(False, "HTTP 500: boom"))
        env.patch(cus, "_keepalive_ping", spy)
        env.patch(cus, "poll_account_usage",
                  lambda name: (_ for _ in ()).throw(AssertionError("no re-poll on failure")))
        state = cus.load_state()
        assert cus._sweep_clock_keepalive(state, cus.load_config()) == []
        assert len(spy.calls) == 1  # attempted
        assert state["accounts"]["idle"].get("last_keepalive_ping_ts") is None
        assert any("decision=failed" in ln for ln in env.audit_lines("clock-keepalive-ping"))
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
