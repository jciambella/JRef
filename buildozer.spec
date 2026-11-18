# JRef for Android: build recipe for buildozer / python-for-android.
#
#   Cloud build:  push the folder to a (private) GitHub repository; the workflow .github/workflows/android.yml
#                 builds the APK and attaches it to the run ("Artifacts").
#   Local build:  bash packaging/android/build_apk.sh        (Ubuntu or WSL2; see ANDROID.md)
#
# The app is the package bibmanager/mobile (Kivy) on top of bibmanager/core; the desktop interface
# (bibmanager/gui, PySide6) is not packaged.

[app]
title = JRef
package.name = jref
package.domain = org.jref
source.dir = .
source.include_exts = py,kv,png,json
source.exclude_dirs = tests, bin, dist, build, packaging, .github, .buildozer, .venv, venv, bibmanager/gui
version.regex = __version__ = ['"](.*)['"]
version.filename = %(source.dir)s/bibmanager/__init__.py

# python3/kivy/sqlite3/openssl/pyjnius/android have python-for-android recipes; the others are pure Python
# (httpx and its dependencies are listed explicitly: p4a does not resolve dependencies of pip packages).
# PyMuPDF has no Android build: bibmanager.core.extractor falls back to pypdf.
requirements = python3,kivy,sqlite3,openssl,pyjnius,android,httpx,httpcore,h11,anyio,sniffio,idna,certifi,typing_extensions,unidecode,pypdf

orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/bibmanager/mobile/assets/icon.png
presplash.filename = %(source.dir)s/bibmanager/mobile/assets/presplash.png
android.presplash_color = #141C2E

# The library is a folder owned by the sync app (ordinary files: PDFs, journals): JRef needs "All files access"
# (Android 11+) or the classic storage permissions (Android <= 10). Fine for an APK installed by hand; Google Play
# would ask for a justification of MANAGE_EXTERNAL_STORAGE.
android.permissions = android.permission.INTERNET, android.permission.MANAGE_EXTERNAL_STORAGE, android.permission.READ_EXTERNAL_STORAGE, android.permission.WRITE_EXTERNAL_STORAGE

# "Share" / "Open with" for PDFs from a browser or a file manager; singleTask delivers them to the running app.
android.manifest.intent_filters = packaging/android/intent_filters.xml
android.manifest.launch_mode = singleTask

# android.api / android.minapi / android.ndk: buildozer's defaults (a recent SDK/NDK pair known to work together).
android.archs = arm64-v8a
android.accept_sdk_license = True
android.allow_backup = False
android.logcat_filters = *:S python:D

# python-for-android: buildozer's default (branch master = release v2026.05.09) cannot build this app. Its pip
# stage resolves charset-normalizer (pulled in by requests, a dependency of the kivy recipe) to the Android wheel
# now on PyPI, then installs it with the build machine's pip, which rejects it ("... android_24_arm64_v8a.whl is
# not a supported wheel on this platform"; p4a issues #3364 and #3411). Fixed on develop by p4a PR #3366, not
# released yet. Pinned to a develop commit for reproducible builds; return to a release tag once one has the fix.
p4a.branch = develop
p4a.commit = 9562d1b4f306d0af1e76991c711114010dea4152

[buildozer]
log_level = 2
warn_on_root = 0
