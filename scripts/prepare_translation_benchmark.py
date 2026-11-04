"""Download pinned public model files, verify hashes, and optionally convert to INT8.

Run with the isolated benchmark Python; never imports or changes the app.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / ".local" / "translation-benchmark"
MODELS = {
    "nllb": ("facebook/nllb-200-distilled-1.3B", "7be3e24664b38ce1cac29b8aeed6911aa0cf0576"),
    "madlad": ("google/madlad400-3b-mt", "fa184c675da0b5c9e1c8694fccd4e12e2d422094"),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ranged_download(url, partial, size):
    """Resume an existing prefix and fetch four bounded ranges concurrently."""
    directory = partial.with_suffix(partial.suffix + ".parts")
    directory.mkdir(exist_ok=True)
    plan_path = directory / "plan.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        assert plan["size"] == size and plan["url"] == url
    else:
        prefix = partial.stat().st_size if partial.exists() else 0
        if prefix > size:
            raise ValueError("Oversized prefix")
        plan = {"url": url, "size": size, "prefix": prefix,
                "ranges": [[start, min(start + 512 * 1024 * 1024, size) - 1] for start in range(prefix, size, 512 * 1024 * 1024)]}
        plan_path.write_text(json.dumps(plan))
    assert (partial.stat().st_size if partial.exists() else 0) == plan["prefix"]

    def fetch(bounds):
        start, end = bounds
        part = directory / f"{start}-{end}"
        for attempt in range(8):
            have = part.stat().st_size if part.exists() else 0
            if have == end - start + 1:
                return part
            if have > end - start + 1:
                raise ValueError("Oversized range")
            request = urllib.request.Request(url, headers={"Range": f"bytes={start + have}-{end}", "Accept-Encoding": "identity"})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    expected_range = f"bytes {start + have}-{end}/{size}"
                    if response.status != 206 or response.headers.get("Content-Range") != expected_range:
                        raise ValueError("Server did not honor exact byte range")
                    with part.open("ab") as out:
                        interval_start, interval_bytes = time.monotonic(), 0
                        while block := response.read1(1024 * 1024):
                            out.write(block)
                            interval_bytes += len(block)
                            if out.tell() > end - start + 1:
                                raise ValueError("Range exceeded expected length")
                            elapsed = time.monotonic() - interval_start
                            if elapsed >= 20:
                                if interval_bytes / elapsed < 256 * 1024:
                                    raise TimeoutError("Stalled range; resume on a fresh connection")
                                interval_start, interval_bytes = time.monotonic(), 0
                if part.stat().st_size == end - start + 1:
                    print(f"Range completed: {partial.parent.parent.name} {start:,}-{end:,}", flush=True)
                    return part
            except (OSError, TimeoutError) as exc:
                print(f"Range retry {start}: {type(exc).__name__}", flush=True)
                if attempt == 7:
                    raise
                time.sleep(min(2 ** attempt, 30))
        raise ValueError("Range incomplete")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        parts = list(pool.map(fetch, plan["ranges"]))
    assembled = partial.with_suffix(partial.suffix + ".assembled")
    with assembled.open("wb") as out:
        for path in ([partial] if plan["prefix"] else []) + parts:
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, out, 8 * 1024 * 1024)
    assert assembled.stat().st_size == size
    return assembled, directory


def download(model):
    repo, revision = MODELS[model]
    folder = ROOT / model / "original"
    folder.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", timeout=60) as response:
        manifest = json.load(response)
    assert manifest["sha"] == revision
    (folder.parent / "manifest.json").write_text(json.dumps(manifest, indent=2))
    records = []
    for entry in manifest["siblings"]:
        name = entry["rfilename"]
        if not name.endswith((".json", ".model", ".bin", ".safetensors", ".md")):
            continue
        assert Path(name).name == name
        path = folder / name
        expected = entry.get("lfs", {}).get("sha256")
        size = entry["size"]
        if not path.exists() or path.stat().st_size != size or (expected and sha256(path) != expected):
            partial = path.with_suffix(path.suffix + ".partial")
            if size > 1024 * 1024 * 1024:
                url = f"https://huggingface.co/{repo}/resolve/{revision}/{name}"
                assembled, directory = ranged_download(url, partial, size)
                if not expected or sha256(assembled) != expected:
                    raise ValueError(f"SHA256 mismatch: {name}")
                assembled.replace(path)
                partial.unlink(missing_ok=True)
                shutil.rmtree(directory)
                records.append({"file": name, "size": size, "sha256": expected, "lfs_verified": True})
                print(f"{model}: verified {name} ({size:,} bytes)", flush=True)
                continue
            for attempt in range(8):
                offset = partial.stat().st_size if partial.exists() else 0
                if offset > size:
                    raise ValueError(f"Oversized partial download: {name}")
                if offset == size:
                    break
                headers = {"User-Agent": "RA-local-translation-benchmark", "Accept-Encoding": "identity"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(f"https://huggingface.co/{repo}/resolve/{revision}/{name}", headers=headers)
                try:
                    with urllib.request.urlopen(request, timeout=90) as response:
                        if offset and response.status != 206:
                            offset = 0
                        if response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                            raise ValueError("Unexpected range response")
                        last = time.monotonic()
                        with partial.open("ab" if offset else "wb") as out:
                            while block := response.read(4 * 1024 * 1024):
                                out.write(block)
                                if out.tell() > size:
                                    raise ValueError("Download exceeds pinned manifest size")
                                if time.monotonic() - last >= 30:
                                    print(f"{model}: {name} {out.tell()/1e9:.2f}/{size/1e9:.2f} GB", flush=True)
                                    last = time.monotonic()
                    if partial.stat().st_size == size:
                        break
                except (OSError, TimeoutError) as exc:
                    print(f"{model}: retry {attempt + 1}: {type(exc).__name__}", flush=True)
                    if attempt == 7:
                        raise
                    time.sleep(min(2 ** attempt, 30))
            if partial.stat().st_size != size:
                raise ValueError(f"Incomplete download: {name}")
            if expected and sha256(partial) != expected:
                raise ValueError(f"SHA256 mismatch: {name}")
            partial.replace(path)
        actual = sha256(path)
        records.append({"file": name, "size": size, "sha256": actual, "lfs_verified": bool(expected)})
        print(f"{model}: verified {name} ({size:,} bytes)", flush=True)
    (folder.parent / "download-verification.json").write_text(json.dumps({"repo": repo, "revision": revision, "files": records}, indent=2))


def convert(model):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import ctranslate2
    import torch
    torch.set_num_threads(4)
    folder = ROOT / model
    if not (folder / "download-verification.json").exists():
        raise ValueError("Complete and verify download before conversion")
    start = time.monotonic()
    converter = ctranslate2.converters.TransformersConverter(str(folder / "original"), load_as_float16=False, trust_remote_code=False)
    converter.convert(str(folder / "int8"), quantization="int8")
    record = {"quantization": "int8", "ctranslate2": ctranslate2.__version__, "seconds": time.monotonic() - start,
              "files": [{"file": p.name, "bytes": p.stat().st_size, "sha256": sha256(p)} for p in (folder / "int8").iterdir() if p.is_file()]}
    (folder / "conversion.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({"model": model, "conversion_seconds": record["seconds"], "bytes": sum(f["bytes"] for f in record["files"])}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "convert"])
    parser.add_argument("models", nargs="+", choices=list(MODELS))
    args = parser.parse_args()
    ROOT.mkdir(parents=True, exist_ok=True)
    if args.action == "download":
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(download, args.models))
    else:
        for model in args.models:
            convert(model)
