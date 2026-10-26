#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: $0 /path/to/kernel/kernel-jammy-src" >&2
  exit 2
fi
[[ $(uname -m) == aarch64 && $(uname -r) == 5.15.185-tegra ]] || {
  echo 'Only native aarch64 / 5.15.185-tegra is covered by this build recipe.' >&2; exit 2;
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source_file="$1/drivers/usb/gadget/udc/tegra-xudc.c"
expected_base=fec05c0060201cce2e27a142bccb629df1b7398479f9d21bed2b053b2535448f
actual=$(sha256sum -- "$source_file" | cut -d' ' -f1)
[[ "$actual" == "$expected_base" ]] || { echo 'Unsupported source revision; refusing to apply with fuzz.' >&2; exit 2; }
mkdir -p "$root/build"
cp -- "$source_file" "$root/build/tegra-xudc.c"
patch --batch --fuzz=0 --forward -d "$root/build" -p1 < "$root/patches/0001-tegra-xudc-experimental-gen2.patch"
actual=$(sha256sum "$root/build/tegra-xudc.c" | cut -d' ' -f1)
[[ "$actual" == 3ff1616f8924e006d724fc4568025f9f1a11e49ddb801a3a7e56694efcc20f00 ]] || { echo 'Patched source differs from the tested source.' >&2; exit 1; }
printf 'obj-m += tegra-xudc.o\n' > "$root/build/Makefile"
make -C "/lib/modules/$(uname -r)/build" M="$root/build" modules
cc -O2 -Wall -Wextra -Werror "$root/tools/padctl.c" -o "$root/build/orin-usb-speed"
printf '\nBuilt %s\nNo module loaded or installed; no registers changed.\n' "$root/build/tegra-xudc.ko"
