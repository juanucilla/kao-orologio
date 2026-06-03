"""
TSC Clock - Orologio software basato sul Time Stamp Counter
===========================================================
CPU: Intel i5-1135G7 @ 2.40GHz (Tiger Lake)
TSC freq: 2.419200 GHz (da CPUID leaf 0x15: crystal 38.4 MHz x63)
Invariant TSC: SI (CPUID 0x80000007 EDX bit8 = 1)
"""

import ctypes, time, datetime, sys, os, threading, collections, signal
import statistics

# ── RDTSC via shellcode ───────────────────────────────────────────────────────
# push rbx; rdtsc; shl rdx,32; or rax,rdx; pop rbx; ret
_RDTSC_CODE = bytes([0x53, 0x0F, 0x31, 0x48, 0xC1, 0xE2, 0x20, 0x48, 0x09, 0xD0, 0x5B, 0xC3])
_k32 = ctypes.windll.kernel32
_k32.VirtualAlloc.restype = ctypes.c_void_p
_mem = _k32.VirtualAlloc(None, len(_RDTSC_CODE), 0x3000, 0x40)
ctypes.memmove(_mem, _RDTSC_CODE, len(_RDTSC_CODE))
_RDTSC_FUNC = ctypes.CFUNCTYPE(ctypes.c_uint64)
_rdtsc_fn = _RDTSC_FUNC(_mem)


# ── Calibrazione frequenza TSC ────────────────────────────────────────────────
TSC_FREQ_CPUID = 2_419_200_000   # Da CPUID 0x15: 38400000 Hz * 126/2

def calibrate_tsc(duration_s: float = 2.0) -> float:
    """Misura sperimentale della frequenza TSC tramite confronto con QPC."""
    k32 = ctypes.windll.kernel32
    qpf = ctypes.c_longlong(0)
    k32.QueryPerformanceFrequency(ctypes.byref(qpf))
    qpc_freq = qpf.value

    qpc1 = ctypes.c_longlong(0)
    k32.QueryPerformanceCounter(ctypes.byref(qpc1))
    tsc1 = _rdtsc_fn()

    time.sleep(duration_s)

    qpc2 = ctypes.c_longlong(0)
    k32.QueryPerformanceCounter(ctypes.byref(qpc2))
    tsc2 = _rdtsc_fn()

    qpc_elapsed = (qpc2.value - qpc1.value) / qpc_freq
    tsc_elapsed = tsc2 - tsc1
    measured = tsc_elapsed / qpc_elapsed
    return measured


def rdtsc() -> int:
    return _rdtsc_fn()


# ── Classe orologio TSC ───────────────────────────────────────────────────────
class TSCClock:
    def __init__(self, calibration_secs: float = 2.0):
        print("Calibrazione TSC in corso... (%.1fs)" % calibration_secs)

        # Calibra frequenza
        self.freq_measured = calibrate_tsc(calibration_secs)
        self.freq_cpuid    = float(TSC_FREQ_CPUID)

        print("  CPUID freq : %.0f Hz  (%.6f GHz)" % (self.freq_cpuid,  self.freq_cpuid/1e9))
        print("  Misurata   : %.0f Hz  (%.6f GHz)" % (self.freq_measured, self.freq_measured/1e9))
        print("  Delta      : %+.0f Hz  (%+.3f ppm)" % (
            self.freq_measured - self.freq_cpuid,
            (self.freq_measured - self.freq_cpuid) / self.freq_cpuid * 1e6))

        # Usa la media pesata: CPUID è esatto per hw invariant, misurata corregge offset Hyper-V
        self.freq = self.freq_measured

        # Sincronizza epoch con l'orologio di sistema
        # Campiona più volte e prende la mediana per ridurre jitter
        samples = []
        for _ in range(20):
            t_before = time.time()
            tsc_now  = rdtsc()
            t_after  = time.time()
            t_sys    = (t_before + t_after) / 2.0
            samples.append((tsc_now, t_sys))

        tsc_vals = [s[0] for s in samples]
        sys_vals = [s[1] for s in samples]
        mid = len(samples) // 2
        self.epoch_tsc = sorted(tsc_vals)[mid]
        self.epoch_sys = sorted(sys_vals)[mid]
        self.epoch_wall = self.epoch_sys

        # Statistiche deriva
        self.drift_log: list[tuple[float, float]] = []  # (elapsed_s, drift_us)
        self.minute_stats: list[dict] = []
        self._lock = threading.Lock()
        self._running = True

        # Thread statistiche minuto
        self._stats_thread = threading.Thread(target=self._stats_worker, daemon=True)
        self._stats_thread.start()

    def now_tsc(self) -> int:
        return rdtsc()

    def elapsed_ticks(self) -> int:
        return rdtsc() - self.epoch_tsc

    def elapsed_seconds(self) -> float:
        return self.elapsed_ticks() / self.freq

    def tsc_time(self) -> float:
        """Unix timestamp calcolato esclusivamente dai tick TSC."""
        return self.epoch_wall + self.elapsed_seconds()

    def sys_time(self) -> float:
        return time.time()

    def drift_us(self) -> float:
        """Differenza (TSC - SYS) in microsecondi."""
        return (self.tsc_time() - self.sys_time()) * 1e6

    def tsc_datetime(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.tsc_time())

    def _stats_worker(self):
        while self._running:
            time.sleep(60)
            if not self._running:
                break
            d = self.drift_us()
            e = self.elapsed_seconds()
            with self._lock:
                self.drift_log.append((e, d))
                self.minute_stats.append({
                    "elapsed_s":  round(e, 3),
                    "elapsed_min": round(e / 60, 2),
                    "drift_us":   round(d, 2),
                    "drift_ppm":  round(d / e if e else 0, 3),
                    "tsc_time":   self.tsc_datetime().isoformat(timespec="milliseconds"),
                })

    def stop(self):
        self._running = False


# ── Rendering grafico deriva (ASCII) ─────────────────────────────────────────
def render_drift_chart(drift_log: list[tuple[float, float]], width: int = 60, height: int = 12):
    if len(drift_log) < 2:
        return "  (dati insufficienti per il grafico — serve almeno 2 minuti)"

    times  = [d[0] / 60 for d in drift_log]
    drifts = [d[1] for d in drift_log]
    d_min, d_max = min(drifts), max(drifts)
    t_min, t_max = min(times),  max(times)

    if d_max == d_min:
        d_max = d_min + 1

    grid = [[" "] * width for _ in range(height)]
    zero_row = int((0 - d_min) / (d_max - d_min) * (height - 1))
    zero_row = max(0, min(height - 1, zero_row))
    zero_row = height - 1 - zero_row

    # Linea dello zero
    for c in range(width):
        grid[zero_row][c] = "-"

    # Punti
    for t, d in zip(times, drifts):
        col = int((t - t_min) / max(t_max - t_min, 1e-9) * (width - 1))
        row = int((d - d_min) / (d_max - d_min) * (height - 1))
        row = height - 1 - max(0, min(height - 1, row))
        col = max(0, min(width - 1, col))
        grid[row][col] = "*"

    lines = []
    for r, row in enumerate(grid):
        if r == 0:
            label = "%+.0f us" % d_max
        elif r == height - 1:
            label = "%+.0f us" % d_min
        else:
            label = "        "
        lines.append("  %8s |%s|" % (label, "".join(row)))

    lines.append("           +%s+" % ("-" * width))
    lines.append("           0 min%s%.1f min" % (" " * (width - 12), t_max))
    return "\n".join(lines)


# ── Display principale ────────────────────────────────────────────────────────
def display_loop(clock: TSCClock):
    CLEAR = "\033[2J\033[H"
    os.system("cls")

    update_count = 0
    try:
        while True:
            update_count += 1
            tsc_now      = clock.now_tsc()
            ticks        = clock.elapsed_ticks()
            elapsed_s    = clock.elapsed_seconds()
            tsc_dt       = clock.tsc_datetime()
            sys_dt       = datetime.datetime.fromtimestamp(clock.sys_time())
            drift        = clock.drift_us()

            lines = []
            lines.append("=" * 70)
            lines.append("  TSC CLOCK  —  Intel i5-1135G7  —  Invariant TSC 2.4192 GHz")
            lines.append("=" * 70)
            lines.append("")
            lines.append("  TSC corrente      : %d" % tsc_now)
            lines.append("  Tick trascorsi    : %d" % ticks)
            lines.append("  Secondi trascorsi : %.9f s" % elapsed_s)
            lines.append("")
            lines.append("  Ora TSC           : %s" % tsc_dt.strftime("%Y-%m-%d  %H:%M:%S.%f"))
            lines.append("  Ora Sistema       : %s" % sys_dt.strftime("%Y-%m-%d  %H:%M:%S.%f"))
            lines.append("")
            lines.append("  Frequenza TSC     : %.0f Hz  (%.6f GHz)" % (clock.freq, clock.freq/1e9))
            lines.append("  Freq CPUID        : %.0f Hz  (%.6f GHz)" % (clock.freq_cpuid, clock.freq_cpuid/1e9))
            lines.append("")

            # Drift
            drift_ppm = drift / elapsed_s if elapsed_s > 1 else 0.0
            drift_bar_len = min(40, int(abs(drift) / 10))
            drift_dir = "+" if drift >= 0 else "-"
            drift_bar = "[%s%s%s]" % (
                " " * (20 - drift_bar_len if drift < 0 else 20),
                "|" if drift >= 0 else drift_bar_len * "<",
                drift_bar_len * ">" if drift >= 0 else "|" + " " * (20 - drift_bar_len)
            )
            lines.append("  Deriva TSC-SYS    : %+.3f us  (%+.4f ppm)" % (drift, drift_ppm))

            # Proiezioni deriva
            if elapsed_s > 5:
                us_per_s   = drift / elapsed_s
                lines.append("")
                lines.append("  Proiezioni deriva (a frequenza attuale):")
                lines.append("    dopo  1 ora   : %+.3f ms" % (us_per_s * 3600 / 1000))
                lines.append("    dopo  1 giorno : %+.3f ms" % (us_per_s * 86400 / 1000))
                lines.append("    dopo  1 settim.: %+.3f ms" % (us_per_s * 86400 * 7 / 1000))
                lines.append("    dopo  1 mese   : %+.3f ms" % (us_per_s * 86400 * 30 / 1000))

            lines.append("")
            lines.append("  Statistiche per minuto (%d campioni):" % len(clock.minute_stats))
            with clock._lock:
                for st in clock.minute_stats[-5:]:
                    lines.append("    t=%6.1f min  deriva=%+8.2f us  (%+.3f ppm)" % (
                        st["elapsed_min"], st["drift_us"], st["drift_ppm"]))

            lines.append("")
            lines.append("  Grafico deriva (aggiornato ogni minuto):")
            lines.append(render_drift_chart(clock.drift_log))

            lines.append("")
            lines.append("  [Ctrl+C per uscire]  Aggiornamento #%d" % update_count)
            lines.append("=" * 70)

            sys.stdout.write("\033[H")
            sys.stdout.write("\n".join(lines))
            sys.stdout.flush()

            time.sleep(0.5)

    except KeyboardInterrupt:
        pass


# ── Analisi TSC finale ────────────────────────────────────────────────────────
def print_analysis(clock: TSCClock):
    print("\n\n" + "=" * 70)
    print("  ANALISI FINALE TSC")
    print("=" * 70)

    elapsed = clock.elapsed_seconds()
    drift   = clock.drift_us()

    if elapsed > 0:
        us_per_s = drift / elapsed
        print("\n  Osservazione durata: %.1f secondi" % elapsed)
        print("  Deriva totale osservata: %+.3f us" % drift)
        print("  Tasso di deriva: %+.4f us/s  (%+.4f ppm)" % (us_per_s, us_per_s))
        print()
        print("  Accuratezza stimata:")
        for label, secs in [("1 ora", 3600), ("1 giorno", 86400), ("1 settimana", 86400*7), ("1 mese", 86400*30)]:
            ms = us_per_s * secs / 1000
            print("    %-14s : %+.3f ms  (%+.1f us)" % (label, ms, ms*1000))

    if clock.minute_stats:
        with clock._lock:
            drifts_ppm = [s["drift_ppm"] for s in clock.minute_stats]
        print("\n  Statistiche per-minuto:")
        print("    campioni : %d" % len(drifts_ppm))
        print("    media    : %+.4f ppm" % (sum(drifts_ppm)/len(drifts_ppm)))
        print("    min      : %+.4f ppm" % min(drifts_ppm))
        print("    max      : %+.4f ppm" % max(drifts_ppm))
        if len(drifts_ppm) > 1:
            print("    std dev  : %.4f ppm" % statistics.stdev(drifts_ppm))

    print("\n  Grafico deriva finale:")
    print(render_drift_chart(clock.drift_log))
    print()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    print("\n  TSC Clock — Inizializzazione\n")
    clock = TSCClock(calibration_secs=3.0)
    print("\n  Avvio display... (Ctrl+C per terminare)\n")
    time.sleep(1)

    try:
        display_loop(clock)
    finally:
        clock.stop()
        print_analysis(clock)


if __name__ == "__main__":
    main()
