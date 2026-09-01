#!/usr/bin/env python3
r"""
build_agent.py - the Monitor PC half of the nightly build.

GitHub does the building (fresh pull, esp clean, cargo clean, cargo build) on a
hosted runner. This agent watches for the result and makes it real on this
machine:

  * polls the nightly-build workflow for newly completed runs;
  * downloads the run's artifact (manifest.json + merged .bin + build log);
  * on PASS  - promotes the image to known-good/ and, if NB_AUTO_FLASH=1, asks
               capture_supervisor.py to flash it WITHOUT interrupting capture
               by hand;
  * on FAIL  - keeps the previous known-good image untouched, notifies, and
               sends the compiler diagnostics to Claude for an explanation;
  * always   - publishes build-status.json + build-<date>.json/.md into the
               reports dir, and drops a one-line note into the day's capture log
               so the 11 PM report sees the build as part of the day's progress.

Standard library only. Run it alongside capture_supervisor.py.

Usage:
  python build_agent.py                 # watch forever (the normal mode)
  python build_agent.py --once          # process the newest run and exit
  python build_agent.py --trigger       # ask GitHub to start a build now
  python build_agent.py --run 12345678  # process one specific run id
"""

import os
import io
import sys
import time
import zipfile
import argparse
import subprocess

import nb_common as nb
import nbctl

CURSOR_PATH = os.path.join(nb.STATE_DIR, "last-seen-run.json")
ANALYZER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "analyze_build_failure.py")


# --------------------------------------------------------------- GitHub API -
def latest_runs(limit=10):
    """Completed runs of the nightly workflow, newest first."""
    status, body = nb.gh_request(
        f"repos/{nb.GH_REPO}/actions/workflows/{nb.GH_WORKFLOW}/runs"
        f"?status=completed&per_page={limit}")
    if status != 200 or not isinstance(body, dict):
        nb.log("build-agent", f"run list failed ({status}): {str(body)[:200]}")
        return []
    return body.get("workflow_runs", [])


def trigger_build(ref=None):
    """workflow_dispatch, so `nbctl build` / --trigger can force a build."""
    payload = {"ref": "main"}
    if ref:
        payload["inputs"] = {"ref": ref}
    status, body = nb.gh_request(
        f"repos/{nb.GH_REPO}/actions/workflows/{nb.GH_WORKFLOW}/dispatches",
        method="POST", data=payload)
    if status == 204:
        nb.log("build-agent", "build requested.")
        return True
    nb.log("build-agent", f"dispatch failed ({status}): {str(body)[:300]}")
    return False


def download_artifact(run_id, dest_dir):
    """Fetch and unpack the run's build artifact. Returns the extract dir."""
    status, body = nb.gh_request(f"repos/{nb.GH_REPO}/actions/runs/{run_id}/artifacts")
    if status != 200 or not isinstance(body, dict):
        nb.log("build-agent", f"artifact list failed ({status}): {str(body)[:200]}")
        return None
    arts = [a for a in body.get("artifacts", [])
            if a.get("name", "").startswith("axum-build") and not a.get("expired")]
    if not arts:
        nb.log("build-agent", f"run {run_id} has no build artifact (expired?)")
        return None
    art = arts[0]
    status, blob = nb.gh_request(art["archive_download_url"], raw=True)
    if status != 200 or not isinstance(blob, (bytes, bytearray)):
        nb.log("build-agent", f"artifact download failed ({status}): {str(blob)[:200]}")
        return None
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(dest_dir)
    except zipfile.BadZipFile as exc:
        nb.log("build-agent", f"artifact is not a zip: {exc}")
        return None
    nb.log("build-agent", f"unpacked {art['name']} -> {dest_dir}")
    return dest_dir


# ------------------------------------------------------------- publishing --
def promote_known_good(extract_dir, manifest):
    """Copy the passing image into known-good/, replacing the previous one.

    Only ever called on PASS, so a red build can never overwrite the last image
    that is known to boot - that is the whole point of keeping it.
    """
    image = manifest.get("image")
    src = os.path.join(extract_dir, image) if image else None
    if not src or not os.path.isfile(src):
        nb.log("build-agent", "PASS but no image in the artifact - not promoting.")
        return None
    digest = nb.sha256_file(src)
    expected = manifest.get("image_sha256")
    if expected and digest != expected:
        nb.log("build-agent", f"sha256 mismatch for {image} "
                              f"(got {digest[:12]}, manifest says {expected[:12]}) "
                              "- refusing to promote.")
        return None
    os.makedirs(nb.KNOWN_GOOD_DIR, exist_ok=True)
    dst = os.path.join(nb.KNOWN_GOOD_DIR, "axum-known-good.bin")
    with open(src, "rb") as fin, open(dst + ".tmp", "wb") as fout:
        fout.write(fin.read())
    os.replace(dst + ".tmp", dst)
    nb.atomic_write_json(os.path.join(nb.KNOWN_GOOD_DIR, "known-good.json"), {
        "image": dst,
        "sha256": digest,
        "commit": manifest.get("commit"),
        "short_commit": manifest.get("short_commit"),
        "commit_subject": manifest.get("commit_subject"),
        "built_at": manifest.get("built_at"),
        "run_url": manifest.get("run_url"),
        "promoted_at": nb.now_iso(),
    })
    nb.log("build-agent", f"known-good <- {manifest.get('short_commit')} ({digest[:12]})")
    return dst


def render_markdown(manifest, run, commits, error_excerpt):
    """A small dated build note the web app lists next to the axum reports."""
    verdict = manifest.get("verdict", "UNKNOWN")
    lines = [
        f"## BUILD VERDICT: {verdict}",
        "",
        f"- **Commit**: `{manifest.get('short_commit','?')}` "
        f"{manifest.get('commit_subject','')} ({manifest.get('commit_author','?')})",
        f"- **Ref**: `{manifest.get('ref','?')}`  **Trigger**: {manifest.get('trigger','?')}",
        f"- **Built**: {manifest.get('built_at','?')}  "
        f"**Target**: {manifest.get('target','?')} / {manifest.get('profile','?')}",
        f"- **Run**: {manifest.get('run_url','?')}",
    ]
    if verdict == "PASS" and manifest.get("image"):
        lines.append(f"- **Image**: `{manifest['image']}` "
                     f"(sha256 `{(manifest.get('image_sha256') or '')[:16]}`)")
    if commits:
        lines += ["", "### Commits in the last 24h", "", "```", commits.strip(), "```"]
    if error_excerpt:
        lines += ["", "### Compiler diagnostics", "", "```", error_excerpt.strip()[:6000], "```"]
    return "\n".join(lines) + "\n"


def publish(manifest, run, extract_dir):
    date = nb.today_stamp()
    commits = _read(extract_dir, "commits-24h.txt")
    error_excerpt = _read(extract_dir, "build-error.txt")

    record = dict(manifest)
    record.update({
        "run_id": run.get("id"),
        "run_number": run.get("run_number"),
        "conclusion": run.get("conclusion"),
        "started_at": run.get("run_started_at"),
        "seen_at": nb.now_iso(),
        "artifact_dir": extract_dir,
    })
    nb.atomic_write_json(os.path.join(nb.REPORTS_DIR, f"build-{date}.json"), record)
    nb.atomic_write(os.path.join(nb.REPORTS_DIR, f"build-{date}.md"),
                    render_markdown(manifest, run, commits, error_excerpt))
    nb.atomic_write_json(nb.BUILD_STATUS_PATH, {
        "verdict": manifest.get("verdict"),
        "short_commit": manifest.get("short_commit"),
        "commit_subject": manifest.get("commit_subject"),
        "built_at": manifest.get("built_at"),
        "run_url": manifest.get("run_url"),
        "checked_at": nb.now_iso(),
        "auto_flash": nb.AUTO_FLASH,
    })
    return record, error_excerpt


def _read(d, name):
    try:
        with open(os.path.join(d, name), "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def analyze_failure(extract_dir, manifest):
    """Hand the compiler diagnostics to Claude, isolated in a subprocess.

    Same containment rule report_and_retain.py uses: a 429 or a network failure
    must not take down the agent that is watching for builds.
    """
    if not os.path.isfile(ANALYZER):
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        nb.log("build-agent", "no ANTHROPIC_API_KEY - skipping failure analysis.")
        return
    nb.log("build-agent", "sending compiler diagnostics to Claude ...")
    try:
        r = subprocess.run([sys.executable, ANALYZER, extract_dir], timeout=300)
        if r.returncode != 0:
            nb.log("build-agent", f"analysis failed (exit {r.returncode}); re-run with: "
                                  f"python analyze_build_failure.py {extract_dir}")
    except subprocess.TimeoutExpired:
        nb.log("build-agent", "analysis timed out.")


# ------------------------------------------------------------------ driver --
def process_run(run):
    run_id = run["id"]
    short = (run.get("head_sha") or "")[:7]
    extract_dir = os.path.join(nb.IMAGES_DIR, f"run-{run_id}")
    nb.log("build-agent", f"processing run {run_id} ({run.get('conclusion')}) ...")

    if not download_artifact(run_id, extract_dir):
        # No artifact at all (cancelled, expired, or the gate job skipped the
        # build). Nothing meaningful to report - don't invent a verdict.
        return False

    manifest = nb.read_json(os.path.join(extract_dir, "manifest.json"))
    if not manifest:
        nb.log("build-agent", "artifact has no manifest.json - skipping.")
        return False

    verdict = manifest.get("verdict", "UNKNOWN")
    record, error_excerpt = publish(manifest, run, extract_dir)
    label = manifest.get("short_commit") or short
    subject = (manifest.get("commit_subject") or "")[:80]

    # Put the build into the capture log so the 11 PM analysis sees it.
    nbctl.send({"cmd": "note",
                "text": f"BUILD {verdict} {label} {subject}"}, quiet=True)

    if verdict == "PASS":
        try:
            os.remove(nb.FAILED_MARKER)
        except OSError:
            pass
        image = promote_known_good(extract_dir, manifest)
        nb.log("build-agent", f"PASS {label} - {subject}")
        if image and nb.AUTO_FLASH:
            flash(image, label)
    else:
        nb.atomic_write(nb.FAILED_MARKER,
                        f"Build FAILED {nb.now_iso()}\n"
                        f"commit {label} {subject}\n"
                        f"{manifest.get('run_url','')}\n\n"
                        f"{error_excerpt[:4000]}\n")
        nb.notify("Axum nightly build FAILED",
                  f"{label} {subject}\n{manifest.get('run_url','')}", "error")
        analyze_failure(extract_dir, manifest)

    return True


def flash(image, label):
    """Flash through the supervisor, so capture stops and restarts itself."""
    nb.log("build-agent", f"asking the supervisor to flash {label} ...")
    resp = nbctl.send({"cmd": "flash", "image": image, "label": label,
                       "erase": nb.ERASE_BEFORE_FLASH})
    if resp.get("ok"):
        nb.log("build-agent", f"flashed {label}; capture resumed on its own.")
        nb.notify("Axum flashed", f"{label} is now running on {nb.PORT}.", "info")
    else:
        nb.log("build-agent", f"flash failed: {resp.get('error')}")
        nb.notify("Axum flash FAILED", str(resp.get("error"))[:200], "error")


def cursor_get():
    return (nb.read_json(CURSOR_PATH, default={}) or {}).get("run_id")


def cursor_set(run_id):
    nb.atomic_write_json(CURSOR_PATH, {"run_id": run_id, "at": nb.now_iso()})


def poll_once():
    runs = latest_runs()
    if not runs:
        return False
    seen = cursor_get()
    fresh = [r for r in runs if seen is None or r["id"] > seen]
    if not fresh:
        return False
    # Oldest first, so a burst of pushes is reported in the order it happened.
    handled = False
    for run in sorted(fresh, key=lambda r: r["id"]):
        try:
            handled |= process_run(run)
        except Exception as exc:      # noqa: BLE001 - one bad run, not a dead agent
            nb.log("build-agent", f"run {run['id']} raised {exc!r}")
        cursor_set(run["id"])
    return handled


def watch():
    nb.log("build-agent", f"watching {nb.GH_REPO} :: {nb.GH_WORKFLOW} "
                          f"every {nb.POLL_SECS}s (auto_flash={nb.AUTO_FLASH})")
    if not nb.GH_TOKEN:
        nb.log("build-agent", "WARNING: no NB_GITHUB_TOKEN - artifact downloads "
                              "will be rejected by GitHub.")
    while True:
        try:
            poll_once()
        except KeyboardInterrupt:
            nb.log("build-agent", "stopped.")
            return
        except Exception as exc:      # noqa: BLE001 - keep watching through flakes
            nb.log("build-agent", f"poll error: {exc!r}")
        time.sleep(nb.POLL_SECS)


def main():
    ap = argparse.ArgumentParser(description="Monitor-PC side of the nightly build.")
    ap.add_argument("--once", action="store_true", help="process new runs and exit")
    ap.add_argument("--trigger", metavar="REF", nargs="?", const="",
                    help="ask GitHub to start a build now (optional firmware ref)")
    ap.add_argument("--run", type=int, help="process one specific run id")
    ap.add_argument("--flash-known-good", action="store_true",
                    help="reflash the last image that built cleanly")
    args = ap.parse_args()

    nb.ensure_dirs()

    if args.trigger is not None:
        return 0 if trigger_build(args.trigger or None) else 1

    if args.flash_known_good:
        meta = nb.read_json(os.path.join(nb.KNOWN_GOOD_DIR, "known-good.json"))
        if not meta:
            nb.log("build-agent", "no known-good image on disk yet.")
            return 1
        flash(meta["image"], meta.get("short_commit", "known-good"))
        return 0

    if args.run:
        status, run = nb.gh_request(f"repos/{nb.GH_REPO}/actions/runs/{args.run}")
        if status != 200:
            nb.log("build-agent", f"no such run ({status})")
            return 1
        return 0 if process_run(run) else 1

    if args.once:
        poll_once()
        return 0

    watch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
