import ctypes, struct, os, sys

shellcode = bytes([
    0x53,
    0x89, 0xC8,
    0x0F, 0xA2,
    0x41, 0x89, 0x00,
    0x41, 0x89, 0x58, 0x04,
    0x41, 0x89, 0x48, 0x08,
    0x41, 0x89, 0x50, 0x0C,
    0x5B,
    0xC3,
])

kernel32 = ctypes.windll.kernel32
VirtualAlloc = kernel32.VirtualAlloc
VirtualAlloc.restype = ctypes.c_void_p
VirtualFree = kernel32.VirtualFree

mem = VirtualAlloc(None, len(shellcode), 0x3000, 0x40)
if not mem:
    print("VirtualAlloc failed")
    sys.exit(1)

ctypes.memmove(mem, shellcode, len(shellcode))

CPUID_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
cpuid_func = CPUID_FUNC(mem)

def cpuid(leaf):
    regs = (ctypes.c_uint32 * 4)()
    cpuid_func(leaf, 0, regs)
    return regs[0], regs[1], regs[2], regs[3]

print("=== CPUID Results ===")

eax, ebx, ecx, edx = cpuid(0)
vendor = struct.pack("III", ebx, edx, ecx).decode(errors="replace")
print("Leaf 0x00: max=%d, vendor=%s" % (eax, vendor))

eax, ebx, ecx, edx = cpuid(1)
print("Leaf 0x01: EAX=0x%08X ECX=0x%08X EDX=0x%08X" % (eax, ecx, edx))
print("  TSC bit4 EDX: %d" % ((edx>>4)&1))
family = (eax>>8)&0xF
model = ((eax>>16)&0xF)*16 + ((eax>>4)&0xF)
stepping = eax&0xF
print("  Family: %d, Model: %d (0x%X), Stepping: %d" % (family, model, model, stepping))

eax, ebx, ecx, edx = cpuid(6)
print("Leaf 0x06: EAX=0x%08X" % eax)
print("  ARAT Always Running APIC Timer (bit2): %d" % ((eax>>2)&1))
print("  Turbo Boost (bit1): %d" % ((eax>>1)&1))
print("  HWP Hardware P-states (bit7): %d" % ((eax>>7)&1))

eax, ebx, ecx, edx = cpuid(0x80000007)
print("Leaf 0x80000007: EDX=0x%08X" % edx)
inv_tsc = (edx>>8)&1
print("  Invariant TSC (bit 8): %d  <-- KEY FLAG" % inv_tsc)
if inv_tsc:
    print("  ==> TSC INVARIANTE: si incrementa a frequenza costante indipendentemente da P-state/C-state")
else:
    print("  ==> TSC NON invariante: la frequenza varia con il P-state")

eax, ebx, ecx, edx = cpuid(0x15)
print("Leaf 0x15 TSC/crystal: EAX=%d EBX=%d ECX=%d" % (eax, ebx, ecx))
if eax > 0 and ebx > 0:
    ratio = ebx / eax
    print("  Ratio TSC:crystal = %d:%d = %.4f" % (ebx, eax, ratio))
    if ecx > 0:
        tsc_hz = ecx * ebx // eax
        print("  Crystal freq: %d Hz (%.3f MHz)" % (ecx, ecx/1e6))
        print("  TSC freq: %d Hz (%.6f GHz)" % (tsc_hz, tsc_hz/1e9))

# Leaf 0x16: CPU base/max/bus freq
eax, ebx, ecx, edx = cpuid(0x16)
print("Leaf 0x16 CPU freq: EAX=%d EBX=%d ECX=%d" % (eax, ebx, ecx))
if eax: print("  Base freq: %d MHz" % eax)
if ebx: print("  Max freq: %d MHz" % ebx)
if ecx: print("  Bus freq: %d MHz" % ecx)

VirtualFree(mem, 0, 0x8000)
