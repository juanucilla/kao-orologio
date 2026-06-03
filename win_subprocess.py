"""
win_subprocess.py — Helper per subprocess senza finestra console su Windows.

Uso:
    from win_subprocess import run_hidden
    r = run_hidden(["powershell", "-Command", "..."], timeout=5)
"""

import subprocess


def _hidden_kwargs() -> dict:
    """
    Restituisce i kwargs che sopprimono la finestra console su Windows.
    Usa sia STARTUPINFO (SW_HIDE) sia CREATE_NO_WINDOW per massima
    compatibilità con tutti i build Python/Windows.
    """
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {
        "startupinfo": si,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def run_hidden(cmd: list, **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run() garantito senza finestra console visibile."""
    return subprocess.run(cmd, **_hidden_kwargs(), **kwargs)
