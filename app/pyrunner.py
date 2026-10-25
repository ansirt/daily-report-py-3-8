from __future__ import annotations

"""Dijalankan sebagai proses terpisah oleh node Skrip Python. Berdiri sendiri (tidak mengimpor aplikasi)."""
import contextlib
import io
import pickle
import sys
import traceback
from pathlib import Path


def to_table(df, pd, np):
    if isinstance(df, pd.Series):
        df = df.to_frame()
    if not isinstance(df, pd.DataFrame):
        raise ValueError(f"Hasil harus DataFrame, bukan {type(df).__name__}")
    cols = [str(c) for c in df.columns]
    vals = df.astype(object).where(df.notna(), None).values.tolist()

    def py(v):
        if v is None:
            return None
        if hasattr(v, "to_pydatetime"):
            return v.to_pydatetime()
        if hasattr(v, "to_pytimedelta"):
            return v.to_pytimedelta()
        if isinstance(v, np.generic):
            return v.item()
        return v

    return cols, [[py(v) for v in r] for r in vals]


def main(work):
    p = pickle.load(open(work / "input.pkl", "rb"))
    out, buf = {"ok": False, "stdout": ""}, io.StringIO()
    try:
        import numpy as np
        import pandas as pd
        dfs = [pd.DataFrame(rows, columns=cols) for cols, rows in p["inputs"]]
        g = {"pd": pd, "np": np, "inputs": dfs, "df": dfs[0] if dfs else None, "params": p["params"], "__name__": "__skrip__"}
        with contextlib.redirect_stdout(buf):
            exec(compile(p["script"], "<skrip>", "exec"), g)
        if g.get("result") is None:
            raise ValueError("Skrip harus mengisi variabel 'result' (DataFrame)")
        out["main"] = to_table(g["result"], pd, np)
        extra = g.get("extra") or {}
        out["extra"] = {}
        for name in p["extra"]:
            if name not in extra:
                raise ValueError(f"Port tambahan '{name}' belum diisi: tulis extra['{name}'] = DataFrame")
            out["extra"][name] = to_table(extra[name], pd, np)
        out["ok"] = True
    except BaseException:
        out["error"] = traceback.format_exc()[-4000:]
    out["stdout"] = buf.getvalue()[-20000:]
    pickle.dump(out, open(work / "output.pkl", "wb"), protocol=4)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
