"""Tests for GH #3 — refresh-time drift detection on the swap save-back.

The bug: a drifted pane (in-memory tokens = account X, machine-active =
account Y) refreshes its access token and writes X's tokens over the live
`~/.claude/.credentials.json`. The next swap's save-back then copied the live
file into Y's storage snapshot, clobbering Y's refresh token with X's — and
the corruption compounded on every subsequent swap.

The fix (execute_swap + classify_live_creds_owner): before saving the live
file back into the outgoing account's snapshot, identify its true owner by
refresh-token lineage. On a definite foreign match, skip the save-back and
route the tokens to their real owner's snapshot instead. The live bytes are
read once and written as-is (no re-read between check and write) to close the
read-check-write race with a concurrently-refreshing pane.

Run standalone:  python3 tests/test_creds_saveback_drift.py
Or under pytest: pytest tests/test_creds_saveback_drift.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cus  # noqa: E402


def _creds(refresh: str, access: str) -> dict:
    return {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": 9999999999999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
    }}


class _Env:
    """A throwaway on-disk account tree with every cus path constant
    repointed at it, so execute_swap runs for real (files, state.json, inbox)
    without touching the live machine. Plain setattr + restore(), matching the
    other test files' style so this file also runs standalone."""

    def __init__(self, accounts: dict[str, dict], active: str, live_creds: dict | bytes):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        # Live files
        self.creds_json = claude_dir / ".credentials.json"
        raw = live_creds if isinstance(live_creds, bytes) else json.dumps(live_creds).encode()
        self.creds_json.write_bytes(raw)
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps({
            "userID": f"uid-{active}", "oauthAccount": {"emailAddress": f"{active}@x"},
        }))

        # Per-account snapshots (post-migration layout)
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
        # The tree above is already in post-migration layout; the real
        # migrate_account_dir would try to symlink shared dirs into the live
        # ~/.claude, which is exactly what tests must not touch.
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
# classify_live_creds_owner unit tests (pure identification logic)
# ---------------------------------------------------------------------------

def test_classify_matches_expected_account():
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a-refreshed"))
    try:
        verdict, owner, _ = cus.classify_live_creds_owner(env.live(), "a", json.loads(env.state_json.read_text()))
        assert (verdict, owner) == ("expected", "a")
    finally:
        env.restore()


def test_classify_detects_foreign_owner():
    """Live file holds b's lineage while a is nominally active → foreign."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-b", "at-b-refreshed"))
    try:
        verdict, owner, detail = cus.classify_live_creds_owner(env.live(), "a", json.loads(env.state_json.read_text()))
        assert (verdict, owner) == ("foreign", "b")
        assert "drifted pane" in detail
    finally:
        env.restore()


def test_classify_unknown_when_rotated():
    """A rotated refresh token matches no snapshot → unknown (NOT foreign):
    punishing legitimate rotation would strand the active account's new
    refresh token, which is worse than the drift it tries to prevent."""
    env = _Env({"a": _creds("rt-a", "at-a")}, active="a",
               live_creds=_creds("rt-a-ROTATED", "at-a-new"))
    try:
        verdict, owner, _ = cus.classify_live_creds_owner(env.live(), "a", json.loads(env.state_json.read_text()))
        assert (verdict, owner) == ("unknown", None)
    finally:
        env.restore()


def test_classify_conflict_on_duplicate_identity():
    """Two snapshots sharing one refresh token (GH #70 duplicate-identity
    setup) → ownership is a guess → conflict."""
    env = _Env({"a": _creds("rt-dup", "at-a"), "b": _creds("rt-dup", "at-b")},
               active="a", live_creds=_creds("rt-dup", "at-live"))
    try:
        verdict, owner, detail = cus.classify_live_creds_owner(env.live(), "a", json.loads(env.state_json.read_text()))
        assert (verdict, owner) == ("conflict", None)
        assert "multiple" in detail
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# execute_swap integration tests (real files in a temp tree)
# ---------------------------------------------------------------------------

def test_normal_saveback_persists_refreshed_access_token():
    """No drift: live file is a's lineage with a refreshed access token →
    save-back updates a's snapshot (the whole point of the save-back)."""
    env = _Env({"a": _creds("rt-a", "at-a-old"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a-REFRESHED"))
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-REFRESHED"
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-b"   # b installed
        assert json.loads(env.state_json.read_text())["active"] == "b"
    finally:
        env.restore()


def test_drift_does_not_clobber_current_snapshot():
    """THE GH #3 fix. Drifted pane on b overwrote live creds while a was
    active. Swapping a→b must NOT write b's tokens into a's snapshot."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b-old")},
               active="a", live_creds=_creds("rt-b", "at-b-REFRESHED"))
    try:
        cus.execute_swap("b")
        # a's snapshot untouched — its refresh token survives the drift
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a"
        # b's snapshot healed with the fresher access token from the live file
        assert env.snapshot("b")["claudeAiOauth"]["accessToken"] == "at-b-REFRESHED"
        # and the installed live file carries the healed b tokens
        assert env.live()["claudeAiOauth"]["accessToken"] == "at-b-REFRESHED"
    finally:
        env.restore()


def test_drift_logged_to_inbox():
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-b", "at-b-refreshed"))
    try:
        cus.execute_swap("b")
        inbox = env.inbox_md.read_text()
        assert "drift" in inbox
        assert "'b'" in inbox        # names the true owner
    finally:
        env.restore()


def test_drift_to_third_account():
    """Three accounts: c's tokens drifted onto the live file while a was
    active, and the swap goes a→b. a untouched, c healed, b installed."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b"),
                "c": _creds("rt-c", "at-c-old")},
               active="a", live_creds=_creds("rt-c", "at-c-REFRESHED"))
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert env.snapshot("c")["claudeAiOauth"]["accessToken"] == "at-c-REFRESHED"
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-b"
    finally:
        env.restore()


def test_rotated_token_still_saved_back():
    """Unknown lineage (rotation) keeps the historical save-back — otherwise
    every legitimate refresh-token rotation would be stranded on the live
    file and lost at the next swap."""
    env = _Env({"a": _creds("rt-a-old", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a-ROTATED", "at-a-new"))
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a-ROTATED"
    finally:
        env.restore()


def test_duplicate_identity_skips_saveback_entirely():
    """Conflict: both snapshots share the live refresh token → no write at
    all; both snapshots keep their prior bytes."""
    env = _Env({"a": _creds("rt-dup", "at-a-old"), "b": _creds("rt-dup", "at-b-old")},
               active="a", live_creds=_creds("rt-dup", "at-live-new"))
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-old"
        assert env.snapshot("b")["claudeAiOauth"]["accessToken"] == "at-b-old"
    finally:
        env.restore()


def test_unparseable_live_creds_do_not_destroy_snapshot():
    """Pre-fix, a half-written/corrupt live file was byte-copied over the
    outgoing snapshot. Now the save-back is skipped and the swap proceeds,
    which also rewrites the corrupt live file with the target's good one."""
    env = _Env({"a": _creds("rt-a", "at-a-good"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=b"{ this is not json")
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-good"
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-b"   # repaired
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# Review amendment 2026-07-01 — valid-JSON garbage in the live file
# (the original PR only guarded UNPARSEABLE JSON; these pin the parseable-
# but-tokenless / non-object cases, which used to destroy the snapshot or
# crash the swap with AttributeError)
# ---------------------------------------------------------------------------

def test_classify_invalid_when_no_refresh_token():
    """A live file with no claudeAiOauth.refreshToken has nothing durable to
    save; it must classify 'invalid' (skip), never 'unknown' (save-back)."""
    env = _Env({"a": _creds("rt-a", "at-a")}, active="a",
               live_creds={"claudeAiOauth": {"accessToken": "at-only"}})
    try:
        state = json.loads(env.state_json.read_text())
        for live in ({}, {"claudeAiOauth": {"accessToken": "at-only"}},
                     {"claudeAiOauth": "bogus-not-a-dict"}, ["not", "a", "dict"], "just a string", 42):
            verdict, owner, _ = cus.classify_live_creds_owner(live, "a", state)
            assert (verdict, owner) == ("invalid", None), f"live={live!r} -> {(verdict, owner)}"
    finally:
        env.restore()


def test_tokenless_live_creds_do_not_destroy_snapshot():
    """`claude logout` in a pane rewrites the live file without oauth tokens.
    Saving that back would erase the outgoing account's refresh token — the
    save-back must be skipped, exactly like the unparseable-JSON case."""
    env = _Env({"a": _creds("rt-a", "at-a-good"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds={"claudeAiOauth": {"accessToken": "at-x", "expiresAt": 1}})
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert env.snapshot("a")["claudeAiOauth"]["accessToken"] == "at-a-good"
        assert env.live()["claudeAiOauth"]["refreshToken"] == "rt-b"   # target installed
    finally:
        env.restore()


def test_empty_object_live_creds_do_not_destroy_snapshot():
    """Valid JSON `{}` is still garbage from a save-back perspective."""
    env = _Env({"a": _creds("rt-a", "at-a-good"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=b"{}")
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a"
    finally:
        env.restore()


def test_nondict_live_json_does_not_crash_swap():
    """json.loads can return a list/string/number; the swap must classify it
    'invalid' and proceed, not die with AttributeError (which no caller
    catches — the daemon loop would crash)."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=b'["not", "a", "dict"]')
    try:
        cus.execute_swap("b")
        assert env.snapshot("a")["claudeAiOauth"]["refreshToken"] == "rt-a"
        assert json.loads(env.state_json.read_text())["active"] == "b"
    finally:
        env.restore()


def test_nondict_snapshot_cannot_crash_or_misvote_classification():
    """A snapshot holding a JSON list must be treated like a corrupt snapshot
    (no vote), not AttributeError the classification."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a-refreshed"))
    try:
        (env.accounts_dir / "account-b" / ".credentials.json").write_bytes(b"[1, 2]")
        verdict, owner, _ = cus.classify_live_creds_owner(
            env.live(), "a", json.loads(env.state_json.read_text()))
        assert (verdict, owner) == ("expected", "a")
    finally:
        env.restore()


def test_missing_live_creds_raises_filenotfound():
    """Callers (switch, daemon) catch FileNotFoundError specifically — the
    guard must keep raising the same exception type the old atomic_copy did."""
    env = _Env({"a": _creds("rt-a", "at-a"), "b": _creds("rt-b", "at-b")},
               active="a", live_creds=_creds("rt-a", "at-a"))
    try:
        env.creds_json.unlink()
        try:
            cus.execute_swap("b")
            raise AssertionError("expected FileNotFoundError")
        except FileNotFoundError:
            pass
    finally:
        env.restore()


def _run_all() -> int:
    import types
    tests = [v for k, v in globals().items()
             if k.startswith("test_") and isinstance(v, types.FunctionType)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
