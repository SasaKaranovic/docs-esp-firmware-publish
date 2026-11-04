#!/usr/bin/env python3
"""Publish ESP firmware builds to docs.sasakaranovic.com.

Used by action.yml, but also runs on its own (Python 3.9+, standard library only,
plus the `rsync` and `ssh` commands).

For every build (variant=file) it:
  1. reads product.json from the product's folder on the server (written by the docs build
     from firmware.yml) and checks the product, variant and channel,
  2. writes builds/<id>/firmware.bin and builds/<id>/manifest.json (ESP Web Tools manifest),
  3. uploads them with rsync, then
  4. adds the builds to builds.json and uploads it (re-reading it first, so concurrent
     publishes for the same product don't overwrite each other's entries).

Builds are never removed by this script. Remove them on the server with docs-firmware-admin.

Server layout (per product):
    builds.json
    builds/<id>/manifest.json
    builds/<id>/firmware.bin
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

FUNDING_URL = "https://esphome.io/guides/supporters.html"
ID_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class PublishError(Exception):
    pass


# --------------------------------------------------------------------------- helpers


def log(msg):
    print(msg, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_context():
    """Source information from the GitHub Actions environment (empty outside Actions)."""
    env = os.environ
    repo, sha, ref = env.get("GITHUB_REPOSITORY"), env.get("GITHUB_SHA"), env.get("GITHUB_REF", "")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    if not repo or not sha:
        return {}
    ctx = {"repo": repo, "ref": ref, "sha": sha, "url": f"{server}/{repo}/commit/{sha}"}
    if env.get("GITHUB_RUN_ID"):
        ctx["run"] = f"{server}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
    return ctx


def default_notes():
    """Release body for `release` events, otherwise the head commit message."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as fh:
            event = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ""
    text = (event.get("release") or {}).get("body") or (event.get("head_commit") or {}).get("message") or ""
    return text.strip()[:2000]


def default_version(now):
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/"):
        tag = ref[len("refs/tags/"):]
        return tag[1:] if re.match(r"^[vV]\d", tag) else tag
    if os.environ.get("GITHUB_EVENT_NAME") == "release":
        tag = os.environ.get("GITHUB_REF_NAME", "")
        if tag:
            return tag[1:] if re.match(r"^[vV]\d", tag) else tag
    sha = os.environ.get("GITHUB_SHA", "")
    return f"{now:%Y.%m.%d}-{sha[:7]}" if sha else f"{now:%Y.%m.%d-%H%M%S}"


def default_channel():
    """Tags/releases -> stable, default branch -> preview, anything else -> dev."""
    env = os.environ
    if env.get("GITHUB_REF", "").startswith("refs/tags/") or env.get("GITHUB_EVENT_NAME") == "release":
        return "stable"
    default_branch = ""
    path = env.get("GITHUB_EVENT_PATH")
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                default_branch = ((json.load(fh).get("repository") or {}).get("default_branch")) or ""
        except (OSError, json.JSONDecodeError):
            pass
    return "preview" if env.get("GITHUB_REF_NAME") == (default_branch or "main") else "dev"


# --------------------------------------------------------------------------- transport


class Uploader:
    """rsync to `target` (user@host:path/ or a local directory for testing)."""

    def __init__(self, target, port=22, key=None, known_hosts=None, dry_run=False):
        self.target = target if target.endswith(("/", ":")) else target + "/"
        if self.target.endswith(":"):
            self.target += "./"
        self.remote = ":" in target and not os.path.isabs(target)
        self.dry_run = dry_run
        self.ssh = None
        if self.remote:
            ssh = ["ssh", "-p", str(port), "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes"]
            if key:
                ssh += ["-i", key]
            if known_hosts:
                ssh += ["-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"]
            self.ssh = " ".join(shlex.quote(a) for a in ssh)

    def push(self, local):
        """Copy `local` (a file, or a directory without trailing slash) into the target directory."""
        cmd = ["rsync", "-rltp", "--chmod=D2775,F664"]
        if self.ssh:
            cmd += ["-e", self.ssh]
        cmd += [local, self.target]
        if self.dry_run:
            log("  [dry-run] " + " ".join(shlex.quote(c) for c in cmd))
            return
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise PublishError(f"rsync failed ({res.returncode}): {res.stderr.strip() or res.stdout.strip()}")

    def pull_json(self, name, tmpdir):
        """Read a JSON file from the target directory; None if it doesn't exist."""
        local = os.path.join(tmpdir, f"pulled-{name}")
        if os.path.exists(local):
            os.remove(local)
        cmd = ["rsync", "-t"]
        if self.ssh:
            cmd += ["-e", self.ssh]
        cmd += [self.target + name, local]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            err = (res.stderr or res.stdout).strip()
            if res.returncode == 23 and "No such file" in err:
                return None
            raise PublishError(f"rsync could not read {name} from the server ({res.returncode}): {err}")
        try:
            with open(local, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise PublishError(f"{name} on the server is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------- publish


def parse_builds(lines):
    builds = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PublishError(f"build '{line}' must look like variant=path/to/firmware.bin")
        variant, path = (s.strip() for s in line.split("=", 1))
        if not os.path.isfile(path):
            raise PublishError(f"firmware file not found: {path}")
        builds.append((variant, path))
    if not builds:
        raise PublishError("no builds given")
    return builds


def merge(existing, new_entries, replace):
    """Return the merged build list; raise on conflicting duplicates."""
    by_id = {b.get("id"): b for b in existing if isinstance(b, dict)}
    for entry in new_entries:
        old = by_id.get(entry["id"])
        same_file = old and old.get("sha256") == entry["sha256"]
        same_commit = old and (old.get("source") or {}).get("sha") and \
            (old.get("source") or {}).get("sha") == (entry.get("source") or {}).get("sha")
        if old and not replace and not same_file and not same_commit:
            raise PublishError(
                f"build '{entry['id']}' already exists with a different firmware file. "
                "Use a new version, set replace: true, or remove it with docs-firmware-admin."
            )
        by_id[entry["id"]] = entry
    return sorted(by_id.values(), key=lambda b: str(b.get("date", "")), reverse=True)


def publish(args):
    base = args.base_url.rstrip("/")
    uploader = Uploader(args.target, args.port, args.ssh_key, args.known_hosts, args.dry_run)
    work = tempfile.mkdtemp(prefix="fw-publish-")

    # The target folder must be this product's folder: it holds product.json from the docs build.
    # This also stops a key for one product from being used to publish another product.
    product = uploader.pull_json("product.json", work)
    if product is None:
        raise PublishError(f"no product.json in the target folder. Deploy the docs with '{args.product}' "
                           "in firmware.yml first, and check the key/remote-path points at that product.")
    if product.get("product") != args.product:
        raise PublishError(f"the target folder belongs to '{product.get('product')}', not '{args.product}'. "
                           "Check the product input, the SSH key and remote-path.")
    variants = {v["id"]: v.get("label", v["id"]) for v in product.get("variants", [])}
    channels = {c["id"] for c in product.get("channels", [])}

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    channel = args.channel or default_channel()
    version = (args.version or default_version(now)).strip()
    if channel not in channels:
        raise PublishError(f"channel '{channel}' is not defined in firmware.yml (allowed: {', '.join(sorted(channels))})")
    if not version:
        raise PublishError("version is empty")
    date = args.date or now.isoformat().replace("+00:00", "Z")
    notes = args.notes if args.notes is not None else default_notes()
    source = git_context() if args.record_source else {}
    chip = args.chip or product.get("chip")
    if not chip:
        raise PublishError("chip family unknown (set chip in firmware.yml or pass --chip)")

    entries = []
    with tempfile.TemporaryDirectory() as tmp:
        stage = os.path.join(tmp, "builds")  # uploaded as <target>/builds/<id>/...
        for variant, path in parse_builds(args.build):
            if variant not in variants:
                raise PublishError(f"variant '{variant}' is not defined for {args.product} "
                                   f"in firmware.yml (allowed: {', '.join(variants)})")
            build_id = ID_SAFE.sub("-", f"{variant}-{version}").strip("-.")
            folder = os.path.join(stage, build_id)
            os.makedirs(folder)
            shutil.copyfile(path, os.path.join(folder, "firmware.bin"))
            manifest = {
                "name": f"{product.get('name', args.product)} {variants[variant]}",
                "version": version,
                "funding_url": FUNDING_URL,
                "new_install_prompt_erase": bool(args.erase),
                "builds": [{"chipFamily": chip, "parts": [{"path": "firmware.bin", "offset": 0}]}],
            }
            with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, indent=2)
                fh.write("\n")
            entry = {
                "id": build_id,
                "variant": variant,
                "channel": channel,
                "version": version,
                "date": date,
                "chip": chip,
                "manifest": f"builds/{build_id}/manifest.json",
                "size": os.path.getsize(path),
                "sha256": sha256(path),
                "notes": notes,
            }
            if source:
                entry["source"] = source
            entries.append(entry)
            log(f"• {build_id}: {path} ({entry['size']} bytes, sha256 {entry['sha256'][:12]}…)")

        # Refuse before uploading anything if a build id is already taken by a different file.
        merge((uploader.pull_json("builds.json", work) or {}).get("builds", []), entries, args.replace)

        # 1) firmware files first, so builds.json never points at missing files.
        uploader.push(stage)

        # 2) builds.json: merge against the latest copy; retry if someone else published meanwhile.
        for attempt in range(1, 6):
            current = uploader.pull_json("builds.json", work) or {"builds": []}
            if current.get("product") not in (None, args.product):
                raise PublishError(f"builds.json on the server belongs to '{current.get('product')}'")
            merged = merge(current.get("builds", []), entries, args.replace)
            # Keep any other keys (e.g. "removed"); an explicit publish clears a build's removed record.
            doc = dict(current)
            doc.update({"schema": 1, "product": args.product,
                        "updated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                        "builds": merged})
            new_ids = {e["id"] for e in entries}
            if doc.get("removed"):
                doc["removed"] = [r for r in doc["removed"] if r.get("id") not in new_ids]
            index_path = os.path.join(tmp, "builds.json")
            with open(index_path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
                fh.write("\n")
            again = uploader.pull_json("builds.json", work) or {"builds": []}
            if again.get("updated") != current.get("updated"):
                log(f"  builds.json changed while publishing, merging again (attempt {attempt})")
                continue
            uploader.push(index_path)
            break
        else:
            raise PublishError("builds.json kept changing; another publish may be stuck. Try again.")
    shutil.rmtree(work, ignore_errors=True)

    flasher = f"{base}/firmware/{args.product}/"
    if args.dry_run:
        log(f"Dry run: validated {len(entries)} build(s); nothing was uploaded.")
        return
    log(f"Published {len(entries)} build(s) to {flasher}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"### Firmware published: {product.get('name', args.product)}\n\n")
            fh.write("| Build | Variant | Channel | Version | Flasher |\n|---|---|---|---|---|\n")
            for b in entries:
                link = f"{flasher}?build={b['id']}" + ("&dev" if channel == "dev" else "")
                fh.write(f"| `{b['id']}` | {variants[b['variant']]} | {channel} | {version} | [open]({link}) |\n")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"build-ids={','.join(b['id'] for b in entries)}\n")
            fh.write(f"version={version}\nchannel={channel}\nflasher-url={flasher}\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--product", required=True, help="product key from firmware.yml, e.g. openfan-micro")
    p.add_argument("--build", action="append", default=[], help="variant=path/to/merged-firmware.bin (repeatable)")
    p.add_argument("--channel", default="", help="stable | preview | dev (default: from the git ref)")
    p.add_argument("--version", default="", help="default: tag name, else YYYY.MM.DD-<short sha>")
    p.add_argument("--notes", default=None, help="default: release body or commit message")
    p.add_argument("--date", default="", help="ISO date of the build (default: now)")
    p.add_argument("--chip", default="", help="override chipFamily (default: from product.json)")
    p.add_argument("--erase", action="store_true", help="ask the user to erase the device before installing")
    p.add_argument("--replace", action="store_true", help="overwrite an existing build with the same id")
    p.add_argument("--no-source", dest="record_source", action="store_false", help="don't record git source info")
    p.add_argument("--base-url", default="https://docs.sasakaranovic.com", help="used for links in the summary")
    p.add_argument("--target", required=True, help="rsync target: user@host:[path/] or a local directory")
    p.add_argument("--port", default="22")
    p.add_argument("--ssh-key", default=None)
    p.add_argument("--known-hosts", default=None)
    p.add_argument("--dry-run", action="store_true", help="validate and print rsync commands without uploading")
    args = p.parse_args(argv)
    # Allow newline-separated builds in a single --build (handy from GitHub Actions inputs).
    args.build = [line for item in args.build for line in item.splitlines()]
    try:
        publish(args)
    except PublishError as exc:
        print(f"::error::{exc}" if os.environ.get("GITHUB_ACTIONS") else f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
