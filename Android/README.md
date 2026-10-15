# MEA for Android

MEA uses the same Android packaging design as M9A: the pinned
[MaaFwApp](https://github.com/Aliothmoon/MaaFwApp) submodule is the frontend,
and [MaaFramework](https://github.com/MaaXYZ/MaaFramework) provides the native
automation engine. `Android/build.json` pins the Android engine version.
The desktop Python lockfile is a separate development environment.

## GitHub Actions

**Build Android APK** runs on relevant pushes to `main` / `master` and pull
requests. Its debug artifact contains an arm64-v8a APK. Run it manually with
`assemble: release` to produce three signed artifacts without publishing:

- `MEA-android-universal-<version>.apk` (arm64-v8a and x86_64)
- `MEA-android-arm64-v8a-<version>.apk`
- `MEA-android-x86_64-<version>.apk`

Configure these repository Actions secrets before requesting a release build:
`KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, and `KEY_PASSWORD`.
`KEYSTORE_BASE64` is the base64 encoding of your Android signing keystore.
Keep the same signing key for later updates. Debug builds need no secrets.

Pushing a `v*` tag invokes the reusable `android-build.yml` from `install.yml`.
The release job waits for desktop, resource-update, and Android builds, then
uploads the desktop ZIPs and all three APKs to the same GitHub Release.
The existing MirrorChyan workflow continues to upload desktop/resource packages;
Android APK updates use this repository's GitHub Releases.

## Daily upstream synchronization

**Sync upstream and release MEA** checks `DarkLingYun/MaaGF2Exilium:main`
daily at **09:00 America/Los_Angeles**, following daylight-saving time. GitHub
may delay scheduled runs. The job runs on GitHub; your computer can be off.
Only this MEA repository is synchronized. MaaFwApp and MaaFramework stay pinned
to the versions in the existing release configuration.

Publishing is disabled until repository variable `UPSTREAM_SYNC_ENABLED` is
`true`. Set it under **Settings → Secrets and variables → Actions → Variables**
only after reviewing a successful dry run.

### Initial setup

1. Create a fine-grained personal access token owned by `LushShepherd`, restricted
   to **Only select repositories → MEA**. Grant repository permissions
   **Contents: Read and write**, **Workflows: Read and write**, and
   **Actions: Read-only**. Workflows permission is needed when upstream changes
   workflow files. Set an expiry and renew the token before it expires.
2. Save the token as repository Actions secret `UPSTREAM_SYNC_TOKEN`. Keep it out
   of source files and logs. The built-in `GITHUB_TOKEN` cannot replace it for
   pushing tags: those pushes do not trigger the existing release workflow.
3. Configure the four Android signing secrets listed above. Use the same signing
   key as the published MEA APKs.
4. Check **Settings → Rules → Rulesets** and branch protection for `main` and
   `v*` tags. The token's account must be allowed to make ordinary merge-commit
   pushes and create tags. The sync never force-pushes or bypasses rules. Required
   pull requests, linear history, signed commits, or checks that prevent these
   pushes require a compatible repository policy before publishing can work.
5. Open **Actions → Sync upstream and release MEA → Run workflow**, leave
   **dry_run** selected, and inspect the summary. It performs the merge, Android
   packaging tests, and resource validation in a temporary checkout. It does not
   update branches, push tags, or start releases. Missing publishing credentials
   are reported; configure them before enabling publishing.
6. Set `UPSTREAM_SYNC_ENABLED=true`. To catch up immediately, run the workflow
   again with **dry_run** cleared; otherwise wait for the daily schedule.

The first publishing run considers upstream SemVer tags newer than `v2.7.2`,
including `v2.7.3` and prereleases. Older tags are not backfilled. Existing MEA
tags are preserved even when they differ from upstream. Upstream tags are fetched
into a separate namespace so they cannot overwrite MEA's release tags.

The job merges the current upstream `main`, so each new release includes that
merged snapshot, which can contain commits after the upstream tag. It preserves
MEA's Android additions and creates one release marker commit per new tag. This
keeps Android `versionCode` increasing even when several versions have the same
resources. The annotation records the upstream commit. Tags outside upstream
`main` require manual investigation rather than releasing unrelated code.

New versions are pushed in SemVer order, one tag at a time, together with `main`
in an atomic push. Each tag triggers the existing desktop and signed APK release
workflow. The sync waits for it to succeed before publishing the next tag. When
there are only commits, it updates `main` without creating a release. Unchanged
runs create no commits.

### Failures and recovery

- A merge conflict or validation failure stops the job before any push. The
  summary lists conflicting files. Resolve and merge the upstream changes
  manually, then rerun with **dry_run** selected before publishing.
- If `main` changes concurrently or repository rules reject a push, the sync
  stops without forcing an update. Inspect the summary and rerun against the
  current branch after correcting the cause.
- A failed or unfinished release blocks later versions. Follow the release-run
  link in the summary, fix the underlying problem, and rerun the **existing
  install workflow run**. Once it succeeds, rerun the sync. Published tags are
  permanent: do not delete or move them to retry a build. The sync recognizes its
  annotated tags and checks unfinished releases again after a crash or timeout.
- An expired token or missing signing secret stops publishing. Replace
  `UPSTREAM_SYNC_TOKEN` or restore the signing secret and rerun. To pause future
  publishing, set `UPSTREAM_SYNC_ENABLED=false`; an already-running job continues.
- GitHub disables scheduled workflows in public repositories after 60 days
  without repository activity. Check the Actions page if daily runs stop and
  re-enable the workflow when necessary. Configure GitHub Actions failure
  notifications for the account responsible for this schedule.

For a local read-only preview, install `maafw` and run
`python scripts/sync_upstream.py --dry-run`. Publishing is intended to run through
the guarded Actions workflow. Neither mode changes your working checkout.

## Release versions

MEA release versions follow the upstream MaaGF2Exilium release number. The first
test release on the upstream `v2.7.2` baseline is `v2.7.2-beta.1`. This baseline
also includes the later activity-appearance and cooking-swipe resource fixes
already present in MEA, plus MEA's Android packaging changes.

Use `v<upstream-version>-beta.<number>` for test releases and increment the beta
number for each new test release. Tags with a prerelease suffix are published as
GitHub prereleases and are offered by the Android beta update channel. Stable
releases use `v<upstream-version>`.

The stable `v2.7.2` release promotes the device-tested `v2.7.2-beta.1` functionality
without changes to task resources or Android packaging. Its release documentation
commit increases the APK `versionCode` from 654 to 655, allowing installation over
the beta. Use the same signing key and select the stable update channel for future
stable releases.

Every new APK release must have a higher `versionCode` than the previous APK.
The Android build currently derives it from the full MEA commit count; compare
the new commit count with the previous release before tagging, especially after
a rebase. Changing only the tag on the same commit does not increase it. Keep
published tags unchanged and push only the intended MEA release tag.

## Local build

Install JDK 25, Android SDK, NDK 29.0.13113456, CMake 3.22.1, and Python 3.13.
The Android shell requires Android SDK platform 37; it runs on Android 9 or newer.

From the MEA root:

```powershell
git submodule update --init --recursive
python -m unittest discover -s tests -p test_android_packaging.py -v
python scripts/prepare_android.py --version v0.0.0-dev --repository LushShepherd/MEA
```

Read `maafw_tag` from `Android/build.json` (currently `v5.14.2`), then deploy
the native libraries from inside the shell directory:

```powershell
cd Android/MaaFwApp
python scripts/setup_maa_framework.py --abi arm64-v8a --tag v5.14.2
```

Create `Android/MaaFwApp/local.properties`:

```properties
sdk.dir=C:/path/to/Android/Sdk
pi.profile=../profile.yaml
build.debugAbi=arm64-v8a
build.ndkVersion=29.0.13113456
build.versionName=0.0.0-dev
```

Then, from `Android/MaaFwApp`:

```powershell
.\gradlew.bat :app:assembleDebug
# Or install directly onto a connected Android device:
.\gradlew.bat :app:installDebug
```

The APK appears under `app/build/outputs/apk/debug/`. For a universal release,
also deploy `--abi x86_64`, set `build.releaseAbi=arm64-v8a,x86_64`, provide
the shell's signing environment variables (`KEYSTORE_PATH`,
`KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD`), and run `:app:assembleRelease`.

Use a SemVer value for `build.versionName`; a bare commit hash cannot be parsed
by the updater. To calculate a version from your checkout, run
`python scripts/android_version.py` from the MEA root and use its output.
GitHub Actions applies this version to both the APK and packaged interface.
Without tags, builds use `0.0.0-dev.<commit-count>+g<commit-hash>`; a release
tag such as `v1.2.3` produces `1.2.3`.

Rerun `prepare_android.py` after changing MEA resources or translations.
`Android/assets/` is generated and ignored by Git. The script copies both Android
language packs and the same ppocr_v6 medium OCR models used by the desktop installer.
It leaves the original desktop interface intact.

The generated Android interface uses resource paths relative to its installed
PI directory (for example, `resource/base`), because MaaFwApp does not expand
the desktop `{PROJECT_DIR}` placeholder. When installing rebuilt resources,
use a higher APK `versionCode`: MaaFwApp uses it to decide when to unpack the
bundled resources again.

## On a device

Install the APK, allow notifications and battery/background permissions, then
connect Shizuku or root in the app. Choose the installed game server and the
Chinese or English resource pack. The pipelines assume a landscape 16:9 game
display; use a matching virtual-display preset when necessary. Sign into the
game manually before starting automation.

The Android package excludes the PC controller and PC resource pack. It also
excludes **Community daily operations**: MEA declares `CommunityDailyAction`
but its current agent does not implement it. All other game tasks and server
options are retained. No Python runtime is bundled. If a future pipeline adds
another custom action or recognition, packaging fails with an explicit message;
add an Android agent runtime before enabling that feature.

Packaging checks do not replace testing game recognition and input on a physical
device. Confirm the desired server and tasks on your device before relying on
unattended runs.
