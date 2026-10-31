"""Install the checked Argos/OPUS-MT Portuguese→English model for local RA use."""

from __future__ import annotations

import hashlib
import urllib.request
from pathlib import Path
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / ".local" / "models" / "pt_en"
URL = "https://argos-net.com/v1/translate-pt_en-1_9.argosmodel"
SHA256 = "ae76df6f650895c16f2b582065014fab496755ca846ecb19fae81d51f332a38e"
PREFIX = "translate-pt_en-1_9/"


def main():
    if (TARGET / "model" / "model.bin").exists():
        print(f"Already installed: {TARGET}")
        return
    request = urllib.request.Request(URL, headers={"User-Agent": "RA-Credit-Monitor/0.2"})
    with urllib.request.urlopen(request, timeout=180) as response:
        archive = response.read(80_000_001)
    if len(archive) > 80_000_000 or hashlib.sha256(archive).hexdigest() != SHA256:
        raise ValueError("Translation model checksum mismatch")
    archive_path = TARGET.parent / "pt_en.download"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(archive)
    with ZipFile(archive_path) as zipped:
        for info in zipped.infolist():
            if not info.filename.startswith(PREFIX):
                continue
            relative = info.filename[len(PREFIX):]
            if relative not in ("metadata.json", "sentencepiece.model", "README.md") and not relative.startswith("model/"):
                continue
            if relative.startswith("/") or ".." in Path(relative).parts:
                raise ValueError("Unsafe path in translation model")
            destination = TARGET / relative
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(info) as source, destination.open("wb") as output:
                    while block := source.read(1024 * 1024):
                        output.write(block)
    archive_path.unlink()
    print(f"Installed: {TARGET}")


if __name__ == "__main__":
    main()
