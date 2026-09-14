#!/usr/bin/env python3
r"""
analyze_build_failure.py <artifact-dir>

Turn a red nightly build into something readable. Given the unpacked artifact
directory (manifest.json + build-error.txt + commits-24h.txt) it asks Claude to
explain what broke and who most likely broke it, then writes

    <REPORTS_DIR>\build_report_<YYYYMMDD>.md

next to the axum_report_<date>.md files, so the web app lists it in the same
history and the day's progress narrative includes the build.

Deliberately mirrors process_log.py: stdlib urllib, the same .env, the same
model/token env vars, the same isolate-the-API-call-in-its-own-process rule so
a 429 can only fail this script and nothing upstream of it.

Run by build_agent.py; also fine by hand on any artifact dir.
"""

import io
import os
import sys
import json
import time
import urllib.request
import urllib.error

import nb_common as nb

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
MODEL      = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("BUILD_MAX_OUTPUT_TOKENS", "2000"))
MAX_ERROR_CHARS = int(os.environ.get("BUILD_MAX_ERROR_CHARS", "24000"))

PROMPT_PATH = os.environ.get(
    "NB_BUILD_PROMPT_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_failure_prompt.md"))


def read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def clamp(text, limit):
    """Keep the head and tail: the first error and the final summary line are
    the two things that actually matter, and they sit at opposite ends."""
    if len(text) <= limit:
        return text
    half = limit // 2
    return (text[:half]
            + f"\n\n... [{len(text) - limit} chars elided] ...\n\n"
            + text[-half:])


def call_claude(prompt, body):
    if not API_KEY:
        print("[analyze] ANTHROPIC_API_KEY not set", file=sys.stderr)
        return None
    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": f"{prompt}\n\n---\n\n{body}"}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    # A nightly that lands during a rate-limit window should retry, not vanish.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read())
            return "".join(b.get("text", "") for b in data.get("content", []))
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            print(f"[analyze] HTTP {exc.code}: {detail}", file=sys.stderr)
            if exc.code in (429, 500, 502, 503, 529) and attempt < 2:
                wait = 20 * (attempt + 1)
                print(f"[analyze] retrying in {wait}s ...", file=sys.stderr)
                time.sleep(wait)
                continue
            return None
        except Exception as exc:      # noqa: BLE001
            print(f"[analyze] {exc!r}", file=sys.stderr)
            return None
    return None


def main(artifact_dir):
    manifest = nb.read_json(os.path.join(artifact_dir, "manifest.json"), default={}) or {}
    errors = read(os.path.join(artifact_dir, "build-error.txt"))
    if not errors:
        errors = read(os.path.join(artifact_dir, "build.log"))
    if not errors.strip():
        print("[analyze] nothing to analyse in", artifact_dir)
        return 1

    commits = read(os.path.join(artifact_dir, "commits-24h.txt"))
    prompt = read(PROMPT_PATH)
    if not prompt.strip():
        print(f"[analyze] missing prompt at {PROMPT_PATH}", file=sys.stderr)
        return 1

    body = "\n".join([
        "## BUILD CONTEXT",
        f"repo: {manifest.get('repo','?')}",
        f"ref: {manifest.get('ref','?')}",
        f"commit: {manifest.get('short_commit','?')} - {manifest.get('commit_subject','')}",
        f"author: {manifest.get('commit_author','?')}",
        f"target: {manifest.get('target','?')}  profile: {manifest.get('profile','?')}",
        f"trigger: {manifest.get('trigger','?')}",
        f"run: {manifest.get('run_url','?')}",
        "",
        "## COMMITS IN THE LAST 24H",
        commits.strip() or "(none)",
        "",
        "## COMPILER DIAGNOSTICS",
        clamp(errors, MAX_ERROR_CHARS),
    ])

    print(f"[analyze] {len(body)} chars (~{len(body)//4} tokens) -> {MODEL}")
    answer = call_claude(prompt, body)
    if not answer:
        return 2

    date = nb.today_stamp()
    header = (f"# Axum build failure - {date}\n\n"
              f"**{manifest.get('short_commit','?')}** "
              f"{manifest.get('commit_subject','')} "
              f"({manifest.get('commit_author','?')})  \n"
              f"{manifest.get('run_url','')}\n\n---\n\n")
    out = os.path.join(nb.REPORTS_DIR, f"{nb.scoped('build_report')}-{date}.md")
    nb.atomic_write(out, header + answer.strip() + "\n")
    print(f"[analyze] wrote {out}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python analyze_build_failure.py <artifact-dir>")
    sys.exit(main(sys.argv[1]))
