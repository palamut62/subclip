# ReClip

ReClip is a self-hosted web app for downloading media from popular platforms and optionally adding AI dubbing to downloaded videos.

It is designed to be simple to run on Windows, Linux, or Docker, while still supporting advanced dubbing workflows (fast local path and KrillinAI-based path).

## Highlights

- Download from 1000+ websites supported by [`yt-dlp`](https://github.com/yt-dlp/yt-dlp)
- Export as `MP4` (video) or `MP3` (audio)
- Choose quality/resolution per video
- Queue multiple URLs in one run
- Add dubbing either:
  - during download, or
  - later on an already downloaded file from the UI (`Dublaj Ekle`)
- Multiple dubbing backends:
  - Fast pipeline (`edge-tts` based flow)
  - `KrillinAI + DeepSeek`
  - `KrillinAI + Ollama`
- Runtime settings panel for API keys (`.env`-backed)
- Minimal stack: Flask backend + single-page vanilla HTML/CSS/JS frontend

## Supported Sources

ReClip supports any source that `yt-dlp` supports, including:

- YouTube
- TikTok
- Instagram
- X/Twitter
- Reddit
- Facebook
- Vimeo
- Twitch
- Dailymotion
- SoundCloud
- and many more

Full list: [`yt-dlp` supported sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)

## Project Structure

```text
reclip/
  app.py                 # Flask API + job orchestration
  dub.py                 # Fast dubbing pipeline
  krillin_client.py      # KrillinAI integration (upload/start/poll/download)
  templates/index.html   # Single-page UI
  static/                # Icons/assets
  downloads/             # Generated output files
  .env.example           # Example runtime config
  Dockerfile
```

## Requirements

### System

- Python 3.10+ (recommended: 3.12)
- `ffmpeg` available in PATH
- `yt-dlp` (installed from pip)

### Optional (for advanced dubbing)

- Groq API key (optional; transcription acceleration depending on pipeline)
- OpenRouter API key/model (for KrillinAI DeepSeek setup)
- Local Ollama server (for KrillinAI Ollama setup)
- KrillinAI installed locally (for Krillin modes)

## Installation (Windows)

```powershell
git clone <YOUR_REPO_URL>
cd reclip
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Install ffmpeg if missing (example with winget):

```powershell
winget install --id Gyan.FFmpeg -e
```

Run:

```powershell
python app.py
```

Open: `http://127.0.0.1:8899`

## Installation (Docker)

```bash
docker build -t reclip .
docker run --rm -p 8899:8899 reclip
```

## Runtime Configuration

Settings are managed in the UI and persisted into `.env`.

Managed keys:

- `COLAB_URL`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`

You can also edit `.env` manually if needed.

## How to Use

1. Paste one or more URLs.
2. Select `MP4` or `MP3`.
3. Click `Fetch`.
4. For video cards, select resolution if available.
5. Click `Download`.
6. After completion:
   - click `Save` to download the file to your computer
   - click `Dublaj Ekle` (MP4 only) to start dubbing on that existing file

## Dubbing Modes

### Fast Mode

- Best default for speed and lower setup complexity.
- Triggered when `dub_engine=fast`.

### KrillinAI Modes

- `krillin_deepseek`: uses KrillinAI + OpenRouter/DeepSeek style setup
- `krillin_ollama`: uses KrillinAI + local Ollama

Krillin flow (high level):

1. Upload media to Krillin (`POST /api/file`)
2. Start subtitle/dubbing task (`POST /api/capability/subtitleTask`)
3. Poll task status (`GET /api/capability/subtitleTask?taskId=...`)
4. Download rendered output

`krillin_client.py` includes improved nested error extraction so internal failure reasons are surfaced more clearly.

## API Overview

### `POST /api/info`

Returns media metadata (title, thumbnail, duration, available formats).

### `POST /api/download`

Starts download (and optional immediate dubbing).

### `GET /api/status/<job_id>`

Returns job state (`downloading`, `dubbing`, `done`, `error`) and filename/error.

### `GET /api/file/<job_id>`

Streams completed file as attachment.

### `POST /api/dub/<job_id>`

Starts dubbing for an already completed MP4 job.

## Troubleshooting

### `ffmpeg` not found

Install ffmpeg and ensure it is in PATH.

### Krillin task fails with generic message

Check the latest surfaced error in UI (now includes nested details) and inspect Krillin logs under your Krillin installation directory.

### Download succeeds but no file appears

Check write permissions for `downloads/` and ensure antivirus is not quarantining output files.

### Slow transcription/dubbing

- Use shorter clips to validate pipeline first.
- Prefer fast mode for iteration.
- Validate GPU/driver availability for external tools where applicable.

## Security Notes

- Do not commit real API keys.
- Keep secrets in `.env`.
- Rotate keys if leaked.

## Legal Notice

Use this project responsibly and comply with copyright law and platform terms of service. The maintainers are not responsible for misuse.

## License

MIT - see [LICENSE](LICENSE)
