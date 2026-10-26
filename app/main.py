"""YouTube Full - lokalny serwer: API do pobierania + strona w przeglądarce."""

from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .downloader import DownloadManager, UserError, get_info, list_files, resolve_file

app = FastAPI(title="YouTube Full")
manager = DownloadManager()


class InfoRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    kind: str
    quality: int | None = None
    title: str = ""


@app.exception_handler(UserError)
def user_error(_: Request, exc: UserError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "yt_dlp": yt_dlp.version.__version__}


# Zwykłe "def" (nie async): yt-dlp blokuje na kilka sekund, FastAPI uruchomi to w osobnym wątku.
@app.post("/api/info")
def info(req: InfoRequest) -> dict:
    return get_info(req.url)


@app.post("/api/downloads", status_code=202)
def start_download(req: DownloadRequest) -> dict:
    return manager.submit(req.url, req.kind, req.quality, req.title)


@app.get("/api/downloads")
def downloads() -> list[dict]:
    return manager.list()


@app.get("/api/files")
def files() -> list[dict]:
    return list_files()


@app.get("/api/files/{name}")
def get_file(name: str) -> FileResponse:
    path = resolve_file(name)
    if not path:
        raise HTTPException(404, "Nie ma takiego pliku.")
    return FileResponse(path, filename=path.name)


@app.delete("/api/files/{name}", status_code=204)
def delete_file(name: str) -> None:
    path = resolve_file(name)
    if not path:
        raise HTTPException(404, "Nie ma takiego pliku.")
    path.unlink()


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
