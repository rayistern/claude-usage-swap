"""Tests for GH #77 — freshness guard on the swap save-back + active-aware
re-login flow.

The outage-recovery scenario this protects: account X's refresh token dies
while X is ACTIVE. The user re-logs in — old instructions pointed at the
storage dir, so the FRESH tokens land in `account-X/.credentials.json` while
the live `~/.claude/.credentials.json` keeps the dead ones. Then:
  (a) polls keep reading the dead live file → user thinks the re-login
      failed and loops;
  (b) the next swap AWAY from X saves the dead live tokens back over the
      fresh snapshot — silently destroying the just-minted refresh token.
#72's identity guard cannot catch (b): same account, freshness (expiresAt),
not identity, is the discriminator.

The fixes under test:
  - execute_swap save-back skips (with log + inbox) when the outgoing
    snapshot's expiresAt is strictly newer than the live file's
  - _relogin_command_for branches on the ACTIVE account (bare `claude /login`
    writes the live slot) and no longer tells an inactive `default` to
    clobber the live files
  - `cus relogin <name> --finish` (finish_active_relogin) installs a
    re-logged-in snapshot into the live files, refusing non-active accounts
    and older-than-live snapshots
  - poll/SOS surface "snapshot fresher — run --finish" instead of the
    generic re-login advice

Run standalone:  python3 tests/test_relogin_freshness.py
Or under pytest: pytest tests/test_relogin_freshness.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402

# expiresAt values (Unix ms): DEAD is long past, FRESH is far future.
DEAD_MS = 1_000_000_000_000       # 2001 — hopelessly expired
FRESH_MS = 9_999_999_999_999      # 2286 — very fresh


def _creds(refresh: str, access: str, expires_at: int = FRESH_MS) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": expires_at,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


class _Env:
    """Throwaway on-disk account tree with every cus path constant repointed
    at it (same pattern as the other test files)."""

    def __init__(self, accounts: dict[str, dict], active: str, live_creds: dict | bytes):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        self.claude_dir = claude_dir
        self.creds_json = claude_dir / ".credentials.json"
        raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
        self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "userID": f"uid-{active}", "oauthAccount": {"emailAddress": f"{active}@x"},
        }))

        self.accounts_dir = accounts_dir
        for name, creds in accounts.items():
            d = accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps({
                "userID": f"uid-{name}", "oauthAccount": {"emailAddress": f"{name}@x"},
            }))

        self.state_json = accounts_dir / "state.json"
        self.state_json.write_text(json.dumps({
            "active": active,
            "accounts": {n: {"next_swap_at_pct": 50} for n in accounts},
            "swap_history": [],
        }))
        self.inbox_md = accounts_dir / "inbox.md"

        self._saved = {k: getattr(cus, k) for k in (
            "ACCOUNTS_DIR", "STATE_JSON", "CREDS_JSON", "CLAUDE_JSON",
            "CONFIG_YAML", "INBOX_MD", "migrate_account_dir",
        )}
        cus.ACCOUNTS_DIR = accounts_dir
        cus.STATE_JSON = self.state_json
        cus.CREDS_JSON = self.creds_json
        cus.CLAUDE_JSON = self.claude_json
        cus.CONFIG_YAML = accounts_dir / "config.yaml"   # absent → pure defaults
        cus.INBOX_MD = self.inbox_md
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def snapshot(self, name: str) -> dict:
        return json.loads((self.accounts_dir / f"account-{name}" / ".credentials.json").read_text())

    def live(self) -> dict:
        return json.loads(self.creds_json.read_text())

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


# ---------------------------------------------------------------------------
# The GH #77 outage-recovery scenario, end to end
# ---------------------------------------------------------------------------

def test_outage_recovery_saveback_does_not_destroy_fresh_relogin():
    """THE #77 scenario. Active account a: dead live tokens, re-logged-in
    snapshot (rotated refresh token + future expiresAt). The rotation makes
    #72's classifier say 'unknown' → historical save-back would clobber the
    fresh snapshot with the dead live tokens. The freshness guard must skip."""
    env = _Env(
        {"a": _creds("rt-a-RELOGIN", "at-a-fresh", expires_at=FRESH_MS),
         "b": _creds("rt-b", "at-b", expires_at=FRESH_MS)},
        active="a",
        live_creds=_creds("rt-a-DEAD", "at-a-dead", expires_at=DEAD_MS),
    )
    try:
        cus.execute_swap("b")
        # a's fresh re-login survives — refresh token AND expiry untouched
        snap = env.snapshot("a")["claudeAiOauth"]
        assert snap["refreshToken"] == "rt-a-RELOGIN"
        assert snap["expiresAt"] == FRESH_MS
        # swap itself proceeded: b installed live, state updated
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-b"
        assert json.loads(env.state_json.read_text())["active"] == "b"
        # and the skip was surfaced for the operator
        inbox = env.inbox_md.read_text()
        assert "freshness" in inbox.lower()
        assert "--finish" in inbox or "relogin" in inbox
    finally:
        env.restore()


def test_freshness_guard_applies_to_expected_verdict_too():
    """Same refresh token in live and snapshot (no rotation) but the snapshot
    carries a newer expiresAt — an out-of-band refresh wrote to the snapshot.
    Identity says 'expected'; freshness must still veto the save-back."""
    env = _Env(
        {"a": _creds("rt-a", "at-a-newer", expires_at=FRESH_MS),
         "b": _creds("rt-b", "at-b")},
        active="a",
        live_creds=_creds("rt-a", "at-a-older", expires_at=DEAD_MS),
    )
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-newer"
    finally:
        env.restore()


def test_normal_saveback_unaffected_when_live_is_fresher():
    """Regression: the everyday case — live tokens refreshed during use, so
    live expiresAt > snapshot's — must still save back (that IS the point of
    the save-back)."""
    env = _Env(
        {"a": _creds("rt-a", "at-a-old", expires_at=DEAD_MS),
         "b": _creds("rt-b", "at-b")},
        active="a",
        live_creds=_creds("rt-a", "at-a-REFRESHED", expires_at=FRESH_MS),
    )
    try:
        cus.execute_swap("b")
        snap = env.snapshot("a")["claudeAiOauth"]
        assert snap["accessToken"] == "at-a-REFRESHED"
        assert snap["expiresAt"] == FRESH_MS
    finally:
        env.restore()


def test_missing_expiresat_falls_through_to_saveback():
    """No comparable expiresAt on the snapshot → the guard must NOT block
    the historical save-back (missing metadata is not evidence of freshness)."""
    snap_a = _creds("rt-a", "at-a-old")
    del snap_a["claudeAiOauth"]["expiresAt"]
    env = _Env({"a": snap_a, "b": _creds("rt-b", "at-b")},
               active="a",
               live_creds=_creds("rt-a", "at-a-live", expires_at=DEAD_MS))
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-live"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# _relogin_command_for — active-aware target file
# ---------------------------------------------------------------------------

def test_relogin_command_active_account_targets_live_files():
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        assert cus._relogin_command_for("a") == "claude /login"
    finally:
        env.restore()


def test_relogin_command_inactive_account_targets_storage_dir():
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        cmd = cus._relogin_command_for("b")
        assert "CLAUDE_CONFIG_DIR=" in cmd and "account-b" in cmd
    finally:
        env.restore()


def test_relogin_command_inactive_default_no_longer_clobbers_live():
    """The mirror-image bug from the issue: bare `claude /login` for an
    inactive `default` writes the LIVE file — i.e. the ACTIVE account's
    tokens. Must now point at account-default's storage dir instead."""
    env = _Env({"default": _creds("rt-d", "at-d"), "b": _creds("rt-b", "at-b")},
               active="b", live_creds=_creds("rt-b", "at-b"))
    try:
        cmd = cus._relogin_command_for("default")
        assert cmd != "claude /login"
        assert "CLAUDE_CONFIG_DIR=" in cmd and "account-default" in cmd
        # ...while an ACTIVE default still gets the bare live login
        st = json.loads(env.state_json.read_text())
        st["active"] = "default"
        env.state_json.write_text(json.dumps(st))
        assert cus._relogin_command_for("default") == "claude /login"
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# finish_active_relogin (`cus relogin <name> --finish`)
# ---------------------------------------------------------------------------

def test_finish_installs_fresh_snapshot_live():
    """Round trip: dead live tokens, fresh re-logged snapshot → --finish
    installs snapshot creds live (backing up the old live file), and merges
    the account-bound identity keys."""
    env = _Env(
        {"a": _creds("rt-a-RELOGIN", "at-a-fresh", expires_at=FRESH_MS),
         "b": _creds("rt-b", "at-b")},
        active="a",
        live_creds=_creds("rt-a-DEAD", "at-a-dead", expires_at=DEAD_MS),
    )
    # Give the snapshot identity a field the live file lacks, to see the merge.
    snap_cj = env.accounts_dir / "account-a" / ".claude.json"
    snap_cj.write_text(json.dumps({
        "userID": "uid-a", "oauthAccount": {"emailAddress": "a@x", "accountUuid": "uuid-a-new"},
    }))
    try:
        cus.finish_active_relogin("a")
        live = env.live()["claudeAiOauth"]
        assert live["refreshToken"] == "rt-a-RELOGIN"
        assert live["expiresAt"] == FRESH_MS
        # old live content preserved as a GH #79 backup generation
        baks = sorted(env.claude_dir.glob(".credentials.json.bak.*"))
        assert any("rt-a-DEAD" in b.read_text() for b in baks)
        # identity keys carried over
        live_cj = json.loads(env.claude_json.read_text())
        assert live_cj["oauthAccount"]["accountUuid"] == "uuid-a-new"
    finally:
        env.restore()


def test_finish_refuses_inactive_account():
    """Installing an inactive account's snapshot live would clobber the
    ACTIVE account's tokens — exactly the #70/#77 damage class."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        try:
            cus.finish_active_relogin("b")
            raise AssertionError("expected RuntimeError for inactive account")
        except RuntimeError as e:
            assert "not the active account" in str(e)
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-a"   # untouched
    finally:
        env.restore()


def test_finish_refuses_snapshot_older_than_live():
    """--finish must never replace working live tokens with deader ones."""
    env = _Env(
        {"a": _creds("rt-a-old", "at-a-old", expires_at=DEAD_MS),
         "b": _creds("rt-b", "at-b")},
        active="a",
        live_creds=_creds("rt-a-live", "at-a-live", expires_at=FRESH_MS),
    )
    try:
        try:
            cus.finish_active_relogin("a")
            raise AssertionError("expected RuntimeError for older snapshot")
        except RuntimeError as e:
            assert "OLDER" in str(e)
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-a-live"   # untouched
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# _snapshot_fresher_than_live + the poll-side hint
# ---------------------------------------------------------------------------

def test_snapshot_fresher_detection():
    env = _Env(
        {"a": _creds("rt-a-RELOGIN", "at-a-fresh", expires_at=FRESH_MS),
         "b": _creds("rt-b", "at-b", expires_at=DEAD_MS)},
        active="a",
        live_creds=_creds("rt-a-DEAD", "at-a-dead", expires_at=DEAD_MS),
    )
    try:
        assert cus._snapshot_fresher_than_live("a") is True
        # b's snapshot is older than the live file → not "fresher"
        assert cus._snapshot_fresher_than_live("b") is False
    finally:
        env.restore()


def test_poll_hints_finish_when_active_snapshot_is_fresher():
    """poll on the active account reads the dead LIVE file → token_expired;
    with a fresher snapshot present, the advice must be `--finish`, not
    another browser re-login."""
    from click.testing import CliRunner

    env = _Env(
        {"a": _creds("rt-a-RELOGIN", "at-a-fresh", expires_at=FRESH_MS)},
        active="a",
        live_creds=_creds("rt-a-DEAD", "at-a-dead", expires_at=DEAD_MS),
    )
    saved_poll = cus.poll_account_usage
    cus.poll_account_usage = lambda name: cus.AccountUsage(token_expired=True)
    try:
        result = CliRunner().invoke(cus.cli, ["poll", "--account", "a"], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "--finish" in result.output
        assert "FRESHER" in result.output
    finally:
        cus.poll_account_usage = saved_poll
        env.restore()


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
