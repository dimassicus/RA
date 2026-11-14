"""Install only the offline translation pairs needed by watched countries or leads."""

from __future__ import annotations

import json
import fcntl
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
from zipfile import BadZipFile, ZipFile, is_zipfile


ROOT = Path(__file__).resolve().parent.parent
MODEL_ROOT = ROOT / ".local" / "models"
CACHE = ROOT / ".local" / "cache" / "argos-index.json"
INDEX_URL = "https://raw.githubusercontent.com/argosopentech/argospm-index/main/index.json"
MAX_ARCHIVE_BYTES = 200_000_000
MAX_EXTRACTED_BYTES = 500_000_000
EXCLUDED_LANGUAGES = {"av", "ce"}  # Explicit watchlist-language exclusions; retain saved translations.
_lock = threading.RLock()
_backfill_lock = threading.Lock()
_recent_failures: dict[str, float] = {}


def installed(code: str, target: str = "en") -> bool:
    path = MODEL_ROOT / f"{code}_{target}"
    return (path / "model" / "model.bin").is_file() and (path / "sentencepiece.model").is_file()


def package_index() -> list[dict]:
    """Cache the official Argos package index for one week."""
    if CACHE.exists() and time.time() - CACHE.stat().st_mtime < 7 * 86400:
        return json.loads(CACHE.read_text())
    try:
        request = urllib.request.Request(INDEX_URL, headers={"User-Agent": "RA-Credit-Monitor/0.3"})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Model index exceeded size limit")
        index = json.loads(raw)
        if not isinstance(index, list):
            raise ValueError("Unexpected model index format")
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_bytes(raw)
        return index
    except Exception:
        if CACHE.exists():
            return json.loads(CACHE.read_text())
        raise


def _download(url: str, path: Path) -> None:
    """Resume a trusted model archive after a transient network interruption."""
    for attempt in range(8):
        offset = path.stat().st_size if path.exists() else 0
        if offset > MAX_ARCHIVE_BYTES:
            raise ValueError("Translation model archive exceeded size limit")
        headers = {"User-Agent": "RA-Credit-Monitor/0.3"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=90) as response:
                if offset and response.status != 206:
                    path.unlink()
                    continue
                total = (int(response.headers["Content-Range"].rsplit("/", 1)[1])
                         if response.status == 206 else int(response.headers["Content-Length"]))
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Translation model archive exceeded size limit")
                with path.open("ab" if offset else "wb") as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        if output.tell() > MAX_ARCHIVE_BYTES:
                            raise ValueError("Translation model archive exceeded size limit")
            if path.stat().st_size == total:
                return
        except (OSError, TimeoutError, urllib.error.URLError):
            if attempt == 7:
                raise
        time.sleep(min(attempt + 1, 5))
    raise OSError("Model download remained incomplete after retries")


def ensure_model(code: str, target: str = "en") -> tuple[bool, str]:
    """Install a direct Argos language pair from the official package index."""
    code = code.lower().split("-", 1)[0]
    target = target.lower().split("-", 1)[0]
    if not all(len(c) == 2 and c.isascii() and c.isalpha() for c in (code, target)):
        return False, "unsupported language code"
    if code == target:
        return True, "original English" if target == "en" else "original language"
    pair = f"{code}_{target}"
    if installed(code, target):
        return True, "offline model installed"
    if os.environ.get("RA_ALLOW_MODEL_DOWNLOADS") != "1":
        return False, "offline model installation requires approval (RA_ALLOW_MODEL_DOWNLOADS=1)"
    with _lock:
        if installed(code, target):
            return True, "offline model installed"
        if time.time() - _recent_failures.get(pair, 0) < 3600:
            return False, "model setup retry scheduled"
        try:
            entry = next((item for item in package_index()
                          if item.get("from_code") == code and item.get("to_code") == target), None)
            if not entry:
                _recent_failures[pair] = time.time()
                return False, f"no direct offline model for {pair}"
            url = next((link for link in entry.get("links", [])
                        if urlparse(link).scheme == "https" and urlparse(link).hostname == "argos-net.com"), None)
            if not url:
                raise ValueError("No trusted HTTPS model link in index")
            MODEL_ROOT.mkdir(parents=True, exist_ok=True)
            archive_path = MODEL_ROOT / f".{pair}.download"
            temp_path = MODEL_ROOT / f".{pair}-{os.getpid()}.tmp"
            lock_path = MODEL_ROOT / f".{pair}.lock"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
                if installed(code, target):
                    return True, "offline model installed"
                if not archive_path.exists() or not is_zipfile(archive_path):
                    _download(url, archive_path)
                shutil.rmtree(temp_path, ignore_errors=True)
                with ZipFile(archive_path) as zipped:
                    members = zipped.infolist()
                    if sum(item.file_size for item in members) > MAX_EXTRACTED_BYTES:
                        raise ValueError("Translation model exceeded extracted size limit")
                    prefix = f"translate-{pair}-"
                    for item in members:
                        parts = Path(item.filename).parts
                        if not parts or not (parts[0] == pair or parts[0].startswith(prefix)):
                            continue
                        relative = Path(*parts[1:])
                        if (relative.is_absolute() or ".." in relative.parts or
                            not (str(relative) in ("metadata.json", "sentencepiece.model", "README.md")
                                 or str(relative).startswith("model/"))):
                            continue
                        destination = temp_path / relative
                        if item.is_dir():
                            destination.mkdir(parents=True, exist_ok=True)
                        else:
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            with zipped.open(item) as source, destination.open("wb") as target:
                                shutil.copyfileobj(source, target)
                if not (temp_path / "model" / "model.bin").is_file() or not (temp_path / "sentencepiece.model").is_file():
                    raise ValueError("Translation package lacks model files")
                temp_path.rename(MODEL_ROOT / pair)
            archive_path.unlink(missing_ok=True)
            shutil.rmtree(temp_path, ignore_errors=True)
            return True, "offline model installed"
        except Exception as exc:
            shutil.rmtree(MODEL_ROOT / f".{pair}-{os.getpid()}.tmp", ignore_errors=True)
            if isinstance(exc, BadZipFile):
                (MODEL_ROOT / f".{pair}.download").unlink(missing_ok=True)
            _recent_failures[pair] = time.time()
            return False, f"model setup failed: {type(exc).__name__}"


def country_languages(country: str) -> list[str]:
    """Find several substantial local languages; article detection can add others."""
    import pycountry
    from babel.languages import get_territory_language_info

    try:
        territory = pycountry.countries.lookup(country).alpha_2
    except LookupError:
        territory = {"turkey": "TR", "türkiye": "TR"}.get(country.strip().casefold(), "")
        if not territory:
            return []
    info = get_territory_language_info(territory)
    ranked = sorted(info.items(), key=lambda item: float(item[1].get("population_percent") or 0), reverse=True)
    languages = []
    for code, details in ranked:
        share = float(details.get("population_percent") or 0)
        official = details.get("official_status") in ("official", "de_facto_official", "official_regional")
        if len(code) == 2 and code != "en" and (official or share >= 10):
            languages.append(code)
        if len(languages) >= 5:
            break
    return [code for code in languages if code not in EXCLUDED_LANGUAGES]


def schedule_countries(countries: list[str]) -> None:
    """Backfill eligible headlines after the watchlist changes."""
    if not countries:
        return

    def run_backfill() -> None:
        if _backfill_lock.acquire(blocking=False):
            try:
                from .translation import backfill
                backfill()
            finally:
                _backfill_lock.release()

    threading.Thread(target=run_backfill, daemon=True, name="ra-translation-backfill").start()


def coverage(countries: list[str]) -> list[dict]:
    """List expected watchlist languages and their configured translation route."""
    return [{"country": country, "language": code, "status": "Gemma with Google Translate backup"}
            for country in sorted(set(countries)) for code in country_languages(country)]
