"""下载 LongMemEval (cleaned) 原始文件到 research/bench/raw/longmemeval/。

- 先试直连（requests，trust_env=False，完全忽略系统代理）。
- 直连失败再退 hf-mirror.com 镜像。
- 边下边算 sha256，写完写进 SHA256SUMS（由调用方汇总）。
- 只读原始文件：已存在且大小/hash 对得上就跳过。

用法：
    python research/bench/fetch_raw.py --files oracle s m
    python research/bench/fetch_raw.py --files s --force
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
RAW = BASE / "raw" / "longmemeval"

# HF 仓库 xiaowu0162/longmemeval-cleaned，commit sha 见 manifest
REPO = "xiaowu0162/longmemeval-cleaned"
REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
FILES = {
    "oracle": ("longmemeval_oracle.json", 15388478),
    "s": ("longmemeval_s_cleaned.json", 277383467),
    "m": ("longmemeval_m_cleaned.json", 2737100077),
    "readme": ("README.md", 626),
}
MIRRORS = [
    ("direct", "https://huggingface.co"),
    ("hf-mirror", "https://hf-mirror.com"),
]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, force: bool = False) -> dict:
    fname, expected_size = FILES[name]
    dest = RAW / fname
    if dest.exists() and not force:
        size = dest.stat().st_size
        if expected_size and size != expected_size:
            print(f"[skip?] {fname} 已存在但大小不符 ({size} != {expected_size})，重下")
        else:
            print(f"[skip] {fname} 已存在 size={size}")
            return {"file": fname, "size": size, "sha256": sha256_file(dest), "route": "cached"}
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    errors = []
    for label, host in MIRRORS:
        url = f"{host}/datasets/{REPO}/resolve/{REVISION}/{fname}"
        session = requests.Session()
        session.trust_env = False  # 不用系统代理
        session.headers["User-Agent"] = "factlayer-research/1.0"
        t0 = time.time()
        try:
            with session.get(url, stream=True, timeout=(15, 120), allow_redirects=True) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                total = int(r.headers.get("Content-Length") or 0) or expected_size
                h = hashlib.sha256()
                done = 0
                last_report = 0.0
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_report > 5:
                            pct = 100.0 * done / total if total else 0
                            rate = done / (now - t0) / 1e6
                            print(f"  {fname} {done/1e6:.1f}/{total/1e6:.1f}MB ({pct:.1f}%) {rate:.2f}MB/s", flush=True)
                            last_report = now
                if expected_size and done != expected_size:
                    raise RuntimeError(f"大小不符 done={done} expected={expected_size}")
            tmp.replace(dest)
            dt = time.time() - t0
            print(f"[ok] {fname} via {label} {done/1e6:.1f}MB in {dt:.0f}s sha256={h.hexdigest()}")
            return {"file": fname, "size": done, "sha256": h.hexdigest(), "route": label}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {exc}")
            print(f"[fail] {label} {fname}: {exc}", flush=True)
            if tmp.exists():
                tmp.unlink()
    raise SystemExit(f"{fname} 下载失败：\n  " + "\n  ".join(errors))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", nargs="+", default=["readme", "oracle"], choices=sorted(FILES))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    results = []
    for name in args.files:
        try:
            results.append(download(name, args.force))
        except SystemExit as exc:
            print(exc, file=sys.stderr)
            return 1
    print("\n== SHA256 ==")
    for r in results:
        print(f"{r['sha256']}  {r['file']}  ({r['size']} bytes, {r['route']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
