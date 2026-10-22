#!/bin/sh
# Release devpad to npm.
#
#   ./release.sh              # checks -> prompt for 2FA code -> publish
#   ./release.sh --check      # checks only, no publish
#   ./release.sh --otp 123456 # publish with a code supplied up front
#
# The 2FA code is requested only at the final step, on purpose: the full check
# run takes minutes and an OTP lives about 30 seconds, so anything supplied
# earlier would expire before it could be used.
#
# This refuses to publish from a red test run or an unauthenticated machine,
# because a broken version cannot be cleanly retracted: npm restricts unpublish
# and the name stays reserved.
set -eu

cd "$(dirname "$0")"

CHECK_ONLY=0
OTP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --otp)
      OTP="${2:-}"
      [ -n "$OTP" ] || { echo "--otp needs a 6-digit code" >&2; exit 2; }
      shift 2 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

say "1/5  python"
command -v python3 >/dev/null || die "python3 is not on PATH"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' \
  || die "Python 3.8+ is required"

say "2/5  tests"
python3 tests/check_static.py || die "static checks failed; aborted"
python3 tests/test_terminals.py || die "driver tests failed; aborted"
if [ "$CHECK_ONLY" -eq 0 ]; then
  python3 tests/test_devpad.py || die "end-to-end tests failed; aborted"
else
  echo "  (--check: skipping the slower end-to-end suite)"
fi

say "3/5  npm identity"
npm whoami >/dev/null 2>&1 || die "not logged in to npm. Run: npm login"
echo "  logged in as: $(npm whoami)"

say "4/5  package"
NAME=$(python3 -c 'import json;print(json.load(open("package.json"))["name"])')
VERSION=$(python3 -c 'import json;print(json.load(open("package.json"))["version"])')
echo "  $NAME@$VERSION"
if npm view "$NAME@$VERSION" version >/dev/null 2>&1; then
  die "$NAME@$VERSION is already published. Bump it: npm version patch"
fi
npm pack --dry-run 2>&1 | grep -E 'total files|package size' | sed 's/npm notice/ /'

if [ "$CHECK_ONLY" -eq 1 ]; then
  say "checks passed (not published). Drop --check to publish."
  exit 0
fi

say "5/5  publish"
if [ -z "$OTP" ]; then
  printf 'npm 2FA code (6 digits; press Enter if your token bypasses 2FA): '
  read -r OTP || OTP=""
fi

set +e
if [ -n "$OTP" ]; then
  OUT=$(npm publish --otp="$OTP" 2>&1)
  RC=$?
else
  OUT=$(npm publish 2>&1)
  RC=$?
fi
set -e

printf '%s\n' "$OUT"

if [ "$RC" -ne 0 ]; then
  echo
  case "$OUT" in
    *"one-time password"*|*EOTP*|*"Two-factor"*|*2FA*|*"2fa"*)
      die "that code was rejected or had expired. Re-run: ./release.sh --otp <fresh code>" ;;
    *E403*)
      die "permission denied. Easiest durable fix: create a Granular Access Token on
  npmjs.com with \"Bypass 2FA\" checked, then:
  npm config set //registry.npmjs.org/:_authToken=<token>" ;;
    *)
      die "publish failed (exit $RC); see the output above" ;;
  esac
fi

say "done: https://www.npmjs.com/package/$NAME"
echo
echo "verify the install: npx $NAME@$VERSION --version"
