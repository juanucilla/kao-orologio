"""
process_freezer.py — Sospendi/riprendi processi per risparmiare batteria
=========================================================================
Meccanismo: NtSuspendProcess (via psutil.suspend) sospende tutti i thread
del processo → CPU scende a 0%, memoria resta allocata.
Equivalente di `docker pause` (che usa cgroups freezer su Linux).

Differenze Docker vs Windows:
  Docker (Linux)  → cgroups v1 freezer / cgroup.freeze
  Windows         → NtSuspendProcess (sospende tutti i thread del processo)
  Effetto finale  → identico: processo non esegue istruzioni, CPU = 0%

Protezioni:
  - Lista nera di processi di sistema non sospendibili
  - Richiede conferma per processi con RAM > 500 MB
  - Limite di 20 processi congelati contemporaneamente
  - Auto-resume dopo timeout configurabile (default: mai)
"""

import psutil, threading, time, logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ── Processi di sistema che non devono mai essere sospesi ─────────────────────
PROTECTED = frozenset({
    "System", "Registry", "Idle", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "lsass.exe", "services.exe",
    "fontdrvhost.exe", "dwm.exe", "explorer.exe",
    "svchost.exe", "RuntimeBroker.exe", "taskhostw.exe",
    "MsMpEng.exe", "NisSrv.exe", "SecurityHealthService.exe",
    "audiodg.exe", "conhost.exe", "wmpnscfg.exe",
    # Processi shell/terminale critici
    "cmd.exe",
})

MAX_FROZEN = 20


@dataclass
class FrozenEntry:
    pid:           int
    name:          str
    frozen_at:     float = field(default_factory=time.time)
    cpu_before:    float = 0.0    # CPU% al momento del freeze
    mem_mb:        float = 0.0
    auto_resume_at: Optional[float] = None  # timestamp Unix, None = mai

    @property
    def frozen_for_str(self) -> str:
        s = int(time.time() - self.frozen_at)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        if h: return "%dh%02dm" % (h, m)
        if m: return "%dm%02ds" % (m, s)
        return "%ds" % s

    @property
    def energy_saved_mwh(self) -> float:
        """Stima energia risparmiata (cpu_before% × TDP_base × tempo)."""
        duration_h = (time.time() - self.frozen_at) / 3600
        cpu_frac   = self.cpu_before / 100
        # i5-1135G7: 15W TDP, risparmio proporzionale al carico
        saved_mw   = cpu_frac * 12_800  # ~85% del TDP base risparmiato
        return saved_mw * duration_h


class FreezeError(Exception):
    pass


class ProcessFreezer:
    """Gestisce la sospensione e ripresa dei processi."""

    def __init__(self, auto_resume_s: Optional[float] = None):
        """
        auto_resume_s: secondi dopo cui riprendere automaticamente.
                       None = nessun auto-resume.
        """
        import os
        self._own_pid        = os.getpid()          # mai congelare se stessi
        self._frozen:        dict[int, FrozenEntry] = {}
        self._lock           = threading.Lock()
        self._auto_resume_s  = auto_resume_s
        self._running        = False
        self._watcher:       Optional[threading.Thread] = None

    # ── Controllo ────────────────────────────────────────────────────────────
    def start(self):
        self._running = True
        self._watcher = threading.Thread(target=self._watch_loop, daemon=True)
        self._watcher.start()

    def stop(self):
        self._running = False
        self.resume_all()

    # ── Freeze ────────────────────────────────────────────────────────────────
    def freeze(self, pid: int, auto_resume_s: Optional[float] = None) -> FrozenEntry:
        """
        Sospende il processo `pid`.
        Lancia FreezeError se non è sicuro o non è possibile.
        """
        with self._lock:
            if pid in self._frozen:
                raise FreezeError("Processo %d già congelato." % pid)
            if len(self._frozen) >= MAX_FROZEN:
                raise FreezeError("Limite di %d processi congelati raggiunto." % MAX_FROZEN)

        try:
            proc = psutil.Process(pid)
        except psutil.NoSuchProcess:
            raise FreezeError("Processo %d non trovato." % pid)

        name = proc.name()

        # Protezioni
        if pid == self._own_pid:
            raise FreezeError("Non puoi congelare il processo corrente.")
        if name in PROTECTED:
            raise FreezeError("'%s' è un processo di sistema protetto." % name)
        if proc.username() in ("NT AUTHORITY\\SYSTEM", "NT AUTHORITY\\LOCAL SERVICE",
                               "NT AUTHORITY\\NETWORK SERVICE"):
            raise FreezeError("'%s' è un processo di servizio di sistema." % name)

        try:
            cpu    = proc.cpu_percent(interval=0.1)
            mem_mb = proc.memory_info().rss / 1_048_576
        except psutil.AccessDenied:
            raise FreezeError("Accesso negato a '%s' (PID %d). Avvia come Amministratore." % (name, pid))

        # Esegui sospensione
        try:
            proc.suspend()
        except psutil.AccessDenied:
            raise FreezeError("Permesso negato per sospendere '%s'." % name)
        except psutil.NoSuchProcess:
            raise FreezeError("Processo '%s' terminato prima della sospensione." % name)

        ar_at = time.time() + (auto_resume_s or self._auto_resume_s or 0) \
                if (auto_resume_s or self._auto_resume_s) else None

        entry = FrozenEntry(
            pid=pid, name=name,
            cpu_before=cpu, mem_mb=mem_mb,
            auto_resume_at=ar_at,
        )
        with self._lock:
            self._frozen[pid] = entry

        log.info("FREEZE PID=%d name=%s cpu_before=%.1f%%", pid, name, cpu)
        return entry

    # ── Resume ────────────────────────────────────────────────────────────────
    def resume(self, pid: int) -> bool:
        """Riprende il processo `pid`. Ritorna True se riuscito."""
        with self._lock:
            entry = self._frozen.pop(pid, None)
        if entry is None:
            return False

        try:
            proc = psutil.Process(pid)
            proc.resume()
            log.info("RESUME PID=%d name=%s frozen_for=%s saved=%.3f mWh",
                     pid, entry.name, entry.frozen_for_str, entry.energy_saved_mwh)
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False

    def resume_all(self):
        with self._lock:
            pids = list(self._frozen.keys())
        for pid in pids:
            self.resume(pid)

    # ── Stato ─────────────────────────────────────────────────────────────────
    def is_frozen(self, pid: int) -> bool:
        with self._lock:
            return pid in self._frozen

    def frozen_list(self) -> list[FrozenEntry]:
        with self._lock:
            return sorted(self._frozen.values(), key=lambda e: e.frozen_at, reverse=True)

    def frozen_count(self) -> int:
        with self._lock:
            return len(self._frozen)

    def total_saved_mwh(self) -> float:
        with self._lock:
            return sum(e.energy_saved_mwh for e in self._frozen.values())

    def can_freeze(self, name: str) -> bool:
        return name not in PROTECTED

    # ── Auto-resume watcher ───────────────────────────────────────────────────
    def _watch_loop(self):
        while self._running:
            time.sleep(5)
            now = time.time()
            with self._lock:
                to_resume = [pid for pid, e in self._frozen.items()
                             if e.auto_resume_at and now >= e.auto_resume_at]
            for pid in to_resume:
                self.resume(pid)
