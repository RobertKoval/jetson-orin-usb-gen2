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

## v0.2.0 executable validation

The original wrapper regression was reproduced before replacement: a failed rollback was reported only as the initial target error. The new executable distinguishes verified rollback from incomplete recovery.

- 49 local contract tests passed: runtime state transitions, same-mode no-op, invalid preconditions, signal cancellation, failed target/rollback/binding diagnostics, Gen2-capability/Gen1-negotiated restoration, deadline boundaries, process exclusion, atomic helper publication, installation failures, interrupts during recovery, corrupt backups, drift and initramfs mismatch. These tests use boundary simulators and temporary files; they are not proofs of kernel behavior.
- The same 49 tests passed natively on the Orin. The exact pinned kernel source applied and built without fuzz; helper compilation succeeded. The driver patch was not changed.
- An independent review found and resolved three integration issues: obsolete unlocked mutator, non-atomic helper compilation, and rollback diagnostics preventing/masking rebind. Installer signal handling received additional failure tests.
- New runtime CLI executed10G→5G→10G on the board. Device state/readback verified each mode; the independent host reported5000 then10000.
- Real `install --dry-run` passed. Real installation preserved originals, staged the module/initrd, and verified one matching XUDC copy inside the generated initramfs. No loaded module was replaced and no reboot was executed.
- The installed executable successfully enabled boot Gen2. The actual systemd oneshot ran successfully on the already-running board. `disable --permanent` produced device/host Gen1 and disabled boot policy; `enable --permanent` restored configuredGen2 and enabled boot policy.
- After the final permanent off/on sequence,1.25GiB streaming SHA-256 checks passed in both directions again. These were integrity checks, not new throughput claims.
- **Real reboot validation remains OPEN at the user's explicit request.** Running a boot unit on an already-running system is not proof of a successful cold boot. See TODO.md.

The current executable is new automation around the previously tested driver. Package installation and runtime operations are now exercised on one board, but other kernels/boards, power loss, generic suspend/resume, every hot-unload race and long-duration USB compliance remain unvalidated.
