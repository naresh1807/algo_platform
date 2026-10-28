# Running this PC as an always-on server

Goal: the platform (Django/Daphne, both Celery workers, Celery Beat, the
live Angel One feed, plus MySQL/Redis via Docker) keeps running every
day without you manually running `scripts\start_platform.ps1`, surviving
logoff and reboots.

## Already done (this session, no action needed)

- **Sleep/hibernate disabled while on AC power** (`powercfg`). A
  sleeping PC isn't a server; this doesn't affect display-off, only full
  system sleep.
- **Docker Desktop set to start automatically when you log in**
  (`%APPDATA%\Docker\settings-store.json`, `"AutoStart": true`).

## One step you need to run yourself (needs Administrator)

Windows Service creation requires elevation, which can't be done
without a UAC prompt only you can click. Open PowerShell **as
Administrator** (right-click the Start menu → "Windows PowerShell
(Admin)" / "Terminal (Admin)"), then:

```powershell
cd "E:\algo_trading\claude ai algo trading platform\algo_platform"
powershell -ExecutionPolicy Bypass -File scripts\install_as_windows_services.ps1
```

This installs 5 real Windows Services (`algo-django`,
`algo-celery-worker-default`, `algo-celery-worker-priority`,
`algo-celery-beat`, `algo-run-live-feed`) — same commands
`start_platform.ps1` already uses, just wrapped via
[NSSM](https://nssm.cc/) instead of run as a tracked foreground process.
Each is set to:
- **Start automatically at boot** — no login required for these 5
  specifically (only Docker Desktop needs a login, see below).
- **Restart automatically 5s after any crash, forever** — this is also
  what makes the boot-ordering problem harmless: if a service starts
  before Docker/MySQL/Redis are ready and fails immediately, NSSM just
  keeps retrying every 5s until it succeeds once Docker catches up.

Manage them afterwards via `services.msc`, or:
```
nssm status algo-django
nssm stop algo-django
nssm start algo-django
```
Logs land in `logs\<name>.service.log` / `.service.err.log` (rotated at
20MB, same as `start_platform.ps1`'s own logs).

To fully remove them later: `scripts\uninstall_windows_services.ps1`
(also needs an elevated PowerShell).

## Windows auto-login (your choice, already made: enabled)

Docker Desktop itself only starts once someone logs into Windows — it
doesn't run as a system service. To close that gap after a genuine
reboot (power outage, Windows Update), enable auto-login:

1. Press **Win+R**, type `netplwiz`, Enter.
2. Uncheck **"Users must enter a user name and password to use this
   computer."**
3. Click **OK** — a dialog asks you to confirm your password. Type it
   there (it's never sent anywhere else, just Windows' own login
   mechanism).

**Trade-off, worth knowing since this is a financial platform:** this
stores your Windows login password in the registry in a weakly-protected
form. Anyone with physical or administrator access to this machine could
retrieve it. Reasonable if this PC is somewhere physically secure and not
shared — reconsider if that's not the case. Day-to-day use (sleep, lock
screen, normal login) is unaffected either way; this only matters for an
actual reboot with nobody present to log back in.

## Watchdog (needed -- a real outage happened without it)

Auto-login gets Docker Desktop running again after a reboot, but that
alone wasn't enough: live-verified, `algo-django` went down for **~3
hours** after a reboot and never recovered on its own -- Windows kept
reporting its service as "Running" the entire time, but the actual
process had exhausted NSSM's own crash-loop retry backoff (most likely
racing Docker/MySQL's own slower cold-start after the same reboot) and
just stayed dead. Nothing was watching for that specific lie.

Install the watchdog (elevated PowerShell, same as the services step):

```powershell
cd "E:\algo_trading\claude ai algo trading platform\algo_platform"
powershell -ExecutionPolicy Bypass -File scripts\install_watchdog_task.ps1
```

This registers a Scheduled Task (as SYSTEM, no login needed) that runs
every 3 minutes: it sends a real HTTP request to `algo-django` (Windows'
own "Running" status already proved untrustworthy for it) and checks
every other `algo-*` service's SCM status, restarting anything found
unhealthy. It only writes to `logs\watchdog.log` when it actually has to
restart something -- silent the rest of the time.

## Verifying it's working

```powershell
# From an elevated or normal PowerShell:
Get-Service algo-* | Select-Object Name, Status

# From any shell:
curl http://127.0.0.1:8000/api/monitoring/health/
```

To really prove it: reboot the PC without logging in immediately
afterward if you skipped auto-login, or just reboot and wait ~1-2
minutes if you enabled it, then check the health endpoint from another
device on the same network.

## Relationship to `scripts\start_platform.ps1`

Unchanged, still useful for local dev — it launches the same 5
processes as tracked foreground processes instead of services, which is
easier to stop/inspect during active development. Don't run both at
once for the same process (a service already listening on port 8000
will make `start_platform.ps1`'s own `django` process fail to bind it,
though it degrades safely — you'll just see that specific process exit
immediately with a clear "port already in use"-style error in
`logs\django.err.log`, no port conflict damage or silent corruption).
