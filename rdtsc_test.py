import ctypes, time, sys

# RDTSC shellcode: push rbx; rdtsc; shl rdx,32; or rax,rdx; pop rbx; ret
rdtsc_code = bytes([0x53, 0x0F, 0x31, 0x48, 0xC1, 0xE2, 0x20, 0x48, 0x09, 0xD0, 0x5B, 0xC3])
k32 = ctypes.windll.kernel32
k32.VirtualAlloc.restype = ctypes.c_void_p
mem = k32.VirtualAlloc(None, len(rdtsc_code), 0x3000, 0x40)
if not mem:
    print("VirtualAlloc failed")
    sys.exit(1)

ctypes.memmove(mem, rdtsc_code, len(rdtsc_code))

RDTSC = ctypes.CFUNCTYPE(ctypes.c_uint64)
rdtsc = RDTSC(mem)

tsc_freq = 2419200000

t0_sys = time.time()
t0_tsc = rdtsc()

print("=== TSC Sync Test (5 samples x 1s) ===")
print("%-6s | %-12s | %-12s | %s" % ("Sample", "Wall time", "TSC time", "Delta(us)"))
for i in range(5):
    time.sleep(1.0)
    t_sys = time.time()
    t_tsc_raw = rdtsc()
    elapsed_tsc = (t_tsc_raw - t0_tsc) / tsc_freq
    elapsed_sys = t_sys - t0_sys
    delta_us = (elapsed_tsc - elapsed_sys) * 1e6
    print("%-6d | %-12.6f | %-12.6f | %+.2f us" % (i+1, elapsed_sys, elapsed_tsc, delta_us))

k32.VirtualFree(mem, 0, 0x8000)
