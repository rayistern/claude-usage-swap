# Project Behavior

## Deploy after changes

- Treat every configuration change and every merge as incomplete until the live daemon has been deployed.
- Deploy by restarting the systemd-managed daemon: `systemctl --user restart cus.service`.
- Do not start a separate foreground `cus daemon`; it would compete with the systemd service.
- Before reporting completion, verify that `cus.service` is active with a new start time/PID, the daemon log contains a completed post-restart cycle, and `cus sos` reports no urgent condition.
- If deployment or verification cannot be completed, report the work as **deployment pending** rather than complete.
