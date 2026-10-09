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

## Release versions

MEA release versions follow the upstream MaaGF2Exilium release number. The first
test release on the upstream `v2.7.2` baseline is `v2.7.2-beta.1`. This baseline
also includes the later activity-appearance and cooking-swipe resource fixes
already present in MEA, plus MEA's Android packaging changes.

Use `v<upstream-version>-beta.<number>` for test releases and increment the beta
number for each new test release. Tags with a prerelease suffix are published as
GitHub prereleases and are offered by the Android beta update channel. Stable
releases use `v<upstream-version>`.

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
