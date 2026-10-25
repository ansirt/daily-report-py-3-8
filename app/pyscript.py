from __future__ import annotations

"""Node Skrip Python (pandas / numpy). Berjalan di proses Python terpisah dengan batas waktu."""
import os
import pickle
import re
import signal
import subprocess
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path

ENABLED = os.getenv("ENABLE_PYTHON_SCRIPTS") == "1"
RUNNER = Path(__file__).with_name("pyrunner.py")
_KEEP_ENV = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL")
PORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,30}$")


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL) if hasattr(os, "killpg") else proc.kill()
    except OSError:
        pass


def extra_ports(cfg):
    return [n.strip() for n in re.split(r"[,\s]+", cfg.get("extra_ports") or "") if n.strip()]


def features():
    return {"python_scripts": ENABLED, "pandas": find_spec("pandas") is not None, "numpy": find_spec("numpy") is not None}


def validate_py(cfg):
    if not ENABLED:
        raise ValueError("Node Skrip Python dinonaktifkan di server. Pemilik server harus menyetel ENABLE_PYTHON_SCRIPTS=1 "
                         "(skrip adalah kode Python penuh) lalu memasang pandas dan numpy.")
    if not (cfg.get("script") or "").strip():
        raise ValueError("Skrip kosong")
    if not 1 <= int(cfg.get("timeout") or 120) <= 3600:
        raise ValueError("Timeout harus 1-3600 detik")
    for n in extra_ports(cfg):
        if not PORT_RE.match(n):
            raise ValueError(f"Nama port tambahan tidak valid: '{n}'")


def run_script(cfg, inputs, params):
    """inputs: [(kolom, baris)]. Mengembalikan (hasil_utama, {port: hasil}, stdout)."""
    if not ENABLED:
        raise ValueError("Node Skrip Python dinonaktifkan di server (ENABLE_PYTHON_SCRIPTS=1)")
    timeout = int(cfg.get("timeout") or 120)
    ports = extra_ports(cfg)
    env = {k: v for k, v in os.environ.items() if k in _KEEP_ENV}  # rahasia aplikasi tidak diwariskan
    with tempfile.TemporaryDirectory(prefix="pyscript_") as tmp:
        work = Path(tmp)
        with open(work / "input.pkl", "wb") as f:
            pickle.dump({"inputs": inputs, "params": params, "script": cfg["script"], "extra": ports}, f, protocol=4)
        proc = subprocess.Popen([sys.executable, "-I", "-X", "utf8", str(RUNNER), str(work)], cwd=work, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=hasattr(os, "killpg"))
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise ValueError(f"Skrip melebihi batas waktu {timeout} detik dan dihentikan")
        out_file = work / "output.pkl"
        if not out_file.exists():
            err = stderr.decode("utf-8", "replace")[-1500:]
            raise ValueError("Proses skrip berhenti tanpa hasil" + (f": {err}" if err else ""))
        with open(out_file, "rb") as f:
            out = pickle.load(f)
    if not out["ok"]:
        err = out["error"]
        if "No module named 'pandas'" in err or "No module named 'numpy'" in err:
            err += "\nPasang dengan: pip install pandas numpy"
        raise ValueError(f"Skrip gagal:\n{err}" + (f"\n--- keluaran skrip ---\n{out['stdout']}" if out["stdout"] else ""))
    return out["main"], out["extra"], out["stdout"]
