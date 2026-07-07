"""Tests for the structured CRED-AUDIT log trail (make a clobber one grep away).

WHY THIS EXISTS: the account-rayi1 clobber (verified from daemon.log) reconstructed
only across scattered, differently-shaped human lines. The CRED-AUDIT contract adds
exactly ONE structured `CRED-AUDIT ...` line at every credential-store write/refuse
point (alongside — never instead of — the human-readable line), so every store
mutation the daemon ever attempted is greppable, and a mismatch between
`incoming_identity=` and `existing_identity=` (the clobber itself) is visible at a
glance. See the CRED-AUDIT schema block in cus.py.

These tests are OBSERVABILITY-ONLY assertions: they prove the audit lines are emitted
with the right op=/decision= fields on the two load-bearing paths — a successful
save-back and a refused wrong-account write. They deliberately do NOT re-assert the
guard's control-flow behavior (that is covered by test_wrong_account_clobber_guard.py);
CRED-AUDIT must be purely additive.

Run standalone:  python3 tests/test_cred_audit_logging.py
Or under pytest: pytest tests/test_cred_audit_logging.py
"""

import contextlib
import io
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


def _cj(uuid: str, email: str) -> dict:
    return {"userID": f"uid-{email}", "oauthAccount": {"accountUuid": uuid, "emailAddress": email}}


class _Env:
    """Throwaway on-disk account tree with cus's path constants repointed at it —
    a trimmed copy of test_wrong_account_clobber_guard._Env (meta.yaml + login
    family scaffolding included, since the anchor sources drive the guard the
    refused-write test exercises)."""

    def __init__(self, accounts: dict[str, dict], active: str, live_creds: dict,
                 live_identity: dict | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        accounts_dir = root / "claude-accounts"
        claude_dir = root / ".claude"
        claude_dir.mkdir(parents=True)
        accounts_dir.mkdir(parents=True)

        self.creds_json = claude_dir / ".credentials.json"
        self.creds_json.write_bytes(json.dumps(live_creds).encode())
        self.claude_json = root / ".claude.json"
        self.claude_json.write_text(json.dumps(live_identity or _cj(f"uuid-{active}", f"{active}@x")))

        self.accounts_dir = accounts_dir
        for name, creds in accounts.items():
            d = accounts_dir / f"account-{name}"
            d.mkdir()
            (d / ".credentials.json").write_text(json.dumps(creds))
            (d / ".claude.json").write_text(json.dumps(_cj(f"uuid-{name}", f"{name}@x")))

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
        cus.CONFIG_YAML = accounts_dir / "config.yaml"
        cus.INBOX_MD = self.inbox_md
        cus.migrate_account_dir = lambda d: {"action": "already_migrated"}

    def write_meta(self, account: str, uuid: str, email: str) -> None:
        d = self.accounts_dir / f"account-{account}"
        cus.write_yaml(d / "meta.yaml", {
            "name": account, "oauth_account_uuid": uuid, "oauth_email": email,
            "priority": 1, "locked_sessions": [],
        })

    def add_family(self, account: str, fam: str, uuid: str, email: str,
                   refresh: str = "rt-fam") -> None:
        fdir = cus.login_family_dir(account, fam)
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / ".claude.json").write_text(json.dumps(_cj(uuid, email)))
        (fdir / ".credentials.json").write_text(json.dumps(_creds(refresh, "at-fam")))

    def restore(self):
        for k, v in self._saved.items():
            setattr(cus, k, v)
        self._tmp.cleanup()


def _audit_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith("CRED-AUDIT")]


def _field(line: str, key: str) -> str | None:
    """Extract a `key=value` field from a CRED-AUDIT line (value is the token up
    to the next space; good enough for the unquoted fields we assert on)."""
    for tok in line.split():
        if tok.startswith(key + "="):
            return tok[len(key) + 1:]
    return None


# ---------------------------------------------------------------------------
# (1) a successful save-back emits CRED-AUDIT op=saveback decision=wrote
# ---------------------------------------------------------------------------

def test_saveback_emits_cred_audit_wrote():
    """No drift: the live mount matches rayi1's anchor, so the swap's credentials
    save-back into rayi1's snapshot succeeds — and emits a `CRED-AUDIT
    op=saveback ... decision=wrote` line carrying the matching incoming/existing
    identity + a token fingerprint (never the raw token)."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1-old"), "merkos": _creds("rt-m", "at-m")},
        active="rayi1",
        live_creds=_creds("rt-1", "at-1-REFRESHED"),
        live_identity=_cj("uuid-rayi1", "rayi1@x"))
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cus.execute_swap("merkos")
        lines = _audit_lines(buf.getvalue())
        # Two save-back writes land for rayi1: the identity (.claude.json) line and
        # the credentials (.credentials.json) line. Assert on the credentials one —
        # it carries the token fingerprint (identity + tokens both audited).
        wrote = [ln for ln in lines
                 if _field(ln, "op") == "saveback" and _field(ln, "decision") == "wrote"
                 and _field(ln, "account") == "rayi1" and _field(ln, "token_fp")]
        assert wrote, f"expected a CRED-AUDIT saveback/wrote (creds) line for rayi1; got: {lines}"
        line = wrote[0]
        # identity fields present and matching (no clobber); token fingerprinted, not raw.
        assert _field(line, "incoming_identity") == "uuid-ray/rayi1@x", line
        assert _field(line, "existing_identity") == "uuid-ray/rayi1@x", line
        assert (_field(line, "token_fp") or "").startswith("sha256:"), line
        assert "rt-1" not in line and "at-1" not in line, f"raw token leaked into audit line: {line}"
        # A swap-install line for the incoming account is also emitted.
        assert any(_field(ln, "op") == "swap-install" and _field(ln, "decision") == "wrote"
                   and _field(ln, "account") == "merkos" for ln in lines), lines
    finally:
        env.restore()


# ---------------------------------------------------------------------------
# (2) a refused wrong-account write emits CRED-AUDIT op=identity-refuse
# ---------------------------------------------------------------------------

def test_refused_identity_write_emits_cred_audit_refused():
    """Swap rayi1 -> merkos while the live mount has DRIFTED to rayi2's identity.
    The guard refuses to save rayi2's identity into rayi1's canonical store — and
    that refusal emits a `CRED-AUDIT op=identity-refuse ... decision=refused-identity`
    line whose incoming_identity (rayi2) visibly disagrees with existing (rayi1)."""
    env = _Env(
        {"rayi1": _creds("rt-1", "at-1"), "merkos": _creds("rt-m", "at-m")},
        active="rayi1",
        live_creds=_creds("rt-1", "at-1-refreshed"),   # rayi1 lineage → isolates the IDENTITY vector
        live_identity=_cj("uuid-rayi2", "rayi2@x"))     # but the identity is rayi2's — the drift
    try:
        env.write_meta("rayi1", "uuid-rayi1", "rayi1@x")
        env.add_family("rayi1", "family-1", "uuid-rayi1", "rayi1@x")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cus.execute_swap("merkos")
        lines = _audit_lines(buf.getvalue())
        refused = [ln for ln in lines
                   if _field(ln, "op") == "identity-refuse"
                   and _field(ln, "decision") == "refused-identity"
                   and _field(ln, "account") == "rayi1"]
        assert refused, f"expected a CRED-AUDIT identity-refuse line for rayi1; got: {lines}"
        line = refused[0]
        # the clobber, one grep away: incoming (rayi2) != existing (rayi1).
        assert _field(line, "incoming_identity") == "uuid-ray/rayi2@x", line
        assert _field(line, "existing_identity") == "uuid-ray/rayi1@x", line
        assert "quarantined=" in line, line
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
