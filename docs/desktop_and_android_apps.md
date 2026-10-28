# Desktop and Android apps

## The one thing to understand before anything else

**The Android app is a thin client. It cannot run the Django backend,
MySQL, Redis, or Celery.** Those keep running somewhere else -- your PC,
or the cloud VM described below. The phone just needs to be able to
reach that backend over the network.

Why: this backend hard-requires MySQL and Redis (no fallback, by
design), needs `mysqlclient` (a native C library, no Android build
path) and `torch`/`transformers` (large platform-specific binaries with
no Android wheels), and runs as **6 concurrent long-running processes**
(Django/Daphne, two Celery workers, Celery Beat, the live Angel One feed,
plus the two database/cache containers). None of that has anywhere to
live under Android's app lifecycle (no persistent multi-daemon
processes, no MySQL/Redis on-device). This is a hard platform
limitation, not a missing feature.

The **desktop app** is different: it's a real native window (Electron)
that, on the same machine, auto-launches your existing
`scripts/start_platform.ps1` -- the same proven process orchestration you
already use, not a reimplementation of it.

---

## Desktop app (Windows)

### Run it in dev mode

```
cd frontend
npm run electron:dev
```

This builds the frontend (`vite build`) and opens a window. On launch,
`electron/main.js`:

1. Checks `http://127.0.0.1:8000/api/monitoring/health/`. If it's not
   reachable, it launches `scripts/start_platform.ps1 -SkipFrontend`
   (Windows only) and waits up to 2 minutes, polling every 2s.
2. Serves the built `frontend/dist/` from its own tiny local HTTP server
   on `http://127.0.0.1:4300/` (a real HTTP origin, not `file://`, so
   `BrowserRouter` keeps working with zero React Router changes).
3. Opens the window pointed at that local server.

Closing the window does **not** stop the backend -- Celery Beat and the
live feed are meant to keep running independently of whether a
dashboard window is open, exactly like today. Stop them the same way you
already do: `scripts/stop_platform.ps1`.

If the backend doesn't come up, check `logs/electron_backend_launch.log`
(start_platform.ps1's own output) and the usual `logs/*.err.log` files.

**Prerequisites** (same as running the platform normally): Docker
Desktop running, `backend/.venv` created with `requirements.txt`
installed, `backend/.env` and `docker/.env` filled in.

**macOS/Linux**: the auto-launch step is Windows-only (it shells out to
a PowerShell script). The window still opens; you'd need to start the
backend yourself first (this is a documented limitation, not something
this app currently handles).

### Build an installer

```
cd frontend
npm run electron:build
```

Produces a Windows NSIS installer under `frontend/release/`.

### Pointing the desktop app at a different backend

By default the desktop app assumes the backend is on the same machine
(`http://127.0.0.1:8000`) -- no setup needed. To point it at the
cloud-hosted backend instead (see below), open the app, go to **Change
server address** on the login screen, and enter the cloud URL.

---

## Android app

### First-time build setup

Requires a JDK and the Android SDK. Verified live: Capacitor 8.x's
Android build needs **JDK 21** specifically -- JDK 17 installs fine but
fails at `compileDebugJavaWithJavac` with "invalid source release: 21".
If you don't have Android Studio already:

1. Install a JDK 21: `winget install --id EclipseAdoptium.Temurin.21.JDK`
2. Install the Android SDK command-line tools (or Android Studio, which
   bundles them) and accept the licenses:
   ```
   sdkmanager --licenses
   sdkmanager "platform-tools" "platforms;android-35" "build-tools;35.0.0"
   ```
3. Set `ANDROID_HOME` to wherever you installed the SDK, and `JAVA_HOME`
   to the JDK 21 install.

### Build

```
cd frontend
npm run build
npx cap sync android
npx cap open android
```

That opens the project in Android Studio, where you can run it on an
emulator/device or build a debug APK (**Build > Build Bundle(s) /
APK(s) > Build APK(s)**). There is no Play Store listing set up -- this
is a sideloaded debug build only.

### First run on the phone

The app has no way to know where your backend is at build time, so the
very first launch takes you straight to a **Server Settings** screen
(before login) to type it in:

- **Same Wi-Fi as your PC**: `http://192.168.x.x:8000` (your PC's LAN
  IP -- find it with `ipconfig`). Requires the phone and PC to be on the
  same network every time; this is why cleartext HTTP is allowed at all
  (see the security note below) -- **never point this at an untrusted
  public `http://` address.**
- **From anywhere, via Tailscale (recommended over cloud for now)**:
  `http://100.x.x.x:8000` -- this PC's Tailscale IP (`tailscale status`
  on the PC, or `Get-Service Tailscale` to confirm it's running). Install
  the Tailscale app on the phone (Play Store), sign into the *same*
  Tailscale account used on the PC, and the phone reaches the PC from
  any network with internet, not just home Wi-Fi -- no port forwarding,
  no public exposure, free. This is a private connection between only
  your own devices (WireGuard-encrypted end to end), not a public URL,
  so the cleartext-HTTP note below is about as low-risk as it gets here.
  `backend/.env`'s `DJANGO_ALLOWED_HOSTS` needs this Tailscale IP added
  (already done for the IP assigned when this was set up -- add a new
  one if you ever re-register the device and get a different IP).
- **Cloud-hosted backend**: the `https://` URL from the cloud section
  below. Only worth it once you want access from devices that aren't
  yours too (or want a real domain instead of a Tailscale IP) -- for
  just your own phone, Tailscale above gets you "anywhere" access today
  at no cost.

Use **Test connection** before saving -- it hits
`/api/monitoring/health/` directly and tells you if the address is
actually reachable. You can change it later from the login screen's
"Change server address" link.

### Security note on cleartext HTTP

`AndroidManifest.xml` sets `android:usesCleartextTraffic="true"` app-wide
-- Android's network-security-config schema has no way to scope
cleartext permission to "private IP ranges only" (it only supports
literal hostnames, not CIDR blocks), so this is all-or-nothing. It has
no effect once you're pointed at the `https://` cloud backend -- the
restriction only ever applies to `http://` destinations. Treat the LAN
option as a same-network-only fallback, not something to expose past
your own Wi-Fi.

---

## Moving the backend to the cloud

This makes the backend reachable from anywhere (not just your home
Wi-Fi) and gives the Android app a real `https://` origin. Both cloud
paths below share the exact same design: one Ubuntu VM/instance running
self-hosted MySQL+Redis via Docker (reusing `docker/docker-compose.yml`
as-is), the five backend processes as systemd units
(`deploy/common/systemd/` -- Linux equivalents of `start_platform.ps1`'s
five processes, no `--pool=solo` since that Celery workaround is
Windows-only), and Caddy in front for automatic HTTPS via a free
`nip.io` hostname (no domain purchase needed). Only the provisioning
step (`provision_vm.sh` vs `provision_ec2.sh`) is cloud-specific;
`deploy/common/vm_setup.sh` runs identically either way. Pick AWS or
GCP -- both require **your own account with billing enabled**, and
neither runs on its own.

**Before running anything in `deploy/`, be aware:**

- **This costs real, ongoing money.** Roughly US$25-40/month for a
  small VM + a 30GB disk + minor egress, at the time this was written --
  check the AWS/GCP pricing calculator for your region before
  committing.
- **This makes your trading backend, including its admin panel and
  broker credentials, reachable from the public internet** (over HTTPS,
  behind a reverse proxy). Only 80/443 are public; SSH (22) is
  restricted to the operator's detected IP by default on both paths
  (`provision_ec2.sh`'s and `provision_vm.sh`'s `MY_IP` detection --
  the GCP path used to leave this open to the whole internet, fixed in
  a security review). 8000/3306/6379 are never exposed on either path.
  Given this is a financial platform, treat SSH access and
  `backend/.env` secrets with the same care as any other production
  credential.
- **Neither path has been run against a real account as part of this
  change** (unlike everything else in this repo's recent history, which
  was verified against the actually-running system) -- there was no
  account to test with at the time. Read the scripts before running
  them, and expect to troubleshoot on the first real run.

### AWS

```
cd deploy/aws
aws configure   # needs an IAM access key: AWS Console > IAM > Users >
                 # your user > Security credentials > Create access key.
                 # Run this yourself -- it prompts for the secret key
                 # interactively and shouldn't be typed anywhere else.
./provision_ec2.sh
# follow its printed instructions: copy the repo over, fill in
# backend/.env and docker/.env with real secrets on the instance, then:
bash deploy/common/vm_setup.sh <the-static-ip-it-printed>
```

### GCP

```
cd deploy/gcp
gcloud auth login   # your own browser login, same reasoning as above
PROJECT_ID=your-gcp-project ./provision_vm.sh
bash deploy/common/vm_setup.sh <the-static-ip-it-printed>
```

### Either way

**Before running `vm_setup.sh`**, edit `backend/.env` and set
`DJANGO_ENVIRONMENT=production` and `DJANGO_DEBUG=0` -- `.env.example`
ships `development`/`1` as its template defaults, and neither looks
like a "secret" so it's easy to fill in only the credential fields and
miss these two. `vm_setup.sh` now checks for both and refuses to
continue if either is wrong (a real gap found and fixed in a security
review: without them, `settings.py`'s own production fail-safes never
trigger, and a public `DEBUG=True` instance leaks source/local
variables in every 500 response).

Once it's up, add the printed `https://<ip-with-dashes>.nip.io` hostname
to `backend/.env`'s `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS`, and
restart `algo-django` (`sudo systemctl restart algo-django`). Then point
both the desktop app's "Change server address" and the Android app's
first-run Server Settings at that same URL.
