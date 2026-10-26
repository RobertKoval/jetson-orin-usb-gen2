#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
[[ $# == 1 && "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "Usage: $0 vX.Y.Z" >&2; exit 2; }
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
[[ -z $(git status --porcelain) ]] || { echo 'Commit/review changes before packaging.' >&2; exit 2; }
version=$1
mkdir -p dist
archive="jetson-orin-usb-gen2-${version}.tar.gz"
git archive --format=tar.gz --prefix="jetson-orin-usb-gen2-${version}/" HEAD > "dist/$archive"
(cd dist && shasum -a 256 "$archive" > "$archive.sha256")
printf '%s\n' "dist/$archive" "dist/$archive.sha256"
