"""Tests for Fix 4 (2026-07-07) — un-stale idle `token_stale` accounts via a
direct OAuth refresh grant.

Bug (verified live): `cus force-poll <acct>` does NOT clear `token_stale`. An idle
account's stored access token expires because nothing exercises it, so it sits
`token_stale` forever even though its refresh token is still valid — causing
(a) chronically wrong cus readings and (b) "stale snapshot → blank on install"
when the daemon swaps a lane onto it. `_unstale_account_snapshot` mints a fresh
access token via `_oauth_refresh_grant` (#127 — no session, no billed tokens),
writes it back to the account SNAPSHOT, and clears `token_stale`. A DEAD refresh
token is left flagged (needs a browser relogin) and NOT retried in a tight loop.

Run standalone:  python3 tests/test_unstale_idle_account.py
Run under pytest: pytest tests/test_unstale_idle_account.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _snap(access: str = "at-old", refresh: str = "rt-good",
          expires_at: int = 1_000) -> dict:
    """Account snapshot with an EXPIRED access token but a valid refresh token —
    the idle token_stale shape."""
    return {"claudeAiOauth": {"accessToken": access, "refreshToken": refresh,
                              "expiresAt": expires_at}}


class _Env:
    """Throwaway tree with cus's account/state/config paths repointed, plus
    click.echo capture (for CRED-AUDIT assertions) and monkeypatch-with-restore."""

    def __init__(self, accounts: dict[str, dict], flags: dict[str, dict] | None = None,
                 config: dict | None = None) -> None:
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
            "active": next(iter(accounts)),
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

    def snap_creds(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".credentials.json").read_text())

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


def _alive(access="at-new", refresh="rt-new", expires_in=3600):
    return ("alive", {"access_token": access, "refresh_token": refresh, "expires_in": expires_in})


# ---------------------------------------------------------------------------
# (a) alive refresh token → snapshot refreshed + token_stale cleared
# ---------------------------------------------------------------------------

def test_a_unstale_clears_flag_and_writes_snapshot():
    env = _Env({"merkos": _snap("at-old", "rt-good")}, flags={"merkos": {"token_stale": True}})
    try:
        env.patch(cus, "_oauth_refresh_grant", lambda rt: _alive("at-fresh", "rt-fresh"))
        state = cus.load_state()
        assert cus._unstale_account_snapshot("merkos", state, cus.load_config()) is True
        # Flag cleared in state.
        assert state["accounts"]["merkos"].get("token_stale") in (None, False)
        # Snapshot rewritten with the fresh access + rotated refresh token.
        oauth = env.snap_creds("merkos")["claudeAiOauth"]
        assert oauth["accessToken"] == "at-fresh" and oauth["refreshToken"] == "rt-fresh"
        assert oauth["expiresAt"] > 1_000  # bumped forward
        lines = env.audit_lines("unstale-refresh")
        assert len(lines) == 1 and "decision=refreshed" in lines[0] and "account=merkos" in lines[0]
    finally:
        env.restore()


def test_a_ignores_non_stale_and_expired_accounts():
    env = _Env({"a": _snap(), "b": _snap()},
               flags={"a": {}, "b": {"token_stale": True, "token_expired": True}})
    try:
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: (_ for _ in ()).throw(AssertionError("must not grant")))
        state = cus.load_state()
        # 'a' is not stale at all; 'b' is hard-expired → neither is a refresh case.
        assert cus._unstale_account_snapshot("a", state, cus.load_config()) is False
        assert cus._unstale_account_snapshot("b", state, cus.load_config()) is False
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (b) dead refresh token → stays flagged + not retried in a tight loop
# ---------------------------------------------------------------------------

def test_b_dead_refresh_token_stays_flagged_and_respects_cooldown():
    env = _Env({"merkos": _snap("at-old", "rt-dead")}, flags={"merkos": {"token_stale": True}})
    try:
        calls = {"n": 0}

        def dead_grant(rt):
            calls["n"] += 1
            return ("dead", None)

        env.patch(cus, "_oauth_refresh_grant", dead_grant)
        state = cus.load_state()
        assert cus._unstale_account_snapshot("merkos", state, cus.load_config()) is False
        # Still flagged (genuinely needs a browser relogin).
        assert state["accounts"]["merkos"]["token_stale"] is True
        # Poll-burnout backoff: an immediate second (non-forced) call is suppressed
        # → the grant endpoint is hit only ONCE within the cooldown.
        assert cus._unstale_account_snapshot("merkos", state, cus.load_config()) is False
        assert calls["n"] == 1
        # ... but force=True (operator force-poll) bypasses the cooldown.
        assert cus._unstale_account_snapshot("merkos", state, cus.load_config(), force=True) is False
        assert calls["n"] == 2
        assert "decision=failed" in env.audit_lines("unstale-refresh")[0]
    finally:
        env.restore()


def test_b_no_refresh_token_in_snapshot_fails():
    env = _Env({"merkos": {"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 1}}},
               flags={"merkos": {"token_stale": True}})
    try:
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: (_ for _ in ()).throw(AssertionError("no rt → no grant")))
        state = cus.load_state()
        assert cus._unstale_account_snapshot("merkos", state, cus.load_config()) is False
        assert "decision=failed" in env.audit_lines("unstale-refresh")[0]
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (c) daemon sweep + force-poll integration
# ---------------------------------------------------------------------------

def test_c_sweep_unstales_only_recoverable_accounts():
    env = _Env({"stale": _snap("at-s", "rt-good"),
                "expired": _snap("at-e", "rt-x"),
                "healthy": _snap("at-h", "rt-h")},
               flags={"stale": {"token_stale": True},
                      "expired": {"token_stale": True, "token_expired": True},
                      "healthy": {}})
    try:
        env.patch(cus, "_oauth_refresh_grant", lambda rt: _alive())
        state = cus.load_state()
        unstaled = cus._sweep_unstale_idle_accounts(state, cus.load_config())
        assert unstaled == ["stale"]  # only the recoverable idle account
        assert state["accounts"]["stale"].get("token_stale") in (None, False)
        assert state["accounts"]["expired"]["token_stale"] is True  # untouched
    finally:
        env.restore()


def test_c_sweep_disabled_by_config():
    env = _Env({"stale": _snap("at-s", "rt-good")}, flags={"stale": {"token_stale": True}},
               config={"token_self_refresh": {"unstale_idle_on_poll": False}})
    try:
        env.patch(cus, "_oauth_refresh_grant",
                  lambda rt: (_ for _ in ()).throw(AssertionError("disabled")))
        state = cus.load_state()
        assert cus._sweep_unstale_idle_accounts(state, cus.load_config()) == []
    finally:
        env.restore()


def test_c_force_poll_unstales_and_repolls():
    from click.testing import CliRunner

    env = _Env({"merkos": _snap("at-old", "rt-good")}, flags={"merkos": {"token_stale": True}})
    try:
        env.patch(cus, "_oauth_refresh_grant", lambda rt: _alive("at-fresh", "rt-fresh"))

        # First poll returns token_stale; after the un-stale, the re-poll succeeds.
        def stale_u():
            u = cus.AccountUsage.empty()
            u.token_stale = True
            u.raw = {"expired_minutes_ago": 7}
            return u

        def ok_u():
            u = cus.AccountUsage.empty()
            u.five_hour = cus.UsageWindow(12.0, None)
            u.seven_day = cus.UsageWindow(21.0, None)
            u.raw = {}
            return u

        seq = iter([stale_u(), ok_u()])
        env.patch(cus, "poll_account_usage", lambda name: next(seq))

        # CliRunner needs a real echo; swap back the captured echo for the invoke.
        real_echo = env._saved_echo
        env.patch(cus.click, "echo", real_echo)

        result = CliRunner().invoke(cus.cli, ["force-poll", "merkos"])
        assert result.exit_code == 0, result.output
        assert "UN-STALED merkos" in result.output
        # State flag cleared and snapshot updated with the fresh token.
        state = cus.load_state()
        assert state["accounts"]["merkos"].get("token_stale") in (None, False)
        assert env.snap_creds("merkos")["claudeAiOauth"]["accessToken"] == "at-fresh"
    finally:
        env.restore()


def test_c_force_poll_dead_token_keeps_stale():
    from click.testing import CliRunner

    env = _Env({"merkos": _snap("at-old", "rt-dead")}, flags={"merkos": {"token_stale": True}})
    try:
        env.patch(cus, "_oauth_refresh_grant", lambda rt: ("dead", None))

        def stale_u():
            u = cus.AccountUsage.empty()
            u.token_stale = True
            u.raw = {"expired_minutes_ago": 7}
            return u

        env.patch(cus, "poll_account_usage", lambda name: stale_u())
        env.patch(cus.click, "echo", env._saved_echo)

        result = CliRunner().invoke(cus.cli, ["force-poll", "merkos"])
        assert result.exit_code == 0, result.output
        assert "TOKEN_STALE" in result.output and "UN-STALED" not in result.output
        assert cus.load_state()["accounts"]["merkos"]["token_stale"] is True
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
