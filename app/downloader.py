"""Obsługa yt-dlp: odczyt dostępnych jakości filmu i kolejka pobrań w tle."""

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", Path(__file__).resolve().parent.parent / "downloads"))
MAX_PARALLEL = int(os.environ.get("MAX_PARALLEL_DOWNLOADS", "2"))
MAX_JOBS_KEPT = 50

YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
MP3_BITRATES = (320, 192, 128)
MEDIA_SUFFIXES = {".mp4", ".mp3", ".m4a"}

# Nazwy postprocesorów yt-dlp (pp_key) -> opis etapu widoczny w interfejsie.
PP_PHASES = {
    "Merger": "Łączenie obrazu i dźwięku",
    "ExtractAudio": "Konwersja dźwięku",
    "Metadata": "Zapisywanie metadanych",
    "EmbedThumbnail": "Dodawanie okładki",
    "MoveFilesAfterDownload": "Zapisywanie pliku",
}

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


class UserError(Exception):
    """Błąd do pokazania użytkownikowi: zły link, niedostępny film itp."""


def normalize_url(raw: str) -> str:
    url = raw.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise UserError("To nie wygląda na link do YouTube.")
    if parsed.path.rstrip("/") == "/playlist":
        raise UserError("Playlisty nie są jeszcze obsługiwane - wklej link do pojedynczego filmu.")
    return url


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        # Kanał lub playlista zwróci tylko listę linków zamiast pobierać wszystko po kolei.
        "extract_flat": "in_playlist",
        "windowsfilenames": True,
        # YouTube wymaga wykonania JavaScriptu; w Dockerze jest Deno, lokalnie wystarczy Node.
        "js_runtimes": {"deno": {}, "node": {}},
    }


# Najczęstsze komunikaty yt-dlp (fragment, małe litery) -> tłumaczenie dla użytkownika.
KNOWN_ERRORS = {
    "private video": "Ten film jest prywatny.",
    "confirm your age": "Film ma ograniczenie wiekowe i wymaga zalogowania - nie da się go pobrać.",
    "not a bot": "YouTube tymczasowo blokuje zapytania z tego adresu IP. Spróbuj ponownie za jakiś czas.",
    "members-only": "Film tylko dla członków kanału.",
    "premieres in": "Premiera filmu jeszcze się nie odbyła.",
    "video unavailable": "Film jest niedostępny (usunięty, prywatny albo zablokowany w tym kraju).",
    "video is unavailable": "Film jest niedostępny (usunięty, prywatny albo zablokowany w tym kraju).",
}


def _clean_error(exc: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    msg = re.sub(r"^ERROR:\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*[\w-]+:\s*", "", msg)
    lowered = msg.lower()
    for fragment, translated in KNOWN_ERRORS.items():
        if fragment in lowered:
            return translated
    return msg or "Nieznany błąd yt-dlp."


def _extract(url: str, opts: dict, download: bool) -> dict:
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=download)
    except DownloadError as exc:
        raise UserError(_clean_error(exc)) from exc
    if info.get("_type") == "playlist" or not info.get("formats"):
        raise UserError("Ten link prowadzi do playlisty lub kanału - wklej link do pojedynczego filmu.")
    if info.get("is_live"):
        raise UserError("Transmisje na żywo nie są obsługiwane.")
    return info


def _size(fmt: dict | None) -> int:
    if not fmt:
        return 0
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def _codec_rank(vcodec: str) -> int:
    # Ta sama kolejność co format_sort "vcodec:h264" przy pobieraniu: H.264 > VP9 > AV1.
    if vcodec.startswith("avc1"):
        return 0
    if vcodec.startswith(("vp9", "vp09")):
        return 1
    return 2


def _video_label(res: int, fps: float | None) -> str:
    label = f"{res}p" + (str(round(fps)) if fps and fps > 30 else "")
    name = {4320: "8K", 2160: "4K", 1440: "2K"}.get(res)
    return f"{label} ({name})" if name else label


def _video_options(formats: list[dict], audio_size: int) -> list[dict]:
    best: dict[int, tuple[tuple, dict]] = {}
    for f in formats:
        vcodec = f.get("vcodec") or "none"
        height = f.get("height")
        if vcodec == "none" or not height:
            continue
        # Krótszy bok, żeby pionowe Shorts (1080x1920) też były "1080p".
        res = min(height, f.get("width") or height)
        # Jak yt-dlp: bezpośrednie https przed HLS (m3u8 nie ma też znanego rozmiaru), potem kodek.
        key = (f.get("protocol") == "https", -_codec_rank(vcodec), f.get("fps") or 0, f.get("tbr") or 0)
        if res not in best or key > best[res][0]:
            best[res] = (key, f)

    options = []
    for res in sorted(best, reverse=True):
        f = best[res][1]
        video_size = _size(f)
        # Format z wbudowanym dźwiękiem (np. 360p "18") yt-dlp pobiera bez doklejania osobnego audio.
        if (f.get("acodec") or "none") == "none":
            video_size += audio_size if video_size else 0
        options.append({
            "value": res,
            "label": _video_label(res, f.get("fps")),
            "size": video_size or None,
        })
    return options


def get_info(raw_url: str) -> dict:
    url = normalize_url(raw_url)
    info = _extract(url, _base_opts(), download=False)
    formats = info["formats"]

    audio = [f for f in formats if (f.get("vcodec") or "none") == "none" and (f.get("acodec") or "none") != "none"]
    # Pobieranie preferuje stereo (format_sort "channels:2"), więc ścieżki 5.1 nie liczą się do szacunków.
    audio = [f for f in audio if (f.get("audio_channels") or 2) <= 2] or audio
    m4a = max((f for f in audio if f.get("ext") == "m4a"), key=lambda f: f.get("abr") or 0, default=None)
    source_kbps = (m4a or {}).get("abr") or max((f.get("abr") or 0 for f in audio), default=0)

    return {
        "url": info.get("webpage_url") or url,
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "video": _video_options(formats, _size(m4a)),
        "audio": {
            "source_kbps": round(source_kbps) or None,
            "m4a_size": _size(m4a) or None,
            "mp3_bitrates": MP3_BITRATES,
        },
    }


def _validate_quality(kind: str, quality: int | None) -> int | None:
    if kind == "mp4":
        if not isinstance(quality, int) or not 0 < quality <= 4320:
            raise UserError("Nieprawidłowa jakość wideo.")
        return quality
    if kind == "mp3":
        if quality not in MP3_BITRATES:
            raise UserError("Nieprawidłowy bitrate MP3.")
        return quality
    if kind == "m4a":
        return None
    raise UserError("Nieznany format - wybierz MP4, MP3 albo M4A.")


@dataclass
class Job:
    id: str
    url: str
    kind: str
    quality: int | None
    title: str
    status: str = "queued"  # queued | downloading | processing | done | error
    phase: str = "W kolejce"
    progress: float = 0.0
    speed: float | None = None
    eta: int | None = None
    filename: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)


def _format_opts(job: Job) -> dict:
    tag = {"mp4": f"{job.quality}p", "mp3": f"{job.quality}k", "m4a": "m4a"}[job.kind]
    opts = {
        "paths": {"home": str(DOWNLOAD_DIR), "temp": str(DOWNLOAD_DIR / ".tmp")},
        "outtmpl": f"%(title).150B [{tag}].%(ext)s",
    }
    metadata = {"key": "FFmpegMetadata", "add_metadata": True}

    if job.kind == "mp4":
        return opts | {
            "format": "bv*+ba/b",
            # Najwyższa rozdzielczość <= wybranej; przy tej samej rozdzielczości
            # H.264 + AAC stereo, bo odtworzy je każdy telefon i QuickTime.
            "format_sort": [f"res:{job.quality}", "vcodec:h264", "channels:2", "acodec:aac"],
            "merge_output_format": "mp4",
            "postprocessors": [metadata],
        }

    extract = {"key": "FFmpegExtractAudio", "preferredcodec": job.kind}
    if job.kind == "mp3":
        extract["preferredquality"] = str(job.quality)
    return opts | {
        # M4A: oryginalna ścieżka AAC bez ponownej kompresji. MP3: najlepsze źródło do konwersji.
        "format": "ba[ext=m4a]/ba/b" if job.kind == "m4a" else "ba/b",
        "format_sort": ["channels:2"],
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"},
            extract,
            metadata,
            {"key": "EmbedThumbnail"},
        ],
    }


class DownloadManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_PARALLEL)

    def submit(self, raw_url: str, kind: str, quality: int | None, title: str) -> dict:
        url = normalize_url(raw_url)
        quality = _validate_quality(kind, quality)
        job = Job(id=uuid.uuid4().hex[:12], url=url, kind=kind, quality=quality, title=title or url)
        with self._lock:
            self._jobs[job.id] = job
            self._prune()
        self._pool.submit(self._run, job)
        return asdict(job)

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)
            return [asdict(j) for j in jobs]

    def _prune(self) -> None:
        finished = sorted((j for j in self._jobs.values() if j.status in ("done", "error")), key=lambda j: j.created)
        for job in finished[: max(0, len(self._jobs) - MAX_JOBS_KEPT)]:
            del self._jobs[job.id]

    def _run(self, job: Job) -> None:
        def on_progress(d: dict) -> None:
            if d["status"] != "downloading":
                return
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            is_video = (d.get("info_dict", {}).get("vcodec") or "none") != "none"
            job.status = "downloading"
            job.phase = "Pobieranie obrazu" if is_video else "Pobieranie dźwięku"
            job.progress = round(d.get("downloaded_bytes", 0) / total * 100, 1) if total else 0
            job.speed = d.get("speed")
            job.eta = d.get("eta")

        def on_postprocess(d: dict) -> None:
            if d["status"] == "started" and d["postprocessor"] in PP_PHASES:
                job.status = "processing"
                job.phase = PP_PHASES[d["postprocessor"]]
                job.progress, job.speed, job.eta = 100, None, None

        opts = _base_opts() | _format_opts(job) | {
            "progress_hooks": [on_progress],
            "postprocessor_hooks": [on_postprocess],
        }
        try:
            info = _extract(job.url, opts, download=True)
            downloads = info.get("requested_downloads") or [{}]
            path = downloads[0].get("filepath")
            if not path or not Path(path).is_file():
                raise UserError("Pobieranie zakończone, ale nie znaleziono pliku wynikowego.")
            job.title = info.get("title") or job.title
            job.filename = Path(path).name
            job.status, job.phase, job.progress = "done", "Gotowe", 100
        except Exception as exc:  # każdy błąd ma trafić do interfejsu, a nie zostawić zadania "w trakcie"
            job.status, job.phase = "error", "Błąd"
            job.error = str(exc) if isinstance(exc, UserError) else f"Nieoczekiwany błąd: {exc}"
        finally:
            job.speed = job.eta = None


def list_files() -> list[dict]:
    files = []
    for path in DOWNLOAD_DIR.iterdir():
        if path.is_file() and path.suffix.lower() in MEDIA_SUFFIXES and not path.name.startswith("."):
            stat = path.stat()
            files.append({"name": path.name, "size": stat.st_size, "modified": stat.st_mtime})
    return sorted(files, key=lambda f: f["modified"], reverse=True)


def resolve_file(name: str) -> Path | None:
    """Ścieżka do pobranego pliku albo None - bez wychodzenia poza katalog pobrań."""
    path = (DOWNLOAD_DIR / name).resolve()
    if path.parent != DOWNLOAD_DIR.resolve() or not path.is_file() or path.suffix.lower() not in MEDIA_SUFFIXES:
        return None
    return path
