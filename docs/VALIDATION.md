# Public package validation

2026-10-26, before initial publication:

- Patch applied with `--fuzz=0` to the pinned NVIDIA R36.5 source; output SHA-256 exactly matched the driver source used in the successful Gen2 experiment.
- `scripts/build.sh` ran natively on aarch64 /5.15.185-tegra in a separate temporary directory. Kernel module built; C register tool compiled with `-Wall -Wextra -Werror`. No candidate module was loaded or installed by this packaging check.
- The build reported a distro compiler revision-string difference (11.4.0 package revisions); it completed successfully. This is not a claim of bit-identical module binaries.
- Invalid C-tool operation rejected with exit2. A write request while the gadget was bound rejected with exit2 before opening `/dev/mem`. Unprivileged Python wrapper invocation rejected with exit2.
- A privileged read-only status check with the packaged wrapper reported generation2 and identical before/after register values; no live speed switch was performed for packaging.
- Bash/Python syntax checks passed. An independent read-only review found no publication blockers in code, instructions, provenance or privacy. Whitespace in the literal patch's context lines is intentional patch syntax.
- Only selected numeric results were exported. No private hostnames, MACs, account credentials, serials, raw crash dumps, binary modules or vendor PDF manuals are published.

The source patch's live hardware validation is in RESULTS.md. The packaged clean-boot installation is **not** end-to-end tested. Status/guard checks do not certify all failure paths of the runtime wrapper, and no generic hardware-support claim follows from a successful build.
