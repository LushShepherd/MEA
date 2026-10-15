import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sync_upstream import GitHubReleases, Report, SyncError, main, synchronize, version_key


ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if result.returncode:
        raise AssertionError(f"git {args}: {result.stdout}\n{result.stderr}")
    return result.stdout.strip()


class Releases:
    def __init__(self):
        self.waited = []
        self.blocked = set()

    def preflight(self, publishing):
        self.publishing = publishing

    def wait(self, tag, commit, report, dry_run=False):
        self.waited.append((tag, commit))
        if tag in self.blocked:
            raise SyncError(f"Release {tag} blocked: https://github.com/test/MEA/actions/runs/1")


class SyncIntegrationTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / "build" / "sync-tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / "upstream"
        self.origin = self.root / "origin.git"
        self.fork = self.root / "fork"
        self.upstream.mkdir()
        git(self.upstream, "init", "--quiet", "--initial-branch=main")
        self.identity(self.upstream)
        self.commit(self.upstream, "game.txt", "base\n")
        git(self.upstream, "tag", "v2.7.2")
        git(self.root, "clone", "--quiet", "--no-hardlinks", str(self.upstream), str(self.fork))
        self.identity(self.fork)
        self.commit(self.fork, "android.txt", "MEA additions\n")
        git(self.fork, "tag", "-d", "v2.7.2")
        git(self.fork, "tag", "-a", "v2.7.2", "-m", "MEA stable baseline")
        git(self.root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.origin))
        git(self.fork, "remote", "set-url", "origin", str(self.origin))
        git(self.fork, "push", "--quiet", "origin", "main", "refs/tags/v2.7.2")
        self.releases = Releases()
        self.validated = []

    def identity(self, root):
        git(root, "config", "user.name", "Sync Test")
        git(root, "config", "user.email", "sync-test@example.invalid")
        git(root, "config", "commit.gpgSign", "false")
        git(root, "config", "tag.gpgSign", "false")

    def commit(self, root, filename, content):
        (root / filename).write_text(content, encoding="utf-8")
        git(root, "add", filename)
        git(root, "commit", "--quiet", "-m", f"Update {filename}")
        return git(root, "rev-parse", "HEAD")

    def update_upstream(self, tag=None):
        filename = f"change-{len(list(self.upstream.glob('change-*')))}.txt"
        commit = self.commit(self.upstream, filename, f"Upstream {tag}\n")
        if tag:
            git(self.upstream, "tag", tag)
        return commit

    def validate(self, root):
        self.validated.append(root)
        self.assertEqual((root / "android.txt").read_text(), "MEA additions\n")

    def sync(self, dry_run=False, validator=None):
        return synchronize(
            self.origin, self.upstream, dry_run=dry_run,
            validator=validator or self.validate, releases=self.releases,
        )

    def refs(self):
        return git(self.origin, "show-ref")

    def test_unchanged_run_creates_nothing(self):
        before = self.refs()
        self.sync()
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.validated, [])

    def test_commit_only_sync_preserves_android_and_creates_no_release(self):
        source = self.update_upstream()
        self.sync()
        self.assertEqual(git(self.origin, "show", "main:android.txt"), "MEA additions")
        git(self.origin, "merge-base", "--is-ancestor", source, "main")
        self.assertEqual(git(self.origin, "tag"), "v2.7.2")
        self.assertEqual(self.releases.waited, [])
        self.assertEqual(len(self.validated), 1)
        before = self.refs()
        self.sync()
        self.assertEqual(self.refs(), before)
        self.assertEqual(len(self.validated), 1)

    def test_dry_run_merges_and_validates_without_changing_remote_refs(self):
        self.update_upstream("v2.7.3")
        before = self.refs()
        report = self.sync(dry_run=True)
        self.assertEqual(self.refs(), before)
        self.assertEqual(report.created, [])
        self.assertEqual(len(self.validated), 1)
        self.assertEqual(self.releases.waited, [])

    def test_multiple_tags_have_semver_order_and_increasing_version_codes(self):
        versions = ["v2.8.0-beta.10", "v2.7.3", "v2.8.0", "v2.8.0-beta.2"]
        sources = {tag: self.update_upstream(tag) for tag in versions}
        git(self.upstream, "tag", "not-a-release")
        baseline_object = git(self.origin, "rev-parse", "v2.7.2")
        report = self.sync()
        expected = ["v2.7.3", "v2.8.0-beta.2", "v2.8.0-beta.10", "v2.8.0"]
        self.assertEqual(report.created, expected)
        self.assertEqual([tag for tag, _ in self.releases.waited], expected)
        counts = [int(git(self.origin, "rev-list", "--count", tag)) for tag in expected]
        self.assertEqual(counts, list(range(counts[0], counts[0] + len(expected))))
        for tag in versions:
            self.assertNotEqual(git(self.origin, "rev-parse", f"{tag}^{{commit}}"), sources[tag])
            self.assertEqual(git(self.origin, "show", f"{tag}:android.txt"), "MEA additions")
            annotation = git(self.origin, "for-each-ref", "--format=%(contents)", f"refs/tags/{tag}")
            self.assertIn(f"Upstream-Commit: {sources[tag]}", annotation)
        self.assertEqual(git(self.origin, "rev-parse", "v2.7.2"), baseline_object)
        before = self.refs()
        self.sync()
        self.assertEqual(self.refs(), before)
        self.assertEqual(len(self.validated), 1)

    def test_existing_fork_tag_is_preserved_even_when_upstream_points_elsewhere(self):
        git(self.fork, "tag", "-a", "v2.7.3", "-m", "Previously published MEA release")
        git(self.fork, "push", "--quiet", "origin", "refs/tags/v2.7.3")
        existing = git(self.origin, "rev-parse", "v2.7.3")
        self.update_upstream("v2.7.3")
        self.sync()
        self.assertEqual(git(self.origin, "rev-parse", "v2.7.3"), existing)
        self.assertEqual(self.releases.waited, [])

    def test_conflict_aborts_before_any_remote_changes(self):
        self.commit(self.fork, "game.txt", "MEA game edit\n")
        git(self.fork, "push", "--quiet", "origin", "main")
        self.commit(self.upstream, "game.txt", "Upstream game edit\n")
        git(self.upstream, "tag", "v2.7.3")
        before = self.refs()
        with self.assertRaisesRegex(SyncError, "game.txt"):
            self.sync()
        self.assertEqual(self.refs(), before)
        self.assertEqual(self.validated, [])

    def test_validation_failure_aborts_before_any_remote_changes(self):
        self.update_upstream("v2.7.3")
        before = self.refs()

        def fail(root):
            raise SyncError("Packaging validation failed")

        with self.assertRaisesRegex(SyncError, "validation failed"):
            self.sync(validator=fail)
        self.assertEqual(self.refs(), before)

    def test_concurrent_main_update_is_preserved_without_pushing_tag(self):
        self.update_upstream("v2.7.3")
        concurrent = []

        def update_origin(root):
            self.validate(root)
            concurrent.append(self.commit(self.fork, "concurrent.txt", "User work\n"))
            git(self.fork, "push", "--quiet", "origin", "main")

        with self.assertRaisesRegex(SyncError, "changed during this run"):
            self.sync(validator=update_origin)
        self.assertEqual(git(self.origin, "rev-parse", "main"), concurrent[0])
        self.assertEqual(git(self.origin, "tag"), "v2.7.2")

    def test_tag_collision_rejects_atomic_branch_update(self):
        self.update_upstream("v2.7.3")
        before = git(self.origin, "rev-parse", "main")

        def competing_tag(root):
            git(self.fork, "tag", "v2.7.3")
            git(self.fork, "push", "--quiet", "origin", "refs/tags/v2.7.3")

        with self.assertRaisesRegex(SyncError, "push failed"):
            self.sync(validator=competing_tag)
        self.assertEqual(git(self.origin, "rev-parse", "main"), before)
        self.assertEqual(git(self.origin, "rev-parse", "v2.7.3"), before)

    def test_failed_release_retains_tag_and_blocks_later_versions_until_recovered(self):
        self.update_upstream("v2.7.3")
        self.update_upstream("v2.7.4")
        self.releases.blocked.add("v2.7.3")
        with self.assertRaisesRegex(SyncError, "Release v2.7.3 blocked"):
            self.sync()
        before = self.refs()
        self.assertIn("refs/tags/v2.7.3", before)
        self.assertNotIn("refs/tags/v2.7.4", before)
        with self.assertRaisesRegex(SyncError, "Release v2.7.3 blocked"):
            self.sync()
        self.assertEqual(self.refs(), before)
        self.releases.blocked.clear()
        report = self.sync()
        self.assertEqual(report.created, ["v2.7.4"])
        self.assertGreater(int(git(self.origin, "rev-list", "--count", "v2.7.4")),
                           int(git(self.origin, "rev-list", "--count", "v2.7.3")))

    def test_moved_upstream_managed_tag_requires_investigation(self):
        self.update_upstream("v2.7.3")
        self.sync()
        before = self.refs()
        new_commit = self.update_upstream()
        git(self.upstream, "tag", "-f", "v2.7.3", new_commit)
        with self.assertRaisesRegex(SyncError, "Upstream moved managed tag"):
            self.sync()
        self.assertEqual(self.refs(), before)

    def test_tag_outside_upstream_main_is_not_released(self):
        git(self.upstream, "checkout", "--quiet", "-b", "other-release")
        self.update_upstream("v2.7.3")
        git(self.upstream, "checkout", "--quiet", "main")
        before = self.refs()
        with self.assertRaisesRegex(SyncError, "outside upstream/main"):
            self.sync()
        self.assertEqual(self.refs(), before)

    def test_detached_release_with_higher_version_code_blocks_publishing(self):
        git(self.fork, "checkout", "--quiet", "-b", "detached-release")
        for number in range(6):
            self.commit(self.fork, "detached.txt", f"Release commit {number}\n")
        git(self.fork, "tag", "v2.7.5")
        git(self.fork, "push", "--quiet", "origin", "refs/tags/v2.7.5")
        self.update_upstream("v2.7.3")
        before = self.refs()
        with self.assertRaisesRegex(SyncError, "versionCode .* would not exceed"):
            self.sync()
        self.assertEqual(self.refs(), before)


class ReleaseGateTests(unittest.TestCase):
    def test_publishing_requires_switch_token_and_signing_prerequisites(self):
        ready = {"UPSTREAM_SYNC_ENABLED": "true", "UPSTREAM_SYNC_TOKEN": "test-token", "MEA_SIGNING_READY": "true"}
        for missing in ready:
            environment = {key: value for key, value in ready.items() if key != missing}
            with self.subTest(missing=missing), patch.dict(os.environ, environment, clear=True):
                with patch("scripts.sync_upstream.sys.argv", ["sync_upstream.py", "--publish"]):
                    with patch("scripts.sync_upstream.synchronize") as sync:
                        self.assertEqual(main(), 1)
                        sync.assert_not_called()

    def test_repository_api_url_has_no_trailing_slash(self):
        client = GitHubReleases("test/MEA", "test-token")
        with patch("scripts.sync_upstream.urlopen", return_value=io.BytesIO(b'{"permissions": {}}')) as request:
            client.get("")
            self.assertEqual(request.call_args.args[0].full_url, "https://api.github.com/repos/test/MEA")

    def client(self, runs):
        client = GitHubReleases("test/MEA", "test-token", timeout=0, interval=0)
        client.get = lambda path: {"workflow_runs": runs}
        return client

    def run_data(self, **overrides):
        return dict(id=1, head_sha="abc", head_branch="v2.7.3", status="completed",
                    conclusion="success", html_url="https://github.com/test/MEA/actions/runs/1", **overrides)

    def test_only_matching_release_tag_and_commit_can_pass(self):
        correct = self.run_data()
        wrong = dict(correct, id=2, head_branch="main", conclusion="failure")
        self.client([wrong, correct]).wait("v2.7.3", "abc", Report())

    def test_failed_release_includes_recovery_link(self):
        run = dict(self.run_data(), conclusion="failure")
        with self.assertRaisesRegex(SyncError, "actions/runs/1"):
            self.client([run]).wait("v2.7.3", "abc", Report())

    def test_missing_or_running_release_blocks_next_version(self):
        for runs in ([], [dict(self.run_data(), status="in_progress", conclusion=None)]):
            with self.subTest(runs=runs), self.assertRaisesRegex(SyncError, "unfinished"):
                self.client(runs).wait("v2.7.3", "abc", Report())

    def test_dry_run_does_not_wait_for_an_unfinished_release(self):
        client = self.client([])
        client.timeout = 7200
        with patch("scripts.sync_upstream.time.sleep") as sleep:
            with self.assertRaisesRegex(SyncError, "unfinished"):
                client.wait("v2.7.3", "abc", Report(), dry_run=True)
            sleep.assert_not_called()

    def test_latest_rerun_result_is_used(self):
        failed = dict(self.run_data(), conclusion="failure")
        successful = dict(failed, id=2, conclusion="success")
        self.client([successful, failed]).wait("v2.7.3", "abc", Report())

    def test_semver_precedence(self):
        tags = ["v2.8.0", "v2.8.0-beta.10", "v2.8.0-beta.2", "v2.7.3"]
        self.assertEqual(sorted(tags, key=version_key), list(reversed(tags)))
        self.assertEqual(version_key("v2.8.0+build.2"), version_key("v2.8.0"))
        for invalid in ("main", "v2.8", "v2.8.0-beta.01", "v02.8.0"):
            self.assertIsNone(version_key(invalid))


if __name__ == "__main__":
    unittest.main()
