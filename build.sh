#!/usr/bin/env bash
# Builds dist/CTC-Agent-<version>-<arch>.dmg containing "CTC Agent.app".
#
# Optional environment variables:
#   PYTHON             Python used for the build venv (default: python3)
#   CODESIGN_IDENTITY  "Developer ID Application: ..." identity to sign with
#   NOTARY_PROFILE     notarytool keychain profile; notarizes and staples the DMG
set -euo pipefail
cd "$(dirname "$0")"

VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ctc_agent.py)
ARCH=$(uname -m)
APP="dist/CTC Agent.app"
DMG="dist/CTC-Agent-$VERSION-$ARCH.dmg"

if [[ ! -f credentials.json ]]; then
  echo "warning: credentials.json not found; the app will not include an OAuth client and each" >&2
  echo "         user must place their own in ~/Library/Application Support/ctc-agent/." >&2
fi

echo "==> Setting up build environment"
[[ -d .venv-build ]] || "${PYTHON:-python3}" -m venv .venv-build
.venv-build/bin/pip install -q --upgrade pip
.venv-build/bin/pip install -q -r requirements.txt -r requirements-build.txt

echo "==> Running tests"
.venv-build/bin/python -W ignore -m unittest -q

echo "==> Building $APP"
.venv-build/bin/pyinstaller --noconfirm --clean --log-level WARN ctc_agent.spec

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Signing with $CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp --sign "$CODESIGN_IDENTITY" "$APP"
fi
codesign --verify --deep --strict "$APP"

echo "==> Creating $DMG"
STAGE=build/dmg
rm -rf "$STAGE" "$DMG"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
hdiutil create -quiet -volname "CTC Agent" -srcfolder "$STAGE" -format UDZO "$DMG"

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$CODESIGN_IDENTITY" "$DMG"
fi
if [[ -n "${NOTARY_PROFILE:-}" ]]; then
  echo "==> Notarizing"
  xcrun notarytool submit "$DMG" --keychain-profile "$NOTARY_PROFILE" --wait
  xcrun stapler staple "$DMG"
fi

echo "==> Done: $DMG ($(du -h "$DMG" | cut -f1))"
