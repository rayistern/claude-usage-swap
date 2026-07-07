# Security & Disclaimers

`cus` reads, writes, and moves **OAuth credentials** for your Claude accounts. Please understand what it touches and the risks before running it.

## What it touches

- **`~/.claude/.credentials.json`** — your live OAuth tokens (access + ~30-day refresh token). `cus` copies these between the live location and per-account snapshots under `~/claude-accounts/account-<name>/` during a swap.
- **`~/.claude.json`** — `cus` merges only the account-bound keys (`userID`, `oauthAccount`) on a swap; everything else (MCP registrations, project state) is left in place.
- **`~/claude-accounts/`** — per-account credential snapshots (mode `0600`), state, config, and logs. This directory holds real refresh tokens. Treat it like an SSH key directory.

`cus` makes only local file operations plus HTTPS calls to Anthropic's OAuth usage endpoint (the same one Claude Code uses for `/usage`). It runs no server, opens no ports, and sends your tokens nowhere except Anthropic.

## Handling guarantees (and limits)

- All credential writes are atomic (tempfile + `os.replace`) and every overwrite keeps a rotated timestamped backup (`.credentials.json.bak.*`, newest 5) so a bad write is recoverable via `cus restore-creds`.
- A swap is serialized by a global lock and journaled, so a crash mid-swap is reconciled rather than left half-applied.
- **Limits:** `cus` is single-machine and single-user. It does not encrypt the snapshots at rest beyond the filesystem `0600` mode Claude Code itself uses. Anyone with read access to your home directory can read these tokens — same threat model as `~/.claude/` without `cus`.

## Redact before sharing

When filing an issue or pasting logs, **never include `.credentials.json` contents or raw tokens.** `cus sos` / `cus status` / `cus daemon --once --no-execute` output is safe to share (it shows percentages and account *labels*, not tokens).

## Important disclaimers

- **Undocumented mechanisms.** `cus` relies on `CLAUDE_CONFIG_DIR` and the OAuth usage endpoint, neither of which is a documented, stable public API. Anthropic can change or remove them in any Claude Code release; a swap or poll may break without warning. `cus doctor` validates the config-dir behavior empirically, but there is no contract.
- **Terms of Service.** `cus` rotates between multiple accounts to stay under per-account usage caps. Whether operating multiple accounts and rotating them this way is consistent with your Claude subscription's terms is **your responsibility to determine** — check the terms of the plans you use. This project takes no position and provides no legal advice. Use at your own risk.
- **No warranty.** MIT-licensed, provided as-is (see `LICENSE`). Credential-management software can fail in ways that log you out or, in the worst case, strand a refresh token; the backup/restore machinery exists precisely because this class of bug is real. Keep the ability to re-login interactively.

## Reporting a vulnerability

If you find a security issue (a way `cus` leaks or corrupts credentials, or an injection in the hook/statusline path), please open a GitHub issue **without** sensitive details and note that you have a security report; a maintainer will arrange a private channel. For non-sensitive hardening ideas, a normal issue is fine.
