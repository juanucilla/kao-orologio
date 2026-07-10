"""
TSC Clock AWS - Porting Linux/EC2 dell'orologio TSC (tsc_clock.py)
==================================================================
Stesso modello del TSCClock Windows, adattato ai vincoli di una VM AWS
(verificati sul campo il 2026-07-10 su una capture box t3.medium,
Xeon Platinum 8259CL, Amazon Linux 2023, hypervisor Nitro):

  * RDTSC e' eseguibile da user-space e il TSC e' invariant
    (CPUID 0x80000007 EDX bit8 = 1; flag constant_tsc + nonstop_tsc);
    il kernel stesso usa `tsc` come clocksource.
  * CPUID leaf 0x15 e' esposto ma coi valori del hypervisor: la formula
    standard crystal*ebx/eax NON ricostruisce la frequenza reale.
    Quindi NIENTE frequenza cablata ne' derivata da CPUID: solo
    calibrazione runtime contro CLOCK_MONOTONIC_RAW (immune allo
    slewing NTP).
  * Su EC2 uno stop/start puo' migrare la VM su un host fisico diverso
    (frequenza/offset TSC diversi): la calibrazione e' legata al
    boot_id del kernel e viene rifatta a ogni boot.

Differenze API rispetto a tsc_clock.py (Windows):
  - kernel32/QueryPerformanceCounter  ->  time.clock_gettime(CLOCK_MONOTONIC_RAW)
  - VirtualAlloc(PAGE_EXECUTE_READWRITE) -> mmap(PROT_READ|WRITE|EXEC)
  - la frequenza da CPUID 0x15 -> rimossa (vedi sopra)

Uso:
    from tsc_clock_aws import TSCClockAWS
    clk = TSCClockAWS(calibration_secs=2.0)
    clk.tsc_time()       # secondi wall-clock derivati dal TSC
    clk.tsc_datetime()   # datetime UTC derivato dal TSC
    clk.drift_us()       # drift attuale vs clock di sistema (NTP)

CLI:
    python3 tsc_clock_aws.py --calibrate 2 --watch 10
"""
from __future__ import annotations

import ctypes
import datetime
import json
import mmap
import os
import time
from pathlib import Path

# ── RDTSC via shellcode (SysV x86-64) ────────────────────────────────────────
# rdtsc; shl rdx,32; or rax,rdx; ret
_RDTSC_CODE = bytes([0x0F, 0x31, 0x48, 0xC1, 0xE2, 0x20, 0x48, 0x09, 0xD0, 0xC3])

_buf = mmap.mmap(-1, len(_RDTSC_CODE),
                 prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)
_buf.write(_RDTSC_CODE)
_addr = ctypes.addressof(ctypes.c_char.from_buffer(_buf))
rdtsc = ctypes.CFUNCTYPE(ctypes.c_uint64)(_addr)


# ── Prerequisiti hardware ─────────────────────────────────────────────────────
def check_invariant_tsc() -> tuple[bool, str]:
    """Il TSC e' utilizzabile come base tempo solo se invariant.

    Su Linux i flag del kernel sono la fonte piu' affidabile in VM
    (il kernel li deriva da CPUID e dai propri test al boot).
    """
    try:
        flags = ""
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                flags = line
                break
        missing = [f for f in ("constant_tsc", "nonstop_tsc") if f not in flags]
        if missing:
            return False, f"flag mancanti: {missing}"
        return True, "constant_tsc + nonstop_tsc presenti"
    except OSError as e:
        return False, f"/proc/cpuinfo non leggibile: {e!r}"


def boot_id() -> str:
    """Identifica il boot corrente: cambia a ogni riavvio (e quindi a
    ogni possibile migrazione di host su EC2)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


# ── Calibrazione ──────────────────────────────────────────────────────────────
def calibrate_tsc(duration_s: float = 2.0) -> float:
    """Frequenza TSC misurata contro CLOCK_MONOTONIC_RAW.

    MONOTONIC_RAW non subisce le correzioni NTP (ne' slew ne' step):
    e' l'equivalente concettuale del QPC usato dalla versione Windows.
    """
    m1 = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    t1 = rdtsc()
    time.sleep(duration_s)
    m2 = time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    t2 = rdtsc()
    return (t2 - t1) / (m2 - m1)


class TSCClockAWS:
    """Orologio TSC per VM AWS: calibrazione per-boot, ancora wall-clock NTP.

    La cache di calibrazione (default ~/.tsc_clock_aws.json) e' valida
    solo per lo stesso boot_id: dopo uno stop/start dell'istanza (host
    fisico potenzialmente diverso) si ricalibra da zero.
    """

    def __init__(self, calibration_secs: float = 2.0,
                 cache_path: str | os.PathLike | None = None):
        ok, why = check_invariant_tsc()
        if not ok:
            raise RuntimeError(f"TSC non invariant su questa macchina: {why}")
        self.boot_id = boot_id()
        self.cache_path = Path(cache_path or
                               Path.home() / ".tsc_clock_aws.json")
        self.freq = self._load_cached_freq()
        if self.freq is None:
            self.freq = calibrate_tsc(calibration_secs)
            self._store_freq()
        # Ancora wall-clock: coppia (tsc, epoch) presa ORA. Da qui in poi
        # il tempo scorre col TSC; il drift vs sistema resta osservabile.
        self.anchor_tsc = rdtsc()
        self.anchor_wall = time.time()

    # -- cache per-boot ----------------------------------------------------
    def _load_cached_freq(self) -> float | None:
        try:
            d = json.loads(self.cache_path.read_text())
            if d.get("boot_id") == self.boot_id and d.get("freq", 0) > 0:
                return float(d["freq"])
        except (OSError, ValueError):
            pass
        return None

    def _store_freq(self) -> None:
        try:
            self.cache_path.write_text(json.dumps(
                {"boot_id": self.boot_id, "freq": self.freq,
                 "calibrated_at": datetime.datetime.now(
                     datetime.timezone.utc).isoformat()}))
        except OSError:
            pass  # cache best-effort: senza, si ricalibra al prossimo avvio

    # -- API identica al TSCClock Windows -----------------------------------
    def now_tsc(self) -> int:
        return rdtsc()

    def elapsed_ticks(self) -> int:
        return rdtsc() - self.anchor_tsc

    def elapsed_seconds(self) -> float:
        return self.elapsed_ticks() / self.freq

    def tsc_time(self) -> float:
        """Epoch (secondi) secondo il TSC, ancorato al wall clock di avvio."""
        return self.anchor_wall + self.elapsed_seconds()

    def sys_time(self) -> float:
        return time.time()

    def drift_us(self) -> float:
        """TSC-derivato meno clock di sistema (NTP), in microsecondi."""
        return (self.tsc_time() - self.sys_time()) * 1e6

    def tsc_datetime(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(
            self.tsc_time(), tz=datetime.timezone.utc)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="TSC clock per VM AWS")
    ap.add_argument("--calibrate", type=float, default=2.0,
                    help="secondi di calibrazione (default 2)")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="osserva il drift per N secondi dopo la calibrazione")
    args = ap.parse_args()

    ok, why = check_invariant_tsc()
    print(f"invariant TSC : {'OK' if ok else 'NO'} ({why})")
    if not ok:
        return 1
    print(f"boot_id       : {boot_id()}")

    clk = TSCClockAWS(calibration_secs=args.calibrate)
    print(f"freq calibrata: {clk.freq / 1e9:.6f} GHz "
          f"(cache: {clk.cache_path})")

    if args.watch > 0:
        t_end = time.monotonic() + args.watch
        while time.monotonic() < t_end:
            time.sleep(min(2.0, max(0.1, t_end - time.monotonic())))
            print(f"  tsc={clk.tsc_datetime().isoformat()} "
                  f"drift={clk.drift_us():+8.1f} us "
                  f"elapsed={clk.elapsed_seconds():8.3f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
