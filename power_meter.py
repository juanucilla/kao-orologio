"""
power_meter.py — Misura consumo energetico in mW
=================================================
Metodi disponibili (in ordine di precisione):

  1. CallNtPowerInformation — Rate in mW (SOLO su batteria)
     Fornisce il flusso istantaneo dalla batteria verso il sistema.
     Su AC restituisce UNKNOWN_RATE (-1 / 0x80000000).

  2. Delta RemainingCapacity / Δt — Stima mW da variazione mWh
     Funziona su batteria, risoluzione limitata dalla granularità
     del firmware (Celxpert L19C4PDC: 710 mWh granularity).

  3. Stima CPU TDP × utilizzo — Su AC o come fallback
     i5-1135G7: TDP base 15W, boost fino a 28W.
     Stima sistema: CPU + display (~3W) + base (~3W) = ~21W totale.

  4. Tensione × corrente (futuro) — Richiederebbe driver RAPL/EMI.
"""

import ctypes, ctypes.wintypes, time, threading
from dataclasses import dataclass, field
from typing import Callable, Optional
import psutil


# ─── Struttura Windows SystemBatteryState ─────────────────────────────────────
class _SYSTEM_BATTERY_STATE(ctypes.Structure):
    _fields_ = [
        ("AcOnLine",          ctypes.c_bool),
        ("BatteryPresent",    ctypes.c_bool),
        ("Charging",          ctypes.c_bool),
        ("Discharging",       ctypes.c_bool),
        ("Spare1",            ctypes.c_byte * 4),
        ("Tag",               ctypes.c_ulong),
        ("MaxCapacity",       ctypes.c_ulong),   # mWh
        ("RemainingCapacity", ctypes.c_ulong),   # mWh
        ("Rate",              ctypes.c_long),    # mW (<0 discharge, >0 charge)
        ("EstimatedTime",     ctypes.c_ulong),   # secondi
        ("DefaultAlert1",     ctypes.c_ulong),
        ("DefaultAlert2",     ctypes.c_ulong),
    ]

_powrprof = ctypes.windll.powrprof
_UNKNOWN_RATE = -1   # sentinella Windows quando non disponibile

# i5-1135G7 Tiger Lake: TDP configurabile 12–28W
_TDP_IDLE_MW  = 2_200    # mW in idle (C-state profondo)
_TDP_BASE_MW  = 15_000   # mW TDP nominale (PL1)
_TDP_BOOST_MW = 28_000   # mW TDP boost (PL2, breve)
_DISPLAY_MW   = 2_500    # mW stima display a 100% luminosità
_BASE_SYSTEM_MW = 1_800  # mW chipset, SSD, RAM, WiFi


@dataclass
class PowerSample:
    timestamp:        float = 0.0
    method:           str   = "unknown"      # "battery_rate"|"delta"|"tdp_estimate"
    power_mw:         float = 0.0            # consumo sistema stimato (mW)
    battery_rate_mw:  float = 0.0            # rate diretto da batteria (0 se AC)
    cpu_load_pct:     float = 0.0
    estimated_cpu_mw: float = 0.0
    remaining_mwh:    int   = 0
    on_ac:            bool  = True
    is_charging:      bool  = False
    voltage_mv:       int   = 0

    @property
    def power_w(self) -> float:
        return self.power_mw / 1000.0

    @property
    def power_str(self) -> str:
        if self.power_mw <= 0:
            return "N/D"
        return "%.1f W  (%.0f mW)" % (self.power_w, self.power_mw)

    @property
    def method_label(self) -> str:
        return {
            "battery_rate": "Batteria (diretto)",
            "delta":        "Batteria (Δ mWh/s)",
            "tdp_estimate": "Stima CPU TDP",
            "unknown":      "N/D",
        }.get(self.method, self.method)


class PowerMeter:
    """
    Campiona il consumo energetico ogni `interval_s` secondi.
    Usa il metodo più preciso disponibile per la situazione corrente.
    """

    def __init__(self, interval_s: float = 3.0,
                 brightness_cb: Optional[Callable[[], Optional[int]]] = None):
        self.interval        = interval_s
        self._brightness_cb  = brightness_cb  # () -> int|None, non bloccante
        self.current:        PowerSample | None = None
        self.history:        list[PowerSample] = []
        self._prev_cap:      int | None = None
        self._prev_t:        float | None = None
        self._running        = False
        self._lock           = threading.Lock()
        # Inizializza psutil cpu_percent (prima chiamata restituisce 0)
        psutil.cpu_percent(interval=None)

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    # ── Lettura SystemBatteryState ────────────────────────────────────────────
    def _read_battery_state(self) -> _SYSTEM_BATTERY_STATE | None:
        buf = _SYSTEM_BATTERY_STATE()
        ret = _powrprof.CallNtPowerInformation(
            5, None, 0, ctypes.byref(buf), ctypes.sizeof(buf))
        return buf if ret == 0 else None

    # ── CPU load via psutil (istantaneo, no subprocess) ──────────────────────
    def _read_cpu_load(self) -> float:
        return psutil.cpu_percent(interval=0.2)

    # ── Stima potenza da TDP + carico ────────────────────────────────────────
    def _estimate_from_tdp(self, cpu_pct: float, brightness: int = 100) -> float:
        # Interpolazione lineare idle → base con boost per picchi
        if cpu_pct < 10:
            cpu_mw = _TDP_IDLE_MW + (cpu_pct / 10) * (_TDP_BASE_MW - _TDP_IDLE_MW) * 0.3
        elif cpu_pct < 80:
            cpu_mw = _TDP_IDLE_MW + (cpu_pct / 100) * (_TDP_BASE_MW - _TDP_IDLE_MW)
        else:
            # Aggiunge contributo boost
            cpu_mw = _TDP_BASE_MW + ((cpu_pct - 80) / 20) * (_TDP_BOOST_MW - _TDP_BASE_MW) * 0.4
        display_mw = _DISPLAY_MW * (brightness / 100)
        return cpu_mw + display_mw + _BASE_SYSTEM_MW

    # ── Loop principale ───────────────────────────────────────────────────────
    def _loop(self):
        while self._running:
            sample = self._sample()
            with self._lock:
                self.current = sample
                self.history.append(sample)
                if len(self.history) > 3600:
                    self.history = self.history[-3600:]
            time.sleep(self.interval)

    def _sample(self) -> PowerSample:
        now  = time.time()
        bst  = self._read_battery_state()
        cpu  = self._read_cpu_load()

        s = PowerSample(timestamp=now, cpu_load_pct=cpu)

        if bst:
            s.on_ac       = bst.AcOnLine
            s.is_charging = bst.Charging
            s.remaining_mwh = bst.RemainingCapacity
            s.voltage_mv  = 0

        # ── Metodo 1: Rate diretto dalla batteria (solo su batteria) ──────────
        if bst and not bst.AcOnLine and bst.Rate != _UNKNOWN_RATE and bst.Rate < 0:
            s.method         = "battery_rate"
            s.battery_rate_mw = abs(bst.Rate)
            s.power_mw        = abs(bst.Rate)

        # ── Metodo 2: Delta mWh/s (su batteria, rate = 0 o sconosciuto) ──────
        elif bst and not bst.AcOnLine:
            cap = bst.RemainingCapacity
            if self._prev_cap is not None and self._prev_t is not None:
                dt_h = (now - self._prev_t) / 3600.0
                if dt_h > 0:
                    delta_mwh = self._prev_cap - cap
                    mw = delta_mwh / dt_h if dt_h > 0 else 0.0
                    if mw > 0:
                        s.method   = "delta"
                        s.power_mw = mw
            self._prev_cap = cap
            self._prev_t   = now

        # ── Metodo 3: Stima TDP (su AC o fallback) ────────────────────────────
        if s.method == "unknown" or (s.on_ac and s.power_mw == 0):
            if self._brightness_cb:
                b = self._brightness_cb()
                brightness = b if b is not None else 100
            else:
                brightness = self._get_brightness()
            est = self._estimate_from_tdp(cpu, brightness)
            s.method          = "tdp_estimate"
            s.estimated_cpu_mw = est
            s.power_mw         = est

        return s

    @staticmethod
    def _get_brightness() -> int:
        try:
            import win32com.client
            svc = win32com.client.GetObject("winmgmts://./root/wmi")
            for obj in svc.ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness"):
                return int(obj.CurrentBrightness)
        except Exception:
            pass
        return 100

    # ── Statistiche ───────────────────────────────────────────────────────────
    def avg_power_mw(self, last_n: int = 20) -> float:
        with self._lock:
            samples = [s.power_mw for s in self.history[-last_n:] if s.power_mw > 0]
        return sum(samples) / len(samples) if samples else 0.0

    def energy_used_mwh(self) -> float:
        """Energia totale consumata dall'avvio del meter (mWh)."""
        with self._lock:
            hist = list(self.history)
        if len(hist) < 2: return 0.0
        total = 0.0
        for i in range(1, len(hist)):
            dt_h = (hist[i].timestamp - hist[i-1].timestamp) / 3600.0
            avg_mw = (hist[i].power_mw + hist[i-1].power_mw) / 2
            total += avg_mw * dt_h
        return total

    def estimated_runtime_h(self, remaining_mwh: int) -> float:
        avg = self.avg_power_mw(10)
        return remaining_mwh / avg if avg > 0 else 0.0
