#!/usr/bin/env bash
# Build the JRef Android APK on Ubuntu 22.04/24.04 or WSL2 (not on Windows itself: python-for-android needs Linux).
#   bash packaging/android/build_apk.sh            # -> bin/jref-<version>-arm64-v8a-debug.apk
#   bash packaging/android/build_apk.sh deploy     # also installs it on a phone connected by USB (adb, USB debugging on)
# The first build downloads the Android SDK/NDK (about 4 GB in ~/.buildozer) and compiles Python: 30-60 minutes.
set -euo pipefail
cd "$(dirname "$0")/../.."

if ! command -v javac >/dev/null || ! command -v autoconf >/dev/null; then
  echo "Installing the system packages python-for-android needs (sudo) ..."
  sudo apt-get update
  sudo apt-get install -y --no-install-recommends \
    git zip unzip openjdk-17-jdk python3-pip python3-venv autoconf automake autopoint libtool libtool-bin \
    pkg-config cmake gettext zlib1g-dev libncurses5-dev libncursesw5-dev libffi-dev libssl-dev libltdl-dev \
    build-essential ccache
fi

# WSL: build on the Linux file system, not under /mnt/c (file permissions and speed).
case "$(pwd)" in /mnt/*) echo "Note: copy the project into the Linux home (e.g. ~/jref) for a reliable build." ;; esac

python3 -m venv .venv-android
. .venv-android/bin/activate
python -m pip install --upgrade pip
python -m pip install buildozer setuptools "cython==0.29.34" legacy-cgi  # versions of the buildozer guide

if [ "${1:-}" = "deploy" ]; then
  buildozer -v android debug deploy run < /dev/null
else
  buildozer -v android debug < /dev/null
fi
ls -lh bin/*.apk
