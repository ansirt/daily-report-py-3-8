from __future__ import annotations

"""Node Export CSV / Import CSV."""
import csv
import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

from .config import CSV_DIR
from .datasets import DRY_SAMPLE, CsvFile, open_stream

EXPORT_DIR = CSV_DIR / "exports"
_KINDS = {"int": "int", "integer": "int", "num": "num", "float": "num", "decimal": "num", "number": "num",
          "str": "str", "text": "str", "string": "str", "date": "date", "ts": "ts", "datetime": "ts", "timestamp": "ts"}


def safe_csv_name(name):
    n = re.sub(r"[\\/:*?\"<>|]+", "_", (name or "").strip()).strip(". ")
    if not n:
        raise ValueError("Nama file CSV kosong")
    return n if n.lower().endswith(".csv") else n + ".csv"


def check_delim(d):
    d = "\t" if d in ("\\t", "tab", "TAB") else (d or ",")
    if len(d) != 1:
        raise ValueError("Pemisah (delimiter) harus satu karakter, atau 'tab'")
    return d


def check_encoding(e):
    e = (e or "utf-8").strip()
    try:
        "x".encode(e)
    except LookupError:
        raise ValueError(f"Encoding tidak dikenal: {e}")
    return e


def resolve_csv(name):
    """Hanya file di bawah folder CSV aplikasi yang boleh dibaca."""
    p = (CSV_DIR / (name or "")).resolve()
    if CSV_DIR.resolve() not in p.parents:
        raise ValueError("File CSV harus berada di folder CSV aplikasi")
    if not p.is_file():
        raise ValueError(f"File CSV tidak ditemukan: {name}")
    return p


def parse_types(text):
    """'qty=int, harga=num, tgl=date:%d/%m/%Y, dibuat=ts' -> {kolom: (jenis, format)}"""
    out = {}
    for it in re.split(r"[;\n,]+", text or ""):
        it = it.strip()
        if not it:
            continue
        col, sep, spec = it.partition("=")
        kind, _, fmt = spec.strip().partition(":")
        if not sep or _KINDS.get(kind.strip().lower()) is None:
            raise ValueError(f"Tipe kolom tidak valid: '{it}' (pakai kolom=int|num|str|date|ts)")
        out[col.strip().lower()] = (_KINDS[kind.strip().lower()], fmt or None)
    return out


def validate_csv_export(cfg):
    if cfg.get("source") in (None, ""):
        raise ValueError("Data sumber belum dipilih")
    safe_csv_name(cfg.get("filename"))
    check_delim(cfg.get("delimiter"))
    check_encoding(cfg.get("encoding"))


def validate_csv_import(cfg):
    if cfg.get("source") in (None, "") and not cfg.get("file"):
        raise ValueError("Pilih node Export CSV sebagai sumber, atau pilih file CSV")
    check_delim(cfg.get("delimiter"))
    check_encoding(cfg.get("encoding"))
    parse_types(cfg.get("types"))


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v, "f")
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


def _kind_of(types):
    ks = set()
    for t in types:
        if issubclass(t, (bool, int)):
            ks.add("int")
        elif issubclass(t, (float, Decimal)):
            ks.add("num")
        elif issubclass(t, datetime):
            ks.add("ts")
        elif issubclass(t, date):
            ks.add("date")
        else:
            ks.add("str")
    if not ks or "str" in ks:
        return "str"
    if ks <= {"int"}:
        return "int"
    if ks <= {"int", "num"}:
        return "num"
    return "date" if ks == {"date"} else "ts" if ks <= {"ts", "date"} else "str"


def export_csv(data, job_key, cfg, dry=False):
    delim, enc = check_delim(cfg.get("delimiter")), check_encoding(cfg.get("encoding"))
    header = cfg.get("header", True) is not False
    name = safe_csv_name(cfg.get("filename"))
    if dry:  # jangan menimpa file export asli
        name = name[:-4] + ".dryrun.csv"
    folder = EXPORT_DIR / job_key
    folder.mkdir(parents=True, exist_ok=True)
    path, tmp = folder / name, folder / (name + ".tmp")
    n = 0
    try:
        with open_stream(data, DRY_SAMPLE if dry else None) as st, open(tmp, "w", newline="", encoding=enc) as f:
            w = csv.writer(f, delimiter=delim)
            cols = list(st.cols)
            seen = [set() for _ in cols]
            if header:
                w.writerow(cols)
            for chunk in st.chunks():
                for row in chunk:
                    for j, v in enumerate(row):
                        if v is not None:
                            seen[j].add(type(v))
                    w.writerow([_fmt(v) for v in row])
                n += len(chunk)
        kinds = [_kind_of(s) for s in seen]
        meta = {"columns": cols, "types": kinds, "rows": n, "delimiter": delim, "encoding": enc, "header": header}
        (folder / (name + ".meta.json")).write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return CsvFile(path, cols, delim, enc, header, {c.lower(): (k, None) for c, k in zip(cols, kinds)}, True, n)


def make_import(cfg, results, get_ref):
    """Node Import CSV: dari node Export CSV (source) atau dari file di folder CSV."""
    overrides = parse_types(cfg.get("types"))
    empty = cfg.get("empty_as_null", True) is not False
    src = get_ref(results, cfg.get("source")) if cfg.get("source") not in (None, "") else None
    if src is not None:
        if not isinstance(src, CsvFile):
            raise ValueError("Sumber node Import CSV harus node Export CSV (atau pilih file CSV)")
        ds = CsvFile(src.path, src.cols, src.delimiter, src.encoding, src.header, {**src.kinds, **overrides}, empty, src.rows_total)
        return ds
    path = resolve_csv(cfg.get("file"))
    meta_file = path.with_name(path.name + ".meta.json")
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else None
    if meta:
        kinds = {c.lower(): (k, None) for c, k in zip(meta["columns"], meta["types"])}
        delim, enc, header, cols = meta["delimiter"], meta["encoding"], meta["header"], meta["columns"]
        rows_total = meta.get("rows")
    else:
        kinds, cols, rows_total = {}, None, None
        delim, enc, header = check_delim(cfg.get("delimiter")), check_encoding(cfg.get("encoding")), cfg.get("header", True) is not False
    return CsvFile(path, cols, delim, enc, header, {**kinds, **overrides}, empty, rows_total)
