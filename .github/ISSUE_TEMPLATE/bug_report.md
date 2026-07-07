---
name: Bug report
about: Something cus did wrong (a swap that shouldn't have fired, a stall, a wrong account)
title: ""
labels: bug
---

**What happened**
<!-- One or two sentences. -->

**What you expected**

**Repro / context**
- `cus` version or commit:
- OS + Python version:
- Number of accounts / concurrent sessions:
- Mode: `global` or `per_session`

**Diagnostics** (redact tokens — never paste `.credentials.json` contents)
```
# output of:
cus sos
cus status
# and, if a swap decision is in question:
cus daemon --once --no-execute
# and recent daemon log:
journalctl --user -u cus.service -n 50   # or: tail -50 ~/claude-accounts/daemon.log
```

**Anything else**
<!-- Screenshots of the statusline, relevant config.yaml knobs, etc. -->
