from __future__ import annotations

"""Dataset non-memori: query yang dialirkan (stream) dan file CSV. Dipakai untuk data sangat besar (> 1 juta baris)."""
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import chain

from . import dbconnect

CHUNK = 5000
DRY_SAMPLE = 1000  # saat dry-run, sumber stream hanya dibaca sebanyak ini


class Handle:
    def __init__(self, cols, gen, closer=None):
        self.cols, self._gen, self._closer = cols, gen, closer

    def chunks(self):
        return self._gen

    def close(self):
        try:
            self._gen.close()
        finally:
            if self._closer:
                self._closer()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class QueryStream:
    """Query yang baru dijalankan ketika dikonsumsi, hasilnya dibaca bertahap (tidak ditampung di memori)."""

    def __init__(self, info, sql):
        self.info, self.sql = info, sql

    def open(self, limit=None):
        sql = dbconnect.validate_readonly(self.sql)
        conn = dbconnect.open_connection(self.info, stream=True)
        done = []

        def closer():
            if not done:
                done.append(1)
                try:
                    conn.close()
                except Exception:
                    pass

        try:
            cur = conn.cursor()
            if self.info.db_type == "oracle":
                cur.arraysize = CHUNK
            cur.execute(sql)
            cols = [d[0] for d in (cur.description or [])]
        except Exception:
            closer()
            raise

        def gen():
            n = 0
            while limit is None or n < limit:
                rows = cur.fetchmany(CHUNK if limit is None else min(CHUNK, limit - n))
                if not rows:
                    break
                n += len(rows)
                yield [list(r) for r in rows]

        return Handle(cols, gen(), closer)


def _converter(kind, fmt, empty_null):
    def conv(v):
        if v == "":
            return None if (empty_null or kind != "str") else ""
        try:
            if kind == "int":
                return int(v)
            if kind == "num":
                return Decimal(v)
            if kind == "ts":
                return datetime.strptime(v, fmt) if fmt else datetime.fromisoformat(v)
            if kind == "date":
                return datetime.strptime(v, fmt).date() if fmt else date.fromisoformat(v)
        except (ValueError, InvalidOperation):
            raise ValueError(f"nilai '{v[:40]}' bukan {kind}" + (f" (format {fmt})" if fmt else ""))
        return v
    return conv


class CsvFile:
    """File CSV yang dibaca bertahap. kinds: {nama_kolom_huruf_kecil: (jenis, format)} dengan jenis int/num/str/date/ts."""

    def __init__(self, path, cols=None, delimiter=",", encoding="utf-8", header=True, kinds=None,
                 empty_as_null=True, rows_total=None):
        self.path, self.cols, self.delimiter, self.encoding = path, cols, delimiter, encoding
        self.header, self.kinds, self.empty_as_null, self.rows_total = header, kinds or {}, empty_as_null, rows_total

    def open(self, limit=None):
        f = open(self.path, "r", newline="", encoding=self.encoding)
        rd = csv.reader(f, delimiter=self.delimiter)
        try:
            first = next(rd, None)
            if first is None:
                raise ValueError(f"File CSV kosong: {self.path.name}")
            if self.header:
                cols, pending = [c.lstrip("\ufeff") for c in first], []
            else:
                cols = self.cols or [f"COL{i + 1}" for i in range(len(first))]
                pending = [first]
        except Exception:
            f.close()
            raise
        conv = [_converter(*self.kinds.get(c.lower(), ("str", None)), self.empty_as_null) for c in cols]
        names = list(cols)

        def gen():
            n, line, buf = 0, 1 if self.header else 0, []
            for row in chain(pending, rd):
                line += 1
                if not row:
                    continue
                if limit is not None and n >= limit:
                    break
                if len(row) != len(names):
                    raise ValueError(f"Baris {line}: jumlah kolom {len(row)} (seharusnya {len(names)})")
                try:
                    buf.append([c(v) for c, v in zip(conv, row)])
                except ValueError as e:
                    raise ValueError(f"Baris {line}: {e}")
                n += 1
                if len(buf) >= CHUNK:
                    yield buf
                    buf = []
            if buf:
                yield buf

        return Handle(names, gen(), f.close)


def open_stream(data, limit=None):
    """data: (kolom, baris) di memori, QueryStream, atau CsvFile -> Handle(cols, chunks())."""
    if isinstance(data, tuple):
        cols, rows = data
        rows = rows if limit is None else rows[:limit]
        return Handle(cols, ([list(r) for r in rows[i:i + CHUNK]] for i in range(0, len(rows), CHUNK)))
    return data.open(limit)


def to_memory(data, max_rows, label="Sumber"):
    if isinstance(data, tuple):
        return data
    rows = []
    with data.open(None) as st:
        for chunk in st.chunks():
            rows.extend(chunk)
            if len(rows) > max_rows:
                raise ValueError(f"{label}: data stream melebihi {max_rows} baris untuk diproses di memori. "
                                 "Gunakan node Export CSV atau Tulis ke database untuk data sebesar ini.")
        return st.cols, rows
