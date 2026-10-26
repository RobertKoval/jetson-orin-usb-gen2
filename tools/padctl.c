// SPDX-License-Identifier: GPL-2.0-or-later
// AGX Orin devkit J40 only. Not a general-purpose /dev/mem writer.
#define _FILE_OFFSET_BITS 64
#include <ctype.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

static void fail(const char *s) { fprintf(stderr, "%s\n", s); exit(2); }
static size_t read_file(const char *path, char *buf, size_t capacity) {
    FILE *f = fopen(path, "rb");
    if (!f) { perror(path); exit(2); }
    size_t n = fread(buf, 1, capacity - 1, f);
    if (ferror(f) || !feof(f)) { fclose(f); fail("Cannot read complete target identity/state"); }
    fclose(f); buf[n] = 0; return n;
}
static void require_blank(const char *path) {
    char b[256]; size_t n = read_file(path, b, sizeof b);
    for (size_t i = 0; i < n; ++i)
        if (!isspace((unsigned char)b[i])) fail("Unbind the gadget before changing speed");
}
int main(int argc, char **argv) {
    if (argc != 2 || (strcmp(argv[1], "status") && strcmp(argv[1], "gen1") && strcmp(argv[1], "gen2")))
        fail("Usage: orin-usb-speed status|gen1|gen2");
    int writing = strcmp(argv[1], "status") != 0;
    char b[256];
    read_file("/proc/device-tree/model", b, sizeof b);
    if (strcmp(b, "NVIDIA Jetson AGX Orin Developer Kit")) fail("Unsupported board");
    read_file("/proc/device-tree/bus@0/padctl@3520000/compatible", b, sizeof b);
    if (strcmp(b, "nvidia,tegra234-xusb-padctl")) fail("Unsupported PADCTL mapping");
    if (writing) {
        read_file("/sys/module/tegra_xudc/version", b, sizeof b);
        b[strcspn(b, "\r\n")] = 0;
        if (strcmp(b, "orin-gen2-probe-3")) fail("The tested patched driver must be loaded first");
        require_blank("/sys/kernel/config/usb_gadget/l4t/UDC");
        require_blank("/sys/class/udc/3550000.usb/function");
    }
    int fd = open("/dev/mem", (writing ? O_RDWR : O_RDONLY) | O_SYNC);
    if (fd < 0) { perror("/dev/mem"); return 1; }
    void *p = mmap(NULL, 4096, PROT_READ | (writing ? PROT_WRITE : 0), MAP_SHARED, fd, 0x03520000);
    if (p == MAP_FAILED) { perror("mmap"); close(fd); return 1; }
    volatile uint32_t *reg = (volatile uint32_t *)((char *)p + 0x2c);
    uint32_t before = *reg, after = before, mask = 3u << 20;
    unsigned field = (before & mask) >> 20;
    if (field > 1) { munmap(p,4096); close(fd); fail("Reserved device-speed field; refusing to modify"); }
    int result = 0;
    if (writing) {
        uint32_t wanted = (before & ~mask) | ((strcmp(argv[1],"gen2") == 0 ? 1u : 0u) << 20);
        *reg = wanted; __sync_synchronize(); after = *reg;
        if (after != wanted) {
            *reg = (after & ~mask) | (before & mask); __sync_synchronize();
            fprintf(stderr, "Readback mismatch; attempted device-field rollback\n"); result = 1;
        }
    }
    printf("{\"before\":%u,\"after\":%u,\"generation\":%u}\n", before, after, ((after & mask) >> 20) + 1);
    munmap(p,4096); close(fd); return result;
}
