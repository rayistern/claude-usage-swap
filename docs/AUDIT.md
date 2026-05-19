# Code audit (2026-05-19)

External agent audit of `cus.py` + `hooks/*.sh`. Prioritized P0-P3. **P0 + critical P1s fixed in this commit**; remaining items tracked here for future work / community contribution.

## Fixed in 2026-05-19 commit (b607000 + this)

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | **P0** | `cus switch` CLI broken post-`init --force`. References legacy filenames (`credentials.json` + `claude-identity.json`) that `init` deletes; would hit `ERROR: ...credentials.json is missing`. | Rewrote `switch` to delegate to `execute_swap()` — single source of truth, uses the correct `.credentials.json` + `.claude.json` paths. |
| 2 | **P0** | `execute_swap` not atomic: `shutil.copy2` writes in chunks; another process (Claude's token refresh) reading `~/.claude/.credentials.json` mid-copy sees truncated bytes. | Added `atomic_copy()` helper (read-then-atomic-write). Both credential file replacements in `execute_swap` now use it. |
| 3 | **P1** | Threshold ladder hardcoded `THRESHOLD_STEPS = [50, 75, 90, 100]` even though config has `thresholds.steps`. Editing config didn't affect actual ladder progression. | Earlier commit (b607000) fixed `execute_swap` + `maybe_reset_thresholds` to read `config["thresholds"]["steps"]`. |
| 4 | **P1** | `decide_swap` had no hard-7d-cap path. User's stated goal "never go over 80%" wasn't enforceable except as a normal threshold step. | Added Trigger 1 to `decide_swap`: if active's 7d ≥ `hard_7d_cap_pct` (config'd 80% default), force-swap regardless of progressive ladder. |

## Remaining — known issues, tracked for future fix or community contribution

### P1 — correctness / races

| # | Finding | Location | Recommendation |
|---|---|---|---|
| 5 | **No locking on state.json** | `cus.py:329-330`, `744`, `1999`, `2117-2124` | `fcntl.flock` on `state.json` for read-decide-write critical sections. Also detect "another daemon running" via flock on `daemon.pid`. **Impact**: single-daemon setups are fine; users with both systemd + cron-driven `cus daemon` get silent state corruption. |
| 6 | **OAuth token-refresh collision** | `cus.py:722` (now uses `atomic_copy`, mitigated but not solved) | If Claude itself rewrites `.credentials.json` during the brief window between our atomic_copy of OUT and IN, the refreshed token is on disk for a moment but we may overwrite. Mitigation requires either a flock coordination (Claude doesn't honor it) or an "are tokens fresh?" mtime check. **Impact**: rare; refresh happens hourly, swap happens at threshold crossings. |
| 7 | **Hot-swap relaunch race** | `cus.py:1542-1560` | After `tmux_send_text "/exit"`, code sleeps hard-coded 2s then types `claude --resume ...`. If Claude hasn't exited yet, the relaunch string gets typed *into* Claude as a user message. **Recommendation**: poll `tmux_pane_name(pane)` until it != `claude` (or `node`) with bounded retry; bail if it doesn't transition. |
| 8 | **`/exit` may not be a Claude slash-command** | `cus.py:1543-1544` | Unverified empirically. If Claude's TUI doesn't recognize `/exit`, the text goes into the input box. **Recommendation**: verify; consider `C-c C-c` or `C-d` fallback. |
| 9 | **Tier 3 single Escape** | `cus.py:1523` | Single `tmux send-keys Escape` may not interrupt a running tool reliably; Claude docs suggest double-Esc. **Recommendation**: Esc, sleep 0.3, Esc again. |
| 10 | **Subagent skip-guard no max-defer timeout** | `cus.py:1497-1503` | With `defer_below_tier: 100` (current default per user preference), a wedged subagent (Task that never logs a `stop`) → infinite deferral. Account can hit 100% without swap. **Recommendation**: add `max_defer_seconds`; on overshoot, log to inbox.md and proceed. |
| 11 | **`find_live_sessions` returns stale session-ids per pane (verified 2026-05-19)** | `cus.py:1166-1248` | Earlier theory ("cross-account `--resume` doesn't work") was WRONG. Empirical finding: `claude --resume <id>` works fine cross-account because `projects/` is symlinked across accounts. The actual bug is that `find_live_sessions` returns one entry per session-id (deduped by id, NOT by pane). A tmux pane with a history of N claude → /exit → claude cycles produces N session-id entries. The orchestrator then types `claude --resume <id_1>`, `claude --resume <id_2>`, ..., `claude --resume <id_N>` into the SAME pane in sequence. First one starts loading; subsequent ones get typed INTO claude's TUI as text. Additionally, the first `claude --resume <id>` may pick a stale id (not the one currently running in the pane), so the "resumed" session has UNRELATED conversation content. **Hot-swap disabled by default 2026-05-19** until this is fixed. **Real fix**: track current session per pane (latest SessionStart entry per `TMUX_PANE` wins); orchestrator iterates panes (not session-ids); only resume the per-pane current id. Optionally also: validate the pane's `pane_current_command` is `claude`/`node` before sending `/exit`. See `inbox.md` "Hot-swap orchestration disabled 2026-05-19" for incident notes. |
| 11b | **Wake-up message as positional arg** (FIXED in 2026-05-19 rewrite) | `cus.py:1559` | Positional dropped. Relaunch is `claude [flags] --resume <id>` only. |
| 11c | **Orchestration race — daemon + manual can both orchestrate** (verified 2026-05-19) | `cus.py:1717+` | When `hot_swap.enabled=true`, the systemd daemon's `one_cycle` and a manual `cus auto-swap --orchestrate` (or my `systemd-run` detached test) BOTH call `hot_swap_orchestrate`. Both type `/exit` + relaunch into the same panes. Empirically: 2026-05-19 test showed daemon ran a SECOND swap (default→merkos tier 2) 100s after the manual swap (merkos→default tier 1). The pause_message and a fresh session-id appearing in `f9bd536d.jsonl` are evidence. **Fix**: `fcntl.flock` on `~/claude-accounts/orchestrate.lock` at start of `hot_swap_orchestrate`; abort if held. |
| 11d | **`/exit` typed when claude's input box has focus or text is ignored as a slash command** (verified 2026-05-19) | `cus.py:1813` | Pane %3 in the 2026-05-19 test got `/exit` typed via tmux send-keys, but claude didn't actually exit — the text went INTO the input box rather than being interpreted as a slash command. Our `wait_for_shell` defensively skipped that pane's relaunch, but `/exit` was left as text in the input. **Fix**: before sending `/exit`, send `Escape` (lose focus) and `C-u` (clear line). |
| 11e | **`CLAUDE_CODE_SESSION_ID` env-var inheritance into subprocess Haiku calls** (verified 2026-05-19, NOT a cus bug — root cause is in user's `validator.sh`) | n/a — external | Documented here as supporting context for the bungled-relaunch postmortem. When user's `validator.sh` Stop hook calls `claude -p --model haiku "$PROMPT"`, the child claude inherits `$CLAUDE_CODE_SESSION_ID` from the parent. Child claude writes the validator template as a user message in the PARENT'S JSONL transcript. When that session is later resumed (by us or manually), the validator text shows up as user-message history. **Fix (in user's hook, not ours)**: `env -u CLAUDE_CODE_SESSION_ID claude -p ...`. |
| 12 | **Hooks parse JSON with grep+sed** | `hooks/*.sh` | Special characters (escaped quotes, unicode escapes) in session_id or cwd would break parsing. UUIDs are safe; arbitrary cwds aren't. **Recommendation**: switch to `jq -r '.session_id // "unknown"'` with grep fallback. |
| 13 | **No detection of duplicate daemon** | `cus.py:2077-2080` | Two `cus daemon` processes both write `daemon.pid`, both poll, both swap. **Recommendation**: `fcntl.flock(daemon.pid, LOCK_EX \| LOCK_NB)` on start; exit if held. |
| 14 | **state.json has no schema version** | `cus.py:325-330` | Future field renames silently re-initialize. **Recommendation**: add `schema_version: 1`; bump + migrate on load. |

### P2 — robustness

| # | Finding | Recommendation |
|---|---|---|
| 15 | `migrate_account_dir` swallows `read_json` failure on `claude-identity.json` → writes empty identity. | Re-raise on parse failure; warn user. |
| 16 | `atomic_write_bytes` tempfile has permissive mode for a window before `os.chmod`. | Use `os.open(..., flags=O_CREAT\|O_EXCL\|O_WRONLY, mode=mode)`. |
| 17 | `cache_warm` last-line read works but is fragile for files without trailing newline. | Add a test fixture. |
| 18 | Daemon log unbounded; same for 429.log, tool_use.log, sessions.log, stops.log. | Add logrotate config or in-process size-based rotation. |
| 19 | Daemon log via `click.echo`; no timestamps on most lines, no tracebacks on caught exceptions. | Use stdlib `logging` with timestamps + tracebacks. |
| 20 | No retry on transient API errors. Single timeout → stale data this cycle. | 1 retry with 2s backoff; ignore on second failure. |
| 21 | `init`'s default symlinks all shared subdirs to `~/.claude/` — enforces sharing even if user wanted per-account skills. | Document; add `--no-share-skills` flag if requested. |

### P3 — smell

| # | Finding | Recommendation |
|---|---|---|
| 22 | `check_rate_limit_reactive` re-parses `sessions.log` once per 429 entry. | Hoist out of loop. |
| 23 | Hardcoded `THRESHOLD_STEPS` constant alongside config-driven ladder. | Drop the constant or rename to "default." |
| 24 | `cus install` invokes `init_systemd_cmd` declared later in file — works via deferred resolution but fragile. | Move command defs above `install`. |
| 25 | `_relogin_command_for` has unused `source_dir` parameter. | Remove. |
| 26 | `pin` accepts arbitrary string with no validation against current panes/sessions. | Validate against `tmux list-panes` and `find_live_sessions()`. |
| 27 | `install_hooks` uses empty matcher `""` — could collide with other tools using empty matcher. | Use `cus:*` marker. |
| 28 | `shlex_quote` wrapper around `shlex.quote` is pointless. | Use directly. |
| 29 | `uninstall_cmd` statusLine substring match `"cus.py" in cmd` too loose; could match `mycus.py`. | Compare resolved absolute path. |

## Methodology

Audit done by an external agent given the codebase, asked to look at: atomicity / partial failure, concurrency, hot-swap mechanics, OAuth refresh interaction, hook robustness, error handling, log readability, Tier 3 mechanics, subagent skip-guard semantics, `--resume` cross-account, state.json schema versioning, duplicate-daemon detection.

Severity scale:
- **P0**: broken now or unsafe
- **P1**: correctness gap or race; user could hit silently
- **P2**: robustness gap; observable as bad UX but not corruption
- **P3**: smell / minor

Findings filtered to remove items that turned out to be wrong on closer inspection (e.g. the agent flagged that "migration never creates a `.claude.json` for default" but verification shows it does — both migration and fresh-import paths produce it).
