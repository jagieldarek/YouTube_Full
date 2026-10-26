# YouTube Full

Lokalna aplikacja do pobierania filmów z YouTube:

- **MP4** – z wyborem rozdzielczości (144p–4K/8K, tylko te, które film faktycznie ma),
- **MP3** – 128 / 192 / 320 kbps, z okładką i tagami,
- **M4A** – oryginalny dźwięk z YouTube bez ponownej kompresji.

Działa w przeglądarce – na komputerze i na telefonie w tej samej sieci. Wszystko dzieje się lokalnie
(yt-dlp + ffmpeg w kontenerze Docker), bez zewnętrznych serwisów.

## Uruchomienie

```bash
docker compose up -d --build
```

- Aplikacja: <http://localhost:8095>, z telefonu: `http://<IP-komputera>:8095`
- Pobrane pliki: `./downloads` (można je też zapisać/usunąć z poziomu strony)
- Inny port: `APP_PORT=8100 docker compose up -d`

## Aktualizacja yt-dlp

YouTube co jakiś czas zmienia zabezpieczenia i starsza wersja yt-dlp przestaje działać.
Aktualna wersja jest widoczna na dole strony. Aktualizacja = przebudowa obrazu:

```bash
docker compose build --pull --no-cache && docker compose up -d
```

## Jak wybierane są formaty

- **MP4** – najwyższa rozdzielczość nie większa niż wybrana; przy tej samej rozdzielczości H.264 + AAC
  stereo (odtworzy je każdy telefon i QuickTime). Powyżej 1080p YouTube ma zwykle tylko VP9/AV1.
- **MP3** – konwersja z najlepszej ścieżki audio (zwykle Opus ~130 kbps). 320 kbps daje większy plik,
  ale nie lepszą jakość niż źródło.
- **M4A** – ścieżka AAC (~128 kbps) skopiowana 1:1.

## Struktura

| Plik | Co robi |
| --- | --- |
| `app/downloader.py` | yt-dlp: lista jakości, kolejka pobrań w tle, postęp, pliki |
| `app/main.py` | API (FastAPI) + serwowanie strony |
| `app/static/index.html` | interfejs (jeden plik, bez zależności) |

API: `POST /api/info`, `POST /api/downloads`, `GET /api/downloads`, `GET /api/files`,
`GET|DELETE /api/files/{nazwa}`, `GET /api/health`.

## Bezpieczeństwo

Aplikacja nie ma logowania – jest przeznaczona **tylko do sieci lokalnej** (ew. Tailscale).
Nie wystawiaj jej do internetu (reverse proxy, tunel). Przyjmuje wyłącznie linki z YouTube.

## Uwaga prawna

Regulamin YouTube zabrania pobierania treści poza oficjalną funkcją offline, chyba że masz zgodę
właściciela. Używaj do własnych filmów, materiałów na licencji Creative Commons i treści,
na które autor wyraził zgodę.
