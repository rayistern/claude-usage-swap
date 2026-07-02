#!/usr/bin/env python3
"""Phase 0 confirmation harness for the seamless-swap / independent-logins work.

Plan: docs/plans/2026-07-02-seamless-swap-independent-logins.md §6 (Phase 0).
Prior art: issue #109's inlined `verdict.py` (the load-bearing 2-login finding).

Phase 0 exists to turn the four "to CONFIRM" items in §2 of the plan from
assumptions into measured facts BEFORE the swap-path change (Phase 2) is built
on them. This harness automates everything that can be automated and gives a
precise, copy-pasteable procedure for the parts that genuinely need a human at
an interactive `claude /login` browser flow.

Self-contained on purpose (stdlib only, no import of cus) so it can be copied
into a scratch area and run against throwaway config dirs — same posture as the
#109 verdict.py. Nothing here touches ~/.claude/ or the live cus state.

The four items (see §2 "To CONFIRM"):
  1. read-cadence     — does a LIVE session pick up an in-place creds swap at
                        once (re-reads per request) or only on next refresh?
                        [needs a human: long-lived interactive session]
  2. drift-writeback  — whether/when a live session writes its OWN refreshed
                        token back over the live file (the #3 drift path).
                        [needs a human: watch a live session over ~1h]
  3. multi-login      — do >2 independent logins of ONE account coexist without
                        invalidating each other? [AUTOMATED here]
  4. idle-expiry      — how long does a stored, idle independent login stay
                        refreshable? [AUTOMATED ledger, but spans days]

Commands:
  alive   <config-dir>                 one liveness probe of a config dir
  multi   <dir1> <dir2> [...] [-r N]   round-robin liveness over N rounds (#3)
  files                                filesystem-mechanics demo (§2 finding #2)
  ledger  stamp  <name> <config-dir>   record a login for later expiry re-check
  ledger  check                        re-probe every stamped login (#4)

For items 1 and 2, run `python3 phase0_confirm.py --procedure` and follow the
printed steps; record results back into the plan doc (annotate, don't rewrite).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PATH = Path(__file__).resolve().parent / "idle_expiry_ledger.json"
# A trivial prompt that forces `claude` to authenticate (and refresh the access
# token if it has expired) while costing almost nothing. --print is one-shot and
# non-interactive, which is exactly why the liveness rounds CAN be automated.
PROBE_ARGS = ["--print", "reply with the single word: ok"]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fp(refresh_token: str) -> str:
    """Short non-reversible fingerprint — matches cus._refresh_fingerprint so
    results line up with what `cus login-mount --list` prints."""
    return "sha256:" + hashlib.sha256(refresh_token.encode()).hexdigest()[:12]


def _creds_facts(config_dir: Path) -> dict:
    """Inode, mtime, and refresh-token fingerprint of a mount's creds file.

    Inode is the tell for the atomic-rename mechanics (§2 finding #2): a refresh
    swaps the inode at the path, which is what severs shared symlinks/hardlinks.
    The refresh-token fingerprint is the tell for lineage — two independent
    logins MUST show different fingerprints and rotate independently."""
    p = config_dir / ".credentials.json"
    if not p.exists():
        return {"exists": False}
    try:
        st = p.stat()
        creds = json.loads(p.read_text())
        oauth = creds.get("claudeAiOauth", {}) if isinstance(creds, dict) else {}
        rt = oauth.get("refreshToken")
        return {
            "exists": True,
            "inode": st.st_ino,
            "mtime": st.st_mtime,
            "expiresAt": oauth.get("expiresAt"),
            "refresh_fp": _fp(rt) if isinstance(rt, str) and rt else None,
        }
    except (json.JSONDecodeError, OSError) as e:
        return {"exists": True, "error": str(e)}


def cmd_alive(config_dir: str) -> int:
    """Run one `claude --print` under config_dir; report auth success + facts.

    rc note (from #109): a probe can return rc != 0 because the account is at
    its usage cap while the OAuth refresh itself SUCCEEDED — auth-liveness and
    usage-cap are orthogonal. We report both the process rc and the pre/post
    creds facts so a cap-driven failure isn't misread as an auth failure."""
    d = Path(config_dir).expanduser()
    before = _creds_facts(d)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(d)
    try:
        proc = subprocess.run(["claude", *PROBE_ARGS], env=env,
                              capture_output=True, text=True, timeout=120)
        rc, out, err = proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        print("ERROR: `claude` not on PATH.", file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        rc, out, err = 124, "", "timeout"
    after = _creds_facts(d)
    rotated = (before.get("refresh_fp") != after.get("refresh_fp")
               and before.get("exists") and after.get("exists"))
    print(json.dumps({
        "ts": _now(), "dir": str(d), "rc": rc,
        "refresh_fp_before": before.get("refresh_fp"),
        "refresh_fp_after": after.get("refresh_fp"),
        "refresh_rotated": rotated,
        "inode_before": before.get("inode"), "inode_after": after.get("inode"),
        "stdout_head": out[:80], "stderr_head": err[:120],
    }, indent=2))
    # Liveness = the creds file still parses with a refresh token afterwards.
    return 0 if after.get("refresh_fp") else 1


def cmd_multi(dirs: list[str], rounds: int) -> int:
    """Round-robin liveness across independent-login dirs of the SAME account.

    Confirms §2 item #3 (>2 logins): after N rounds every dir must still be
    alive and each must keep its OWN refresh lineage (fingerprints stay
    distinct across dirs; none blanks another). This generalises the #109
    two-login finding to N. FAILS LOUD if any family invalidates another."""
    paths = [Path(d).expanduser() for d in dirs]
    print(f"# multi-login: {len(paths)} dirs x {rounds} rounds  ({_now()})")
    for r in range(1, rounds + 1):
        print(f"\n## round {r}")
        for d in paths:
            rc = cmd_alive(str(d))
            if rc != 0:
                print(f"!! {d} not alive in round {r} — a login family may have "
                      f"been invalidated (see facts above). STOPPING.")
                return 1
    fps = {str(d): _creds_facts(d).get("refresh_fp") for d in paths}
    distinct = len({v for v in fps.values() if v})
    print(f"\n# final fingerprints: {json.dumps(fps, indent=2)}")
    if distinct < len([v for v in fps.values() if v]):
        print("!! two dirs share a refresh-token family — NOT independent logins.")
        return 1
    print(f"OK: {len(paths)} independent logins survived {rounds} rounds, "
          f"lineages stayed distinct.")
    return 0


def cmd_files() -> int:
    """Filesystem-mechanics demo (§2 finding #2), no OAuth involved.

    Reproduces WHY 'share the creds file' schemes break. The scenario that
    matters is: the LIVE path (what Claude reads/writes, e.g.
    ~/.claude/.credentials.json) is a symlink OR hardlink onto a shared target,
    and Claude refreshes by writing a temp file and renaming it OVER the live
    path. os.replace (same primitive as cus.atomic_write_bytes) installs a new
    inode AT the live path, so:
      - symlink at the live path  -> the rename REPLACES the symlink with a
        regular file; the shared target is never updated (stays v1).
      - hardlink at the live path -> the rename gives the live path a new inode;
        the other name (shared target) keeps the old inode/content (stays v1).
    Either way the 'share' is silently severed on the first refresh (~hourly)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # --- symlink case: live path is a symlink to the shared target ---
        shared_s = root / "shared_symlink_target.json"
        shared_s.write_text('{"v": 1}')
        live_s = root / "live_symlink.json"
        live_s.symlink_to(shared_s)
        tmp_s = root / "live_symlink.json.tmp"
        tmp_s.write_text('{"v": 2}')
        os.replace(tmp_s, live_s)  # what a refresh does to the live path
        sym_now_regular = live_s.is_file() and not live_s.is_symlink()
        shared_never_updated = json.loads(shared_s.read_text()).get("v") == 1

        # --- hardlink case: live path is a hardlink onto the shared inode ---
        shared_h = root / "shared_hardlink_target.json"
        shared_h.write_text('{"v": 1}')
        shared_h_ino = shared_h.stat().st_ino
        live_h = root / "live_hardlink.json"
        os.link(shared_h, live_h)
        assert live_h.stat().st_ino == shared_h_ino
        tmp_h = root / "live_hardlink.json.tmp"
        tmp_h.write_text('{"v": 2}')
        os.replace(tmp_h, live_h)
        hard_severed = live_h.stat().st_ino != shared_h.stat().st_ino
        hard_shared_frozen = shared_h.stat().st_ino == shared_h_ino and json.loads(shared_h.read_text()).get("v") == 1

        result = {
            "symlink_replaced_by_regular_file": sym_now_regular,
            "symlink_shared_target_never_updated": shared_never_updated,
            "hardlink_severed_new_inode_at_live_path": hard_severed,
            "hardlink_shared_target_frozen_at_v1": hard_shared_frozen,
            "verdict": "share-the-file schemes break on first refresh (as #109 measured)",
        }
        print(json.dumps(result, indent=2))
        # Fail loud if the mechanics ever DON'T reproduce (e.g. a filesystem
        # where these assumptions change) — the whole design rests on this.
        ok = all([sym_now_regular, shared_never_updated, hard_severed, hard_shared_frozen])
        return 0 if ok else 1


def _load_ledger() -> dict:
    if LEDGER_PATH.exists():
        try:
            return json.loads(LEDGER_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"logins": {}}


def cmd_ledger(action: str, name: str | None, config_dir: str | None) -> int:
    """Idle-expiry ledger (§2 item #4). `stamp` records a login's mint time +
    fingerprint; `check` re-probes each stamped login and appends the result.
    Run `check` days/weeks later to measure how long an idle stored login stays
    refreshable — the number Phase 1's refresh_token_ttl_days is a guess for."""
    ledger = _load_ledger()
    if action == "stamp":
        if not name or not config_dir:
            print("usage: ledger stamp <name> <config-dir>", file=sys.stderr)
            return 2
        facts = _creds_facts(Path(config_dir).expanduser())
        ledger["logins"][name] = {
            "dir": str(Path(config_dir).expanduser()),
            "stamped_ts": _now(),
            "refresh_fp": facts.get("refresh_fp"),
            "checks": [],
        }
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
        print(f"stamped '{name}' at {_now()} fp={facts.get('refresh_fp')}")
        return 0
    if action == "check":
        if not ledger["logins"]:
            print("ledger empty — stamp a login first.")
            return 0
        for nm, rec in ledger["logins"].items():
            rc = cmd_alive(rec["dir"])
            rec["checks"].append({"ts": _now(), "alive": rc == 0,
                                  "fp": _creds_facts(Path(rec["dir"])).get("refresh_fp")})
            age_days = (datetime.now(timezone.utc)
                        - datetime.fromisoformat(rec["stamped_ts"].replace("Z", "+00:00"))
                        ).total_seconds() / 86400.0
            print(f"{nm}: age={age_days:.1f}d alive={rc == 0}")
        LEDGER_PATH.write_text(json.dumps(ledger, indent=2))
        return 0
    print(f"unknown ledger action '{action}'", file=sys.stderr)
    return 2


PROCEDURE = """\
Interactive procedures for the two items a script cannot observe alone.
Record results in docs/plans/2026-07-02-seamless-swap-independent-logins.md §2
(annotate under "To CONFIRM", do not rewrite — preserve-the-log).

ITEM 1 — read cadence (does an in-place swap take effect on a LIVE session?)
  a. Pick two accounts A and B you can log into. Make two scratch dirs:
       mkdir -p ~/p0/mount ~/p0/loginB
  b. Log the mount in as A, and loginB in as B (independent logins):
       CLAUDE_CONFIG_DIR=~/p0/mount  claude   # /login as A, then keep it running
       CLAUDE_CONFIG_DIR=~/p0/loginB claude   # /login as B, then /exit
  c. In the RUNNING A session, ask "which account/email am I?" (or /usage).
  d. From another terminal, swap B's creds into the live mount in place:
       cp ~/p0/loginB/.credentials.json ~/p0/mount/.credentials.json
  e. In the SAME still-running A session, ask again WITHOUT restarting.
     -> If it answers as B on the very next turn: re-reads per request (best case).
     -> If it stays A until the hourly token refresh: picks up only on refresh.
     Record which, and the delay. (Maintainer's operational baseline: global
     in-place swaps take effect without restart — reproduce and pin it down.)

ITEM 2 — drift write-back (does a live session rewrite the live creds file?)
  a. Note the mount's creds inode+mtime:
       python3 phase0_confirm.py alive ~/p0/mount   # records inode/mtime/fp
  b. Leave the A session idle-but-open for ~70 min (past one token refresh).
  c. Re-run the alive probe and diff inode/mtime/refresh_fp.
     -> If refresh_fp changed and inode changed: the live session refreshed and
        wrote a NEW token family to disk (the #3 drift path is real; the store
        must be per-(mount,account) or a save-back will reconcile families).
     Record the observed rotation interval and whether refreshToken rotated.
"""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Phase 0 confirmation harness (#109).")
    ap.add_argument("--procedure", action="store_true",
                    help="Print the interactive procedures for items 1 and 2 and exit.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("files")
    p_alive = sub.add_parser("alive")
    p_alive.add_argument("config_dir")
    p_multi = sub.add_parser("multi")
    p_multi.add_argument("dirs", nargs="+")
    p_multi.add_argument("-r", "--rounds", type=int, default=2)
    p_led = sub.add_parser("ledger")
    p_led.add_argument("action", choices=["stamp", "check"])
    p_led.add_argument("name", nargs="?")
    p_led.add_argument("config_dir", nargs="?")
    args = ap.parse_args(argv)

    if args.procedure:
        print(PROCEDURE)
        return 0
    if args.cmd == "files":
        return cmd_files()
    if args.cmd == "alive":
        return cmd_alive(args.config_dir)
    if args.cmd == "multi":
        return cmd_multi(args.dirs, args.rounds)
    if args.cmd == "ledger":
        return cmd_ledger(args.action, args.name, args.config_dir)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
