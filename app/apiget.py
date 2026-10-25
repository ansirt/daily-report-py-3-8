from __future__ import annotations

"""Node Get Data dari REST API (JSON) -> tabel."""
import base64
import json
import re
import ssl
import urllib.error
import urllib.request

from .security import decrypt

MAX_BODY = 50 * 1024 * 1024
AUTHS = ("none", "bearer", "basic", "apikey")
PAGINATIONS = ("none", "page", "offset", "next_url")
SECRETS = (("token", "token_enc"), ("password", "password_enc"), ("apikey_value", "apikey_enc"))


def get_path(obj, path):
    if path in (None, ""):
        return obj
    for part in str(path).split("."):
        if isinstance(obj, list):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def flatten(rec, prefix="", out=None):
    out = {} if out is None else out
    for k, v in rec.items():
        key = f"{prefix}_{k}" if prefix else str(k)
        if isinstance(v, dict):
            flatten(v, key, out)
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def parse_headers(text):
    out = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        k, sep, v = line.partition(":")
        if not sep or not k.strip():
            raise ValueError(f"Header tidak valid: '{line.strip()}' (format Nama: nilai)")
        out[k.strip()] = v.strip()
    return out


def parse_columns(text):
    out = []
    for it in re.split(r"[\n;]+", text or ""):
        it = it.strip()
        if it:
            name, _, path = it.partition("=")
            out.append((name.strip(), (path or name).strip()))
    return out


def validate_api(cfg):
    if not re.match(r"^https?://", cfg.get("url") or ""):
        raise ValueError("URL harus diawali http:// atau https://")
    if cfg.get("method", "GET") not in ("GET", "POST"):
        raise ValueError("Method harus GET atau POST")
    if cfg.get("auth", "none") not in AUTHS:
        raise ValueError("Jenis autentikasi tidak dikenal")
    if cfg.get("pagination", "none") not in PAGINATIONS:
        raise ValueError("Jenis pagination tidak dikenal")
    if cfg.get("pagination") in ("page", "offset") and not int(cfg.get("page_size") or 0):
        raise ValueError("Ukuran halaman (page_size) wajib diisi untuk pagination page/offset")
    if cfg.get("pagination") == "next_url" and not cfg.get("next_path"):
        raise ValueError("Path 'next url' wajib diisi")
    parse_headers(cfg.get("headers"))
    parse_columns(cfg.get("columns"))


def secure_config(new, old):
    """Rahasia (token/password/api key) disimpan terenkripsi; kosong = pertahankan yang lama."""
    from .security import encrypt
    old = old or {}
    cfg = {k: v for k, v in new.items() if k not in ("token_set", "password_set", "apikey_set") and not k.endswith("_enc")}
    for plain, enc in SECRETS:
        val = cfg.pop(plain, "")
        cfg[enc] = encrypt(val) if val else old.get(enc, "")
    return cfg


def public_config(cfg):
    out = {k: v for k, v in cfg.items() if not k.endswith("_enc")}
    out["token_set"], out["password_set"], out["apikey_set"] = (bool(cfg.get("token_enc")), bool(cfg.get("password_enc")),
                                                                 bool(cfg.get("apikey_enc")))
    return out


def _secret(cfg, plain, enc):
    return cfg.get(plain) or decrypt(cfg.get(enc, ""))


def _request(url, method, headers, body, timeout, verify):
    if not re.match(r"^https?://", url):
        raise ValueError(f"URL tidak valid: {url[:80]}")
    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    ctx = None if verify else ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ValueError("Respons API lebih dari 50 MB per halaman; kecilkan ukuran halaman")
            return json.loads(raw.decode(resp.headers.get_content_charset() or "utf-8"))
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} dari API: {e.read(300).decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise ValueError(f"Gagal menghubungi API: {e.reason}")
    except json.JSONDecodeError:
        raise ValueError("Respons API bukan JSON yang valid")


def _tabulate(records, mapping):
    if mapping:
        def cell(rec, path):
            v = get_path(rec, path)
            return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
        return [n for n, _ in mapping], [[cell(r, p) for _, p in mapping] for r in records]
    flat = [flatten(r) if isinstance(r, dict) else {"value": r} for r in records]
    cols = list(dict.fromkeys(k for r in flat for k in r))
    return cols, [[r.get(c) for c in cols] for r in flat]


def fetch_api(cfg, render, preview=False):
    """Mengembalikan (kolom, baris). render: fungsi untuk mengganti variabel tanggal. preview: 1 halaman, 20 baris."""
    method, pag = cfg.get("method", "GET"), cfg.get("pagination", "none")
    size, start = int(cfg.get("page_size") or 0), int(cfg.get("start_page") or 1)
    max_pages = 1 if preview else int(cfg.get("max_pages") or 100)
    max_rows = 20 if preview else int(cfg.get("max_rows") or 100000)
    timeout, verify = int(cfg.get("timeout") or 30), cfg.get("verify_ssl", True) is not False
    base_headers = parse_headers(cfg.get("headers"))
    base_headers.setdefault("Accept", "application/json")
    base_headers.setdefault("Accept-Encoding", "identity")
    auth = cfg.get("auth", "none")
    if auth == "bearer":
        base_headers["Authorization"] = "Bearer " + _secret(cfg, "token", "token_enc")
    elif auth == "basic":
        pair = f"{cfg.get('basic_user', '')}:{_secret(cfg, 'password', 'password_enc')}"
        base_headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    elif auth == "apikey" and cfg.get("apikey_name"):
        base_headers[cfg["apikey_name"]] = _secret(cfg, "apikey_value", "apikey_enc")
    has_body = method == "POST" and (cfg.get("body") or "").strip()
    if has_body:
        base_headers.setdefault("Content-Type", "application/json")

    def fill(text, ph):
        for k, v in ph.items():
            text = text.replace("{" + k + "}", str(v))
        return render(text)

    records, page, offset, url, finished = [], start, 0, cfg["url"], False
    for _ in range(max_pages):
        ph = {"page": page, "offset": offset, "limit": size}
        hdrs = {k: fill(v, ph) for k, v in base_headers.items()}
        data = _request(fill(url, ph), method, hdrs, fill(cfg.get("body") or "", ph) if has_body else None, timeout, verify)
        recs = get_path(data, cfg.get("records_path"))
        recs = [] if recs is None else [recs] if isinstance(recs, dict) else recs
        if not isinstance(recs, list):
            raise ValueError("Path data (records_path) tidak menunjuk ke array/objek")
        records += recs
        if len(records) > max_rows:
            if preview:
                records = records[:max_rows]
                break
            raise ValueError(f"Data API melebihi batas {max_rows} baris; naikkan batas atau persempit filter tanggal")
        if pag == "none" or preview:
            finished = True
            break
        if pag == "next_url":
            nxt = get_path(data, cfg.get("next_path"))
            if not nxt:
                finished = True
                break
            url = str(nxt)
            continue
        if not recs or (size and len(recs) < size):
            finished = True
            break
        page += 1
        offset += size
    if not finished and not preview and pag != "none":
        raise ValueError(f"Mencapai batas {max_pages} halaman tetapi data masih ada; naikkan max_pages")
    return _tabulate(records, parse_columns(cfg.get("columns")))
