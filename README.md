# nightly-built

Nightly (and on-push) firmware builds for **Axum**, feeding the daily progress
reports in [`JPI-US/data-processor-scheduler`](https://github.com/JPI-US/data-processor-scheduler).

Two halves:

| Half | Runs on | Job |
| --- | --- | --- |
| **`.github/workflows/nightly-build.yml`** | GitHub-hosted runner | fresh pull → `esp clean` → `cargo clean` → `cargo build` → flashable image |
| **`agent/`** | the Monitor PC (`D:\scheduler`) | watch for results, keep the last good image, flash it, publish into the reports the 11 PM analysis reads |

```
push to `deployment` ──┐
                       ├──► GitHub Actions ──► artifact (manifest.json + merged .bin + build log)
22:00 local (cron) ────┘                              │
                                                      ▼
                                            agent/build_agent.py  (polls)
                                                      │
                        ┌─────────────────────────────┼─────────────────────────────┐
                        ▼                             ▼                             ▼
                 PASS: promote to            FAIL: keep last good,          always: build-<date>.json/.md
                 known-good/, optionally      toast, ask Claude why         + build-status.json
                 flash it                                                   + a note in the capture log
                        │
                        ▼
             agent/capture_supervisor.py  ──► stops the monitor, flashes, reattaches.
                                              No Ctrl+C. No Ctrl+R.
```

---

## What "esp clean" means here

Every build wipes `.embuild/` **and** `target/` before `cargo build`. `.embuild` is
where `esp-idf-sys` vendors and builds ESP-IDF itself — leaving it in place means
the ESP-IDF half of the build is not actually fresh, and that is precisely the half
that hides stale-`sdkconfig` problems. `idf.py` leftovers (`build/`, `sdkconfig`,
`sdkconfig.old`) go too.

The cost is honesty about time: a genuinely clean build is roughly **15–25 minutes**
on a hosted runner, every time, because ESP-IDF v5.2.2 (pinned in
`.cargo/config.toml`) is recompiled from scratch, and `[unstable] build-std` rebuilds
`std` and `panic_abort` with it. That is the intended trade for a nightly. If
push-triggered builds start feeling too slow, the thing to add is a *separate* fast
lane with `Swatinem/rust-cache` — not caching on this workflow, which would defeat
its purpose.

`.env` and `certs/` are written before the clean and deliberately survive it; only
`.embuild/`, `target/` and any `idf.py` leftovers are removed.

## Setup

### 1. This repo (`JPI-US/nightly-built`)

**Settings → Secrets and variables → Actions → Variables:**

Every variable already defaults to what `JPI-US/Janta_Power` needs, so you only
have to set one if you are changing it.

| Variable | Default | Notes |
| --- | --- | --- |
| `NB_FIRMWARE_REPO` | `JPI-US/Janta_Power` | |
| `NB_FIRMWARE_BRANCH` | `deployment` | |
| `NB_BUILD_TZ` | `America/Chicago` | the timezone "10 PM" is measured in |
| `NB_CHIP` | `esp32s3` | |
| `NB_RUST_TARGET` | `xtensa-esp32s3-espidf` | from `.cargo/config.toml` |
| `NB_CARGO_PROFILE` | `release` | |
| `NB_BIN_NAME` | `tower` | the `[[bin]]` in `Cargo.toml` |
| `NB_PUBLISH_RELEASE` | *unset* | see the warning below |

**Secrets — the build cannot succeed without the first three:**

| Secret | Required | Notes |
| --- | --- | --- |
| `FIRMWARE_DOTENV` | **yes** | the entire contents of the tower's `.env` |
| `TOWER_CERT_PEM` | **yes** | contents of `tower_<DEVICE_ID>-certificate.pem.crt` |
| `TOWER_KEY_PEM` | **yes** | contents of `tower_<DEVICE_ID>-private.pem.key` |
| `NB_FIRMWARE_TOKEN` | only when private | fine-grained PAT, `Contents: read` on the firmware repo |

## Why those secrets are mandatory

`Janta_Power` does not build from a clean checkout on its own, and neither
failure is obvious:

- **`build.rs` bakes `.env` into `constants.rs`** — gear reduction, soft limits,
  encoder calibration, `DEVICE_ID`, Wi-Fi. `.env` is gitignored, and every lookup
  has a fallback default. Without the secret the build **succeeds** and produces
  an image configured for a different tower (`DEVICE_ID=10`, `GEAR_REDUCTION=1.0`,
  the default SSID). The workflow refuses to build rather than let that ship.
- **`mqtt.rs` `include_str!`s the AWS IoT certs at compile time**, from
  `certs/tower_<DEVICE_ID>-{certificate.pem.crt,private.pem.key}`, and `certs/`
  is gitignored except `fullchain.pem`. Missing them is a hard compile error.

`AmazonRootCA1.pem` is public material, so the workflow downloads it rather than
holding a fourth secret. `DEVICE_ID` is parsed back out of `FIRMWARE_DOTENV`, so
the cert filenames can't drift from the `.env`.

**The image is therefore device-specific** — it embeds one tower's credentials and
constants. `manifest.json` records `device_id` so the installer can refuse to flash
it to the wrong tower. Building for the fleet means a matrix over `DEVICE_ID`, which
this workflow does not yet do.

> ### Make this repo private before adding the cert secrets
>
> The merged `.bin` contains that tower's **AWS IoT private key**, and workflow
> artifacts on a **public** repo are downloadable by anyone who can see the repo.
> Secrets themselves are safe (they're masked, and no `pull_request` trigger
> exists here, so fork PRs cannot run this workflow) — the artifact is the leak.
>
> `NB_PUBLISH_RELEASE` is worse and stays off by default: it would publish that
> same image to an unauthenticated URL.

### 2. The firmware repo

Copy [`firmware-repo-hook/nightly-dispatch.yml`](firmware-repo-hook/nightly-dispatch.yml)
to `.github/workflows/nightly-dispatch.yml` there, and add a secret
`NIGHTLY_DISPATCH_TOKEN` (fine-grained PAT with `Actions: read/write` on
`JPI-US/nightly-built`). While `Janta_Power` is public the dispatch still needs that secret, since it writes to another repo. That is all the firmware repo needs to know — every
build decision stays here.

### 3. The Monitor PC

Copy `agent/` to `D:\scheduler\agent\` and add to the existing `D:\scheduler\.env`:

```ini
NB_GITHUB_TOKEN=ghp_...        # Actions: read  on JPI-US/nightly-built
NB_GITHUB_REPO=JPI-US/nightly-built
NB_AUTO_FLASH=0                # 1 = flash every green build automatically
NB_ERASE_FLASH=1               # erase-flash before writing (see the NVS note)
```

`AXUM_PORT`, `AXUM_LOG_DIR`, `AXUM_REPORTS_DIR`, `ANTHROPIC_API_KEY` are read from
that same file — the agent deliberately shares one `.env` with `process_log.py`.

Then run the two long-lived processes (`start-supervisor.cmd`, `start-build-agent.cmd`):

```
python capture_supervisor.py     # replaces capture_daemon.py
python build_agent.py            # watches GitHub
```

---

## The capture jank, fixed

`capture_supervisor.py` is a drop-in replacement for `capture_daemon.py`. It keeps
everything that worked — 23:00 rollover, dated UTF-16 slices, background
`report_and_retain.py`, `capture-status.json`, reattach across device resets — and
removes the two manual steps:

**"Press Ctrl+R to get lines flowing."**
After espflash attaches, if nothing arrives within `NB_AUTO_RESET_AFTER` seconds
(default 25) the supervisor runs `espflash reset` itself and reattaches. It only
does this while the current attach has produced **zero** lines, at most
`NB_AUTO_RESET_MAX` times (default 2), and the budget is restored the moment a
line arrives — so a tower that is legitimately quiet overnight is never reset in
a loop.

**"Ctrl+C, flash, start capture again."**
The supervisor owns the serial port and takes commands on `127.0.0.1:8787`. Ask it
to flash and it sequences the whole thing — stop monitor, wait for Windows to
release the COM handle, `erase-flash`, `write-bin`, reattach — then annotates the
day's capture log with what it flashed:

```
[2026-09-02 22:41:07.812] === NIGHTLY FLASH a1b2c3d begin (image=axum-merged.bin, erase=True) ===
[2026-09-02 22:42:55.401] === NIGHTLY FLASH a1b2c3d ok (exit 0) ===
```

so the 11 PM report explains the reboot instead of inventing a reason for it.
Capture is down for the length of the flash and not one second longer. **If any
step fails, the port still goes back to capture** — a bad build cannot take the
data collection down with it.

### `nbctl.py`

```
python nbctl.py status
python nbctl.py reset                     # what Ctrl+R used to do
python nbctl.py note "swapped the limit switch"
python nbctl.py pause / resume
python nbctl.py flash <image.bin> [--label a1b2c3d] [--no-erase]
python build_agent.py --flash-known-good  # roll back to the last image that built
python build_agent.py --trigger           # start a build now
```

> **`--no-erase` matters more than it looks.** `erase-flash` wipes NVS, and Axum
> persists heading/encoder snapshots there. A full erase means the tower re-homes
> from nothing on the next boot, which is visible in the report. Use `--no-erase`
> when you want to compare a firmware change against yesterday's stored state.

## What lands in the reports directory

Alongside the existing `axum_report_<date>.md` / `digest-<date>.txt`:

| File | Written by | Contents |
| --- | --- | --- |
| `build-status.json` | `build_agent.py` | latest verdict, commit, run URL — the live tile |
| `build-<date>.json` | `build_agent.py` | full manifest + run metadata |
| `build-<date>.md` | `build_agent.py` | dated build note: verdict, commits in the last 24h, diagnostics |
| `build_report_<date>.md` | `analyze_build_failure.py` | Claude's explanation of a red build |
| `flash-<date>.json` | `capture_supervisor.py` | what was flashed and whether it took |
| `BUILD-FAILED.txt` | `build_agent.py` | present only while the newest build is red |
| `capture-status.json` | `capture_supervisor.py` | as before, plus `supervisor`, `control_port`, `auto_flash`, `last_flash` |

All written with the same atomic temp-file + `os.replace` + bounded-retry pattern
`capture_daemon.py` uses, because the web app reads these over a LAN mount and
Windows will happily hand you a `PermissionError` mid-write.

## Failure behaviour

| Situation | What happens |
| --- | --- |
| `cargo build` fails | artifact still uploaded (with `build-error.txt`), workflow goes red, GitHub emails you |
| agent sees a red build | `BUILD-FAILED.txt` written, desktop toast, diagnostics sent to Claude → `build_report_<date>.md` |
| known-good image | **never** overwritten by a red build; `--flash-known-good` rolls back |
| image sha256 ≠ manifest | refuses to promote — a truncated download is not a build |
| Anthropic 429 / network | retried 3× with backoff, then given up on; isolated in a subprocess so the agent survives |
| GitHub unreachable | logged, retried on the next poll; capture is entirely unaffected |
| supervisor not running | `build_agent.py` still records and reports; only flashing is skipped |

## Testing without hardware

Both daemons take the same test hooks `capture_daemon.py` does:

```bash
AXUM_CAPTURE_CMD='["python","fake_device.py"]'   # stand in for espflash monitor
AXUM_TEST_ROLL_SECS=60                           # roll every 60 s instead of at 23:00
AXUM_LOG_DIR=... AXUM_REPORTS_DIR=... NB_STATE_DIR=...   # keep it out of D:\scheduler
```

```bash
python build_agent.py --once     # process the newest run, then exit
python build_agent.py --run <id> # replay one specific run
python analyze_build_failure.py <artifact-dir>   # re-run analysis on a saved artifact
```
