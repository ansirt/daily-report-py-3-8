from __future__ import annotations

"""Langkah turunan (data kecil, diproses di aplikasi): gabung, bandingkan, pivot. Tanpa pandas."""
from decimal import Decimal, InvalidOperation

from .datasets import to_memory

DERIVED = ("join", "merge", "pivot")  # "merge" = nama lama dari join
DEFAULT_MAX_ROWS = 1000


def _list(x):
    if isinstance(x, str):
        x = x.split(",")
    return [str(i).strip() for i in (x or []) if str(i).strip()]


def _norm(v):
    """Normalisasi nilai kunci agar 1, 1.0, '1' dan ' 1 ' dianggap sama."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (float, Decimal)):
        d = Decimal(str(v))
        return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
    return str(v).strip()


def _is_num(v):
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def _num(d):
    return int(d) if d == d.to_integral_value() else float(d)


def _idx(cols, name):
    for i, c in enumerate(cols):
        if str(c).lower() == str(name).lower():
            return i
    raise ValueError(f"Kolom '{name}' tidak ditemukan. Kolom tersedia: {', '.join(map(str, cols))}")


def get_ref(results, ref):
    """ref: nomor node (hasil utama) atau 'nomor:port' (mis. '2:left_only')."""
    if ref is None or ref == "":
        return None
    if isinstance(ref, str) and ":" in ref:
        return results.get(ref)
    try:
        return results.get(int(ref))
    except (TypeError, ValueError):
        return None


def _src(results, pos, label, cfg):
    data = get_ref(results, pos)
    if data is None:
        raise ValueError(f"{label}: sumber belum dipilih atau belum dijalankan")
    mx = int(cfg.get("max_rows") or DEFAULT_MAX_ROWS)
    cols, rows = to_memory(data, mx, label)
    if len(rows) > mx:
        raise ValueError(f"{label}: {len(rows)} baris melebihi batas {mx}. Untuk data besar gunakan langkah "
                         "'Simpan hasil ke database (staging)' lalu join lewat SQL di database tujuan.")
    return cols, rows


def validate_derived(kind, cfg):
    cfg = cfg or {}
    if kind == "pivot":
        for sp in pivot_specs(cfg):
            if not sp["column"]:
                raise ValueError("Kolom nilai pivot belum diisi")
            if sp["agg"] not in AGGS:
                raise ValueError(f"Fungsi pivot tidak dikenal: {sp['agg']}")
    need = {"join": ("left", "right", "left_on"), "merge": ("left", "right", "left_on"), "pivot": ("source", "index")}[kind]
    for k in need:
        v = cfg.get(k)
        if v is None or v == "" or v == []:
            raise ValueError(f"Pengaturan '{k}' belum diisi")


def run_derived(kind, cfg, results):
    """Mengembalikan (hasil_utama, {port_tambahan: hasil}). Hasil = (kolom, baris)."""
    out = {"join": _join, "merge": _join, "pivot": _pivot}[kind](cfg or {}, results)
    return out if isinstance(out[0], tuple) else (out, {})


def _uniq(name, taken):
    n, k = name, 2
    while n.lower() in taken:
        n = f"{name}_{'B' if k == 2 else k}"
        k += 1
    taken.add(n.lower())
    return n


def _key(row, idx):
    vals = [row[i] for i in idx]
    return None if any(v is None for v in vals) else tuple(_norm(v) for v in vals)  # NULL tidak pernah cocok


def _join(cfg, results):
    c2 = {**cfg, "max_rows": cfg.get("max_rows") or 100000}
    lc, lr = _src(results, cfg.get("left"), "Data kiri", c2)
    rc, rr = _src(results, cfg.get("right"), "Data kanan", c2)
    lk = _list(cfg.get("left_on"))
    rk = _list(cfg.get("right_on")) or lk
    if not lk or len(lk) != len(rk):
        raise ValueError("Kolom kunci kiri dan kanan harus sama jumlahnya")
    li, ri = [_idx(lc, c) for c in lk], [_idx(rc, c) for c in rk]
    sl, sr = _list(cfg.get("left_cols")), _list(cfg.get("right_cols"))
    lsel = [_idx(lc, c) for c in sl] if sl else list(range(len(lc)))
    rsel = [_idx(rc, c) for c in sr] if sr else [j for j in range(len(rc)) if j not in ri]
    taken = set()
    lnames = [_uniq(str(lc[i]), taken) for i in lsel]
    rnames = [_uniq(str(rc[j]), taken) for j in rsel]

    index, right_null = {}, []
    for m in rr:
        k = _key(m, ri)
        if k is None:
            right_null.append(m)
        else:
            index.setdefault(k, []).append(m)
    matched, left_only, seen = [], [], set()
    for l in lr:
        k = _key(l, li)
        ms = index.get(k) if k is not None else None
        if ms:
            seen.add(k)
            matched += [[l[i] for i in lsel] + [m[j] for j in rsel] for m in ms]
        else:
            left_only.append(l)
    right_only = [m for k, ms in index.items() if k not in seen for m in ms] + right_null
    cols = lnames + rnames
    lo = [[l[i] for i in lsel] for l in left_only]
    ro = [[m[j] for j in rsel] for m in right_only]
    if cfg.get("output") == "split":
        return (cols, matched), {"left_only": (lnames, lo), "right_only": (rnames, ro)}
    how = cfg.get("join_type") or cfg.get("how") or "left"
    rows = list(matched)
    if how in ("left", "outer"):
        rows += [r + [None] * len(rsel) for r in lo]
    if how in ("right", "outer"):
        for m in right_only:
            base = [None] * len(lsel)
            for a_, b_ in zip(li, ri):
                if a_ in lsel:
                    base[lsel.index(a_)] = m[b_]
            rows.append(base + [m[j] for j in rsel])
    return (cols, rows), {}


AGGS = ("sum", "count", "count_distinct", "avg", "min", "max", "first", "concat")
_NUMERIC = ("sum", "avg", "min", "max")


def pivot_specs(cfg):
    """Daftar kolom nilai [{column, agg}]. Format lama (value + agg) tetap didukung."""
    vals = cfg.get("values") or [{"column": cfg.get("value"), "agg": cfg.get("agg", "sum")}]
    return [{"column": (v.get("column") or "").strip(), "agg": v.get("agg") or "sum"} for v in vals]


def _pivot(cfg, results):
    cols, rows = _src(results, cfg.get("source"), "Sumber pivot", cfg)
    ii = [_idx(cols, c) for c in _list(cfg.get("index"))]
    hc = _idx(cols, cfg.get("column")) if (cfg.get("column") or "").strip() else None
    specs = pivot_specs(cfg)
    vi = [_idx(cols, sp["column"]) for sp in specs]
    label = lambda v: "(kosong)" if _norm(v) == "" else _norm(v)

    def val(v, k):
        try:
            return Decimal(str(v).strip())
        except InvalidOperation:
            raise ValueError(f"Nilai '{v}' pada kolom '{specs[k]['column']}' bukan angka (fungsi {specs[k]['agg']})")

    groups = {}
    for r in rows:
        cell = groups.setdefault(tuple(label(r[i]) for i in ii), {}).setdefault(label(r[hc]) if hc is not None else "", [[] for _ in specs])
        for k, i in enumerate(vi):
            cell[k].append(r[i])

    def calc(k, vs):
        agg = specs[k]["agg"]
        if agg == "count":
            return len(vs)
        vs = [v for v in vs if v is not None]
        if agg == "count_distinct":
            return len({_norm(v) for v in vs})
        if agg == "first":
            return vs[0] if vs else None
        if agg == "concat":
            return ", ".join(str(v) for v in vs) if vs else None
        nums = [val(v, k) for v in vs]
        if not nums:
            return None
        if agg == "sum":
            return _num(sum(nums))
        if agg == "avg":
            return float(sum(nums) / len(nums))
        return _num(min(nums) if agg == "min" else max(nums))

    multi = len(specs) > 1
    name = lambda k, base: f"{base} - {specs[k]['column']} ({specs[k]['agg']})" if multi else base
    heads = sorted({h for g in groups.values() for h in g}) if hc is not None else [""]
    total = bool(cfg.get("total", True)) and hc is not None
    out_cols = [cols[i] for i in ii]
    if hc is None:
        out_cols += [f"{sp['column']} ({sp['agg']})" for sp in specs]
    else:
        out_cols += [name(k, h) for h in heads for k in range(len(specs))]
        if total:
            out_cols += [name(k, "Total") for k in range(len(specs))]
    out = []
    for key in sorted(groups):
        g = groups[key]
        row = list(key)
        for h in heads:
            for k in range(len(specs)):
                fill = 0 if specs[k]["agg"] in ("sum", "count", "count_distinct") else None
                row.append(calc(k, g[h][k]) if h in g else fill)
        if total:
            row += [calc(k, [v for h in g for v in g[h][k]]) for k in range(len(specs))]
        out.append(row)
    return out_cols, out
