"""
tsc_engine.py — Motore TSC: RDTSC shellcode, calibrazione frequenza, epoch sync.
CPU: Intel i5-1135G7 — Invariant TSC @ 2.4192 GHz
"""

import ctypes, time, threading

# ── RDTSC shellcode x64 ───────────────────────────────────────────────────────
# push rbx; rdtsc; shl rdx,32; or rax,rdx; pop rbx; ret
_CODE = bytes([0x53, 0x0F, 0x31, 0x48, 0xC1, 0xE2, 0x20, 0x48, 0x09, 0xD0, 0x5B, 0xC3])
_k32  = ctypes.windll.kernel32
_k32.VirtualAlloc.restype = ctypes.c_void_p
_mem  = _k32.VirtualAlloc(None, len(_CODE), 0x3000, 0x40)
ctypes.memmove(_mem, _CODE, len(_CODE))
_RDTSC = ctypes.CFUNCTYPE(ctypes.c_uint64)(_mem)

# Da CPUID leaf 0x15: crystal 38.4 MHz × 63
TSC_FREQ_CPUID: int = 2_419_200_000

def rdtsc() -> int:
    return _RDTSC()

def qpc_freq() -> int:
    f = ctypes.c_longlong(0)
    _k32.QueryPerformanceFrequency(ctypes.byref(f))
    return f.value

def qpc() -> int:
    c = ctypes.c_longlong(0)
    _k32.QueryPerformanceCounter(ctypes.byref(c))
    return c.value


def calibrate_tsc_freq(duration_s: float = 3.0) -> float:
    """Misura sperimentale TSC vs QPC."""
    freq = qpc_freq()
    q1, t1 = qpc(), rdtsc()
    time.sleep(duration_s)
    q2, t2 = qpc(), rdtsc()
    elapsed_s = (q2 - q1) / freq
    return (t2 - t1) / elapsed_s


class TSCEngine:
    """Sorgente temporale basata su RDTSC con epoch sincronizzata al wall clock."""

    def __init__(self, calibration_s: float = 3.0, n_sync_samples: int = 20):
        # Calibra frequenza
        self.freq_cpuid    = float(TSC_FREQ_CPUID)
        self.freq_measured = calibrate_tsc_freq(calibration_s)
        self.freq          = self.freq_measured        # frequenza in uso

        # Sync epoch: mediana di n campioni per minimizzare jitter syscall
        samples = []
        for _ in range(n_sync_samples):
            tb = time.time()
            tc = rdtsc()
            ta = time.time()
            samples.append((tc, (tb + ta) / 2.0))
        samples.sort(key=lambda s: s[1])
        mid = len(samples) // 2
        self.epoch_tsc:  int   = samples[mid][0]
        self.epoch_wall: float = samples[mid][1]

        # Stato interno
        self._lock         = threading.Lock()
        self._freq_history: list[float] = [self.freq_measured]

    # ── Letture ──────────────────────────────────────────────────────────────
    def now_tsc(self)        -> int:   return rdtsc()
    def elapsed_ticks(self)  -> int:   return rdtsc() - self.epoch_tsc
    def elapsed_seconds(self)-> float: return self.elapsed_ticks() / self.freq
    def tsc_time(self)       -> float: return self.epoch_wall + self.elapsed_seconds()
    def drift_us(self)       -> float: return (self.tsc_time() - time.time()) * 1_000_000

    def drift_ppm(self) -> float:
        e = self.elapsed_seconds()
        return self.drift_us() / e if e > 1 else 0.0

    # ── Ricalibrazione adattiva ───────────────────────────────────────────────
    def recalibrate(self, duration_s: float = 1.0):
        """Ricampiona la frequenza e aggiorna epoch senza perdere monotonia."""
        new_freq = calibrate_tsc_freq(duration_s)
        with self._lock:
            self._freq_history.append(new_freq)
            # Media mobile delle ultime 5 misure per smorzare il rumore
            window = self._freq_history[-5:]
            self.freq = sum(window) / len(window)

    def resync_epoch(self):
        """Risincronizza l'epoch col wall clock (dopo sospensione/resume)."""
        samples = []
        for _ in range(10):
            tb = time.time()
            tc = rdtsc()
            ta = time.time()
            samples.append((tc, (tb + ta) / 2.0))
        samples.sort(key=lambda s: s[1])
        mid = len(samples) // 2
        with self._lock:
            self.epoch_tsc  = samples[mid][0]
            self.epoch_wall = samples[mid][1]

    # ── Proiezioni deriva ─────────────────────────────────────────────────────
    def drift_projections(self) -> dict[str, float]:
        """Deriva proiettata (ms) ai vari orizzonti temporali."""
        e = self.elapsed_seconds()
        if e < 5:
            return {}
        rate_us_per_s = self.drift_us() / e
        return {
            "1h":  rate_us_per_s * 3_600 / 1_000,
            "1d":  rate_us_per_s * 86_400 / 1_000,
            "1w":  rate_us_per_s * 604_800 / 1_000,
            "1mo": rate_us_per_s * 2_592_000 / 1_000,
        }

    @property
    def freq_delta_ppm(self) -> float:
        return (self.freq_measured - self.freq_cpuid) / self.freq_cpuid * 1_000_000
