"""
process_power.py — Consumo energetico stimato per processo
===========================================================
Modello energetico (stesso approccio di Windows Task Manager):

  P_process = P_cpu_share + P_disk_share + P_mem_share

  P_cpu_share  = (proc_cpu% / sys_cpu_active%) × (sys_cpu_mw - cpu_idle_mw)
  P_disk_share = (proc_io_bytes_s / total_io_bytes_s) × ssd_active_mw
  P_mem_share  = proc_rss_gb × mem_mw_per_gb (contributo minore)

Costanti hardware (i5-1135G7, NVMe SSD, 16GB DDR4):
  CPU TDP base   : 15 W  → idle package ~2.2 W
  NVMe SSD       : picco 4 W, idle 0.05 W
  DDR4 16GB      : ~3 W totali (0.19 W/GB attivo)
  Display 100%   : ~2.5 W
  Base sistema   : ~1.8 W (chipset, WiFi, USB…)

Classificazione (coerente con Task Manager):
  Molto basso  < 0.3 W
  Basso       < 1.0 W
  Moderato    < 3.0 W
  Alto        < 7.0 W
  Molto alto  ≥ 7.0 W
"""

import psutil, time, threading
from dataclasses import dataclass, field
from typing import Optional

# ── Costanti hardware i5-1135G7 ───────────────────────────────────────────────
CPU_TDP_MW       = 15_000   # mW TDP PL1
CPU_IDLE_MW      = 2_200    # mW package idle
SSD_PEAK_MW      = 4_000    # mW NVMe SSD picco I/O
SSD_IDLE_MW      = 50       # mW NVMe SSD idle
MEM_MW_PER_GB    = 190      # mW per GB DDR4 attivo
DISPLAY_MW_FULL  = 2_500    # mW display a luminosità 100%
BASE_SYSTEM_MW   = 1_800    # mW base (chipset, WiFi, USB)

# ── Soglie classificazione (mW) ───────────────────────────────────────────────
THRESHOLDS = [
    (300,   "Molto basso", "▁"),
    (1_000, "Basso",       "▂"),
    (3_000, "Moderato",    "▃"),
    (7_000, "Alto",        "▄"),
    (float("inf"), "Molto alto", "▅"),
]


@dataclass
class ProcessPowerSample:
    pid:            int    = 0
    name:           str    = ""
    cpu_pct:        float  = 0.0   # % CPU istantaneo
    mem_mb:         float  = 0.0   # MB RSS
    disk_read_bs:   float  = 0.0   # byte/s lettura
    disk_write_bs:  float  = 0.0   # byte/s scrittura
    # Stima energetica (mW)
    cpu_mw:         float  = 0.0
    disk_mw:        float  = 0.0
    mem_mw:         float  = 0.0
    total_mw:       float  = 0.0
    # Classificazione
    label:          str    = "N/D"
    bar:            str    = "▁"
    elapsed_str:    str    = ""
    start_time:     float  = 0.0

    @property
    def total_w(self) -> float:
        return self.total_mw / 1000.0

    @property
    def power_str(self) -> str:
        if self.total_mw < 10:
            return "<0.01 W"
        if self.total_mw < 1000:
            return "%.0f mW" % self.total_mw
        return "%.2f W" % self.total_w

    @property
    def cpu_str(self) -> str:
        return "%.1f%%" % self.cpu_pct

    @property
    def disk_str(self) -> str:
        total = self.disk_read_bs + self.disk_write_bs
        if total > 1_048_576:   return "%.1f MB/s" % (total / 1_048_576)
        if total > 1_024:       return "%.0f KB/s" % (total / 1_024)
        if total > 0:           return "%.0f B/s" % total
        return "—"


def _classify(mw: float) -> tuple[str, str]:
    for thresh, label, bar in THRESHOLDS:
        if mw < thresh:
            return label, bar
    return "Molto alto", "▅"


class ProcessPowerMeter:
    """
    Campiona CPU%, disk I/O e memoria per ogni processo ogni `interval_s`
    secondi e calcola la stima di consumo in mW.

    La stima usa il consumo totale del sistema fornito da `PowerMeter`
    come ancora: la somma dei contributi CPU non supera mai il totale
    misurato, distribuendolo proporzionalmente al carico.
    """

    def __init__(self, interval_s: float = 2.0,
                 system_power_cb=None):
        """
        system_power_cb: callable() → float (mW totale sistema)
                         Se None usa stima da TDP.
        """
        self.interval         = interval_s
        self._sys_power_cb    = system_power_cb
        self.samples:         dict[int, ProcessPowerSample] = {}
        self._prev_io:        dict[int, tuple[int,int,float]] = {}  # pid→(r,w,t)
        self._prev_cpu_times: dict[int, tuple[float,float]] = {}    # pid→(user,sys,t)
        self._lock            = threading.Lock()
        self._running         = False
        self._thread:         Optional[threading.Thread] = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ── Loop principale ───────────────────────────────────────────────────────
    def _loop(self):
        # Primo campionamento per inizializzare delta
        self._init_baseline()
        time.sleep(self.interval)

        while self._running:
            t_start = time.time()
            new_samples = self._sample_all()
            with self._lock:
                self.samples = new_samples
            elapsed = time.time() - t_start
            sleep_t = max(0.1, self.interval - elapsed)
            time.sleep(sleep_t)

    def _init_baseline(self):
        now = time.time()
        for p in psutil.process_iter(["pid"]):
            try:
                pid = p.pid
                io  = p.io_counters()
                self._prev_io[pid] = (io.read_bytes, io.write_bytes, now)
            except Exception:
                pass

    # ── Campionamento ─────────────────────────────────────────────────────────
    def _sample_all(self) -> dict[int, ProcessPowerSample]:
        now = time.time()

        # 1. Raccogli dati grezzi da psutil
        raw: dict[int, dict] = {}
        for p in psutil.process_iter([
            "pid", "name", "cpu_percent", "memory_info", "create_time"
        ]):
            try:
                info    = p.info
                pid     = info["pid"]
                if pid in (0, 4): continue

                # Disk I/O delta
                try:
                    io     = p.io_counters()
                    r_bs   = w_bs = 0.0
                    if pid in self._prev_io:
                        pr, pw, pt = self._prev_io[pid]
                        dt = now - pt
                        if dt > 0:
                            r_bs = max(0, io.read_bytes  - pr) / dt
                            w_bs = max(0, io.write_bytes - pw) / dt
                    self._prev_io[pid] = (io.read_bytes, io.write_bytes, now)
                except Exception:
                    r_bs = w_bs = 0.0

                mem_info = info.get("memory_info")
                raw[pid] = {
                    "name":      info.get("name", "?"),
                    "cpu_pct":   info.get("cpu_percent") or 0.0,
                    "mem_mb":    mem_info.rss / 1_048_576 if mem_info else 0.0,
                    "r_bs":      r_bs,
                    "w_bs":      w_bs,
                    "create_t":  info.get("create_time", now),
                }
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # 2. Totali di sistema (per normalizzazione)
        total_cpu_pct  = sum(d["cpu_pct"] for d in raw.values())
        total_io_bs    = sum(d["r_bs"] + d["w_bs"] for d in raw.values())
        total_mem_mb   = sum(d["mem_mb"] for d in raw.values())

        # Evita divisione per zero
        if total_cpu_pct < 1:   total_cpu_pct = 1
        if total_io_bs   < 1:   total_io_bs   = 1
        if total_mem_mb  < 1:   total_mem_mb  = 1

        # 3. Consumo CPU disponibile per distribuzione ai processi
        sys_mw = self._sys_power_cb() if self._sys_power_cb else None
        if sys_mw and sys_mw > 0:
            # Usa il consumo reale del sistema come budget da distribuire
            cpu_budget_mw = max(0, sys_mw - BASE_SYSTEM_MW - DISPLAY_MW_FULL * 0.5)
        else:
            # Fallback: stima da TDP
            cpu_budget_mw = CPU_TDP_MW - CPU_IDLE_MW

        # 4. Calcola stima per processo
        result: dict[int, ProcessPowerSample] = {}
        for pid, d in raw.items():
            # Quota CPU (proporzionale al carico)
            cpu_share  = d["cpu_pct"] / total_cpu_pct
            cpu_mw     = cpu_share * cpu_budget_mw

            # Quota disco
            io_total_bs = d["r_bs"] + d["w_bs"]
            disk_share  = io_total_bs / total_io_bs
            disk_mw     = disk_share * SSD_PEAK_MW * min(1.0, total_io_bs / 50_000_000)

            # Quota memoria (effetto minore)
            mem_share   = d["mem_mb"] / total_mem_mb
            mem_mw      = mem_share * (MEM_MW_PER_GB * 16) * 0.2  # 20% memoria è "attiva"

            total_mw    = cpu_mw + disk_mw + mem_mw

            # Elapsed
            elapsed     = now - d["create_t"]
            h, rem      = divmod(int(elapsed), 3600)
            m, s        = divmod(rem, 60)
            el_str      = ("%dh%02dm" % (h,m)) if h else ("%dm%02ds" % (m,s))

            lbl, bar    = _classify(total_mw)

            result[pid] = ProcessPowerSample(
                pid           = pid,
                name          = d["name"],
                cpu_pct       = d["cpu_pct"],
                mem_mb        = d["mem_mb"],
                disk_read_bs  = d["r_bs"],
                disk_write_bs = d["w_bs"],
                cpu_mw        = cpu_mw,
                disk_mw       = disk_mw,
                mem_mw        = mem_mw,
                total_mw      = total_mw,
                label         = lbl,
                bar           = bar,
                elapsed_str   = el_str,
                start_time    = d["create_t"],
            )

        return result

    # ── Accessori ────────────────────────────────────────────────────────────
    def top_by_power(self, n: int = 12) -> list[ProcessPowerSample]:
        with self._lock:
            return sorted(self.samples.values(),
                          key=lambda s: s.total_mw, reverse=True)[:n]

    def top_by_cpu(self, n: int = 12) -> list[ProcessPowerSample]:
        with self._lock:
            return sorted(self.samples.values(),
                          key=lambda s: s.cpu_pct, reverse=True)[:n]

    def total_estimated_mw(self) -> float:
        with self._lock:
            return sum(s.total_mw for s in self.samples.values())

    def count(self) -> int:
        with self._lock:
            return len(self.samples)

    def get(self, pid: int) -> Optional[ProcessPowerSample]:
        with self._lock:
            return self.samples.get(pid)
