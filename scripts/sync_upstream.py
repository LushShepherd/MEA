"""Merge upstream in a disposable checkout and publish serialized MEA releases.

Remote writes require --publish. The default dry run still merges and validates,
but does not push refs or start release workflows.
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from .android_version import parse_version
except ImportError:
    from android_version import parse_version


ORIGIN = "https://github.com/LushShepherd/MEA.git"
UPSTREAM = "https://github.com/DarkLingYun/MaaGF2Exilium.git"
BASELINE = "v2.7.2"
MARKER = "MEA-Upstream-Sync: 1"


class SyncError(RuntimeError):
    pass


def version_key(tag):
    """SemVer precedence; build metadata does not affect ordering."""
    match = parse_version(tag) if tag.startswith("v") else None
    if not match:
        return None
    pre = match[4]
    identifiers = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return (int(match[1]), int(match[2]), int(match[3]), not bool(pre), identifiers)


@dataclass
class Report:
    lines: list = field(default_factory=list)
    created: list = field(default_factory=list)

    def add(self, message):
        self.lines.append(message)
        print(message, flush=True)

    def write(self):
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as output:
                output.write("## MEA upstream sync\n\n" + "\n\n".join(self.lines) + "\n")


class Git:
    def __init__(self, root, push_env=None):
        self.root = Path(root)
        self.push_env = push_env

    def run(self, *args, check=True, publish=False):
        result = subprocess.run(
            ["git", *args], cwd=self.root, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.push_env if publish else None,
        )
        if check and result.returncode:
            raise SyncError(f"git {args[0]} failed:\n{result.stdout}{result.stderr}")
        return result

    def output(self, *args):
        return self.run(*args).stdout.strip()

    def commit(self, ref="HEAD"):
        return self.output("rev-parse", f"{ref}^{{commit}}")

    def count(self, ref="HEAD"):
        return int(self.output("rev-list", "--count", ref))

    def tags(self, namespace):
        refs = self.output("for-each-ref", "--format=%(refname)", namespace).splitlines()
        return {ref.removeprefix(namespace): ref for ref in refs}

    def push(self, expected_head, commit, tag=None):
        remote = self.output("ls-remote", "--refs", "origin", "refs/heads/main").split()
        if not remote or remote[0] != expected_head:
            raise SyncError("origin/main changed during this run; nothing further was pushed. Rerun the sync.")
        refs = [f"{commit}:refs/heads/main"]
        if tag:
            refs.append(f"refs/tags/{tag}:refs/tags/{tag}")
        self.run("push", "--atomic", "origin", *refs, publish=True)


class GitHubReleases:
    def __init__(self, repository, token, timeout=7200, interval=30):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise SyncError("Repository must be owner/name.")
        self.repository = repository
        self.token = token
        self.timeout = timeout
        self.interval = interval
        self.workflow_url = f"https://github.com/{repository}/actions/workflows/install.yml"

    def get(self, path):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "MEA-upstream-sync"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        url = f"https://api.github.com/repos/{self.repository}" + (f"/{path}" if path else "")
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            raise SyncError(f"GitHub API returned {error.code} for {path or self.repository}; check token permissions and expiry.") from error
        except (URLError, TimeoutError) as error:
            raise SyncError(f"Cannot reach GitHub: {error}") from error

    def preflight(self, publishing):
        repository = self.get("")
        if publishing and not repository.get("permissions", {}).get("push", False):
            raise SyncError("UPSTREAM_SYNC_TOKEN does not have push access to MEA.")
        workflow = self.get("actions/workflows/install.yml")
        if workflow["state"] != "active":
            raise SyncError(f"The install release workflow is not active: {self.workflow_url}")

    def wait(self, tag, commit, report, dry_run=False):
        deadline = time.monotonic() + self.timeout
        reported_url = None
        while True:
            query = urlencode({"event": "push", "head_sha": commit, "per_page": 100})
            runs = self.get(f"actions/workflows/install.yml/runs?{query}")["workflow_runs"]
            matching = [r for r in runs if r["head_sha"] == commit and r["head_branch"] == tag]
            if matching:
                run = max(matching, key=lambda item: item["id"])
                url = run["html_url"]
                if url != reported_url:
                    report.add(f"Release `{tag}`: {url}")
                    reported_url = url
                if run["status"] == "completed":
                    if run["conclusion"] != "success":
                        raise SyncError(
                            f"Release {tag} ended with {run['conclusion']}: {url}. "
                            "Rerun that release workflow, then rerun sync. Keep the tag unchanged."
                        )
                    return
            if dry_run or time.monotonic() >= deadline:
                raise SyncError(
                    f"Release {tag} is unfinished or has not started: {reported_url or self.workflow_url}. "
                    "No further versions will be published; rerun sync after it succeeds."
                )
            time.sleep(self.interval)


def validate(root):
    git = Git(root)
    git.run("submodule", "update", "--init", "--recursive")
    # Validation runs merged code, without passing publishing credentials to it.
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ("UPSTREAM_SYNC_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
        and not key.startswith("GIT_CONFIG_")
    }
    environment["PYTHONUTF8"] = "1"
    for command in (
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_android_packaging.py", "-v"],
        [sys.executable, "check_resource.py"],
    ):
        result = subprocess.run(command, cwd=root, env=environment)
        if result.returncode:
            raise SyncError(f"Validation failed: {' '.join(command)}. No changes were pushed.")


def synchronize(origin=ORIGIN, upstream=UPSTREAM, *, dry_run=True, validator=validate,
                releases, report=None, push_env=None):
    report = report if report is not None else Report()
    report.add("Mode: **dry run** (no remote writes)." if dry_run else "Mode: **publish**.")
    releases.preflight(not dry_run)
    with tempfile.TemporaryDirectory(prefix="mea-upstream-sync-") as temporary:
        git = Git(temporary, push_env)
        git.run("init", "--quiet", "--initial-branch=main")
        git.run("config", "user.name", "github-actions[bot]")
        git.run("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        git.run("config", "commit.gpgSign", "false")
        git.run("config", "tag.gpgSign", "false")
        git.run("remote", "add", "origin", str(origin))
        git.run("remote", "add", "upstream", str(upstream))
        git.run("fetch", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main", "refs/tags/*:refs/tags/*")
        git.run("fetch", "--no-tags", "upstream", "refs/heads/main:refs/remotes/upstream/main",
                "refs/tags/v*:refs/upstream-tags/v*")
        git.run("checkout", "--quiet", "-B", "main", "refs/remotes/origin/main")
        original = git.commit()
        destination = git.tags("refs/tags/")
        source = git.tags("refs/upstream-tags/")
        baseline = version_key(BASELINE)
        ordered = sorted((name for name in source if version_key(name) is not None),
                         key=lambda name: (version_key(name), name))
        pending = []
        historical = 0
        for name in ordered:
            if version_key(name) <= baseline:
                historical += 1
            elif name in destination:
                report.add(f"Skipped existing MEA tag `{name}` (preserved unchanged).")
            else:
                pending.append(name)
        report.add(f"Skipped {historical} upstream tags at or below the `{BASELINE}` baseline.")

        # Recover after a crash, timeout or failed release, even if no tags are missing.
        for name in sorted(destination, key=lambda n: (version_key(n) or (), n)):
            if version_key(name) is None or version_key(name) <= baseline:
                continue
            contents = git.output("for-each-ref", "--format=%(contents)", destination[name])
            if MARKER in contents.splitlines():
                recorded = next((line.removeprefix("Upstream-Commit: ") for line in contents.splitlines()
                                 if line.startswith("Upstream-Commit: ")), None)
                if name in source and git.commit(source[name]) != recorded:
                    raise SyncError(f"Upstream moved managed tag {name}; manual investigation is required. MEA tags were preserved.")
                releases.wait(name, git.commit(destination[name]), report, dry_run=dry_run)

        for name in pending:
            if git.run("merge-base", "--is-ancestor", git.commit(source[name]), "refs/remotes/upstream/main",
                       check=False).returncode:
                raise SyncError(f"Upstream tag {name} is outside upstream/main; refusing to label unrelated code.")
        merge = git.run("merge", "--no-edit", "--no-ff", "refs/remotes/upstream/main", check=False)
        if merge.returncode:
            conflicts = git.output("diff", "--name-only", "--diff-filter=U")
            git.run("merge", "--abort", check=False)
            raise SyncError(f"Upstream merge failed. No changes were pushed.\n{conflicts or merge.stderr}")
        merged = git.commit()
        commits = git.output("log", "--format=%h %s", f"{original}..{merged}")
        report.add("Merged commits:\n\n" + "\n".join(f"- {line}" for line in commits.splitlines())
                   if commits else "No upstream commits to merge.")
        if merged == original and not pending:
            report.add("Already up to date; no commits or tags created.")
            return report

        maximum_count = max([git.count(original)] + [git.count(ref) for name, ref in destination.items()
                                                    if version_key(name) is not None])
        planned = []
        for name in pending:
            source_commit = git.commit(source[name])
            git.run("commit", "--quiet", "--allow-empty", "-m", f"chore(release): sync upstream {name}")
            commit = git.commit()
            count = git.count()
            if count <= maximum_count:
                raise SyncError(f"Android versionCode {count} would not exceed {maximum_count}. No changes were pushed.")
            maximum_count = count
            git.run("tag", "-a", name, "-m", f"MEA {name}: upstream sync\n\n{MARKER}\nUpstream-Tag: {name}\nUpstream-Commit: {source_commit}")
            planned.append((name, commit))
            report.add(f"Planned `{name}` on MEA `{commit[:12]}`; upstream `{source_commit[:12]}`; Android versionCode `{count}`.")

        validator(Path(temporary))
        report.add("Android packaging tests and resource validation passed.")
        if dry_run:
            report.add("Dry run complete. No branches or tags were pushed and no releases were started.")
            return report
        expected = original
        if not planned:
            git.push(expected, merged)
            report.add(f"Updated `main` to `{merged[:12]}` without creating a release.")
        for name, commit in planned:
            git.push(expected, commit, name)
            expected = commit
            report.created.append(name)
            report.add(f"Pushed `main` and `{name}` atomically; the tag is now permanent.")
            releases.wait(name, commit, report)
        return report


def publishing_environment(token):
    environment = os.environ.copy()
    environment["GIT_CONFIG_COUNT"] = "1"
    environment["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    environment["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {credentials}"
    return environment


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Merge and validate without pushing (default).")
    mode.add_argument("--publish", action="store_true", help="Push validated changes and release tags.")
    args = parser.parse_args()
    report = Report()
    try:
        token = os.environ.get("UPSTREAM_SYNC_TOKEN", "")
        if args.publish:
            if os.environ.get("UPSTREAM_SYNC_ENABLED") != "true":
                raise SyncError("Publishing is disabled. Review a dry run, then set UPSTREAM_SYNC_ENABLED=true.")
            if not token:
                raise SyncError("Configure the UPSTREAM_SYNC_TOKEN Actions secret before publishing.")
            if os.environ.get("MEA_SIGNING_READY") != "true":
                raise SyncError("Configure KEYSTORE_BASE64, KEYSTORE_PASSWORD, KEY_ALIAS and KEY_PASSWORD before publishing.")
        elif not token or os.environ.get("MEA_SIGNING_READY") != "true":
            report.add("Setup incomplete: configure the sync token and all four signing secrets before publishing.")
        releases = GitHubReleases("LushShepherd/MEA", token or os.environ.get("GH_TOKEN", ""))
        synchronize(dry_run=not args.publish, releases=releases, report=report,
                    push_env=publishing_environment(token) if args.publish else None)
        return 0
    except (SyncError, OSError) as error:
        report.add(f"**Sync stopped:** {error}")
        report.add("Published refs, if any, are retained. Inspect the failure before rerunning.")
        return 1
    finally:
        report.write()


if __name__ == "__main__":
    sys.exit(main())
