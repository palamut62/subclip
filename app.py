import glob
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid

from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
JOBS_PATH = os.path.join(DOWNLOAD_DIR, "jobs.json")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs: dict[str, dict] = {}

_MANAGED_KEYS = ["COLAB_URL", "LIPSYNC_URL", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"]


def _ytdlp_cmd() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def _now_ts() -> float:
    return time.time()


def _update_job(job: dict, *, status: str | None = None, stage: str | None = None,
                phase_message: str | None = None, progress_percent: float | None = None,
                error: str | None = None) -> None:
    if status is not None:
        job["status"] = status
    if stage is not None:
        job["stage"] = stage
    if phase_message is not None:
        job["phase_message"] = phase_message
    if progress_percent is not None:
        job["progress_percent"] = max(0.0, min(100.0, float(progress_percent)))
    if error is not None:
        job["error"] = error
    job["updated_at"] = _now_ts()
    _save_jobs()


def _elapsed_seconds(job: dict) -> int:
    started_at = job.get("started_at") or job.get("created_at") or _now_ts()
    return max(0, int(_now_ts() - started_at))


def _save_jobs() -> None:
    try:
        with open(JOBS_PATH, "w", encoding="utf-8") as f:
            json.dump(jobs, f, ensure_ascii=True, indent=2)
    except OSError:
        pass


def _load_jobs() -> None:
    if not os.path.exists(JOBS_PATH):
        return
    try:
        with open(JOBS_PATH, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            jobs.update(loaded)
            for job in jobs.values():
                if job.get("status") in {"downloading", "dubbing"}:
                    job["status"] = "error"
                    job["stage"] = "error"
                    job["error"] = "Uygulama yeniden baslatildi, islem yarim kaldi"
    except (OSError, json.JSONDecodeError):
        pass


def _read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env


def _write_env(data: dict[str, str]) -> None:
    lines: list[str] = []
    existing_keys: set[str] = set()

    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.partition("=")[0].strip()
                    if k in data:
                        if data[k]:
                            lines.append(f"{k}={data[k]}\n")
                        existing_keys.add(k)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")

    for k, v in data.items():
        if k not in existing_keys and v:
            lines.append(f"{k}={v}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _apply_env(data: dict[str, str]) -> None:
    for k, v in data.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]


def _parse_ytdlp_progress(line: str) -> tuple[float | None, str | None]:
    lowered = line.lower()
    percent_match = re.search(r"(\d+(?:\.\d+)?)%", line)
    percent = float(percent_match.group(1)) if percent_match else None

    if "[download]" in lowered:
        if "fragment" in lowered or "frag" in lowered:
            return percent, "Video parcalari indiriliyor"
        return percent, "Video indiriliyor"
    if "merging formats" in lowered:
        return 96.0, "Video ve ses birlestiriliyor"
    if "[extractaudio]" in lowered:
        return 97.0, "Ses cikartiliyor"
    if "[fixup" in lowered:
        return 98.0, "Dosya duzenleniyor"
    if "destination:" in lowered:
        return 3.0, "Indirme basladi"
    return percent, None


def _mask(v: str) -> str:
    return ("*" * (len(v) - 4) + v[-4:]) if len(v) > 4 else ("*" * len(v))


def _run_download_process(job: dict, cmd: list[str]) -> list[str]:
    _update_job(job, status="downloading", stage="download", phase_message="Indirme hazirlaniyor", progress_percent=0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        errors="replace",
    )
    progress_lines: list[str] = []
    assert proc.stdout is not None
    for raw_line in proc.stdout:
        line = raw_line.strip()
        if not line:
            continue
        progress_lines.append(line)
        progress_lines = progress_lines[-30:]
        percent, message = _parse_ytdlp_progress(line)
        if percent is not None or message:
            _update_job(
                job,
                status="downloading",
                stage="download",
                phase_message=message or job.get("phase_message") or "Video indiriliyor",
                progress_percent=percent if percent is not None else job.get("progress_percent", 0),
            )
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(progress_lines[-1] if progress_lines else "Download failed")
    return progress_lines


def _find_output_file(job_id: str, format_choice: str) -> str:
    files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
    if not files:
        raise RuntimeError("Download completed but no file was found")
    if format_choice == "audio":
        candidates = [f for f in files if f.endswith(".mp3")]
    else:
        candidates = [f for f in files if f.endswith(".mp4")]
    chosen = candidates[0] if candidates else files[0]
    for path in files:
        if path != chosen:
            try:
                os.remove(path)
            except OSError:
                pass
    return chosen


def _finalize_filename(job: dict, chosen: str, suffix: str = "") -> None:
    ext = os.path.splitext(chosen)[1]
    title = job.get("title", "").strip()
    if title:
        safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
        stem = safe_title if safe_title else os.path.splitext(os.path.basename(chosen))[0]
        job["filename"] = f"{stem}{suffix}{ext}"
    else:
        job["filename"] = os.path.basename(chosen)
    _save_jobs()


def run_download(job_id: str, url: str, format_choice: str, format_id: str | None,
                 dub: bool = False, dub_engine: str = "fast", src_lang: str = "en",
                 tgt_lang: str = "tr") -> None:
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = _ytdlp_cmd() + ["--no-playlist", "--newline", "--concurrent-fragments", "8", "-o", out_template]
    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]
    cmd.append(url)

    try:
        _run_download_process(job, cmd)
        chosen = _find_output_file(job_id, format_choice)

        if dub and format_choice != "audio" and chosen.endswith(".mp4"):
            try:
                _update_job(job, status="dubbing", stage="dub_prepare", phase_message="Dublaj baslatiliyor", progress_percent=4)
                dubbed_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_tr.mp4")
                if dub_engine.startswith("krillin"):
                    from krillin_client import dub_video as krillin_dub

                    llm = "ollama" if dub_engine == "krillin_ollama" else "deepseek"
                    krillin_dub(
                        chosen,
                        dubbed_path,
                        origin_lang=src_lang,
                        target_lang=tgt_lang,
                        llm=llm,
                        on_progress=lambda p: _update_job(
                            job,
                            status="dubbing",
                            stage="dub_krillin",
                            phase_message="KrillinAI dublaj yapiyor",
                            progress_percent=p,
                        ),
                    )
                else:
                    from dub import dub_video

                    dub_video(
                        chosen,
                        dubbed_path,
                        src_lang=src_lang,
                        tgt_lang=tgt_lang,
                        on_progress=lambda p, msg: _update_job(
                            job,
                            status="dubbing",
                            stage="dub_fast",
                            phase_message=msg,
                            progress_percent=p,
                        ),
                    )
                try:
                    os.remove(chosen)
                except OSError:
                    pass
                chosen = dubbed_path
            except Exception as e:
                _update_job(job, status="error", stage="error", error=f"Dublaj hatasi: {e}")
                return

        final_stage = "dub_done" if dub and format_choice != "audio" else "download_done"
        _update_job(job, status="done", stage=final_stage, phase_message="Tamamlandi", progress_percent=100)
        job["file"] = chosen
        _finalize_filename(job, chosen)
    except Exception as e:
        _update_job(job, status="error", stage="error", error=str(e))


def run_dub_existing(job_id: str, dub_engine: str = "fast", src_lang: str = "en", tgt_lang: str = "tr") -> None:
    job = jobs[job_id]
    src_path = job.get("file", "")
    if not src_path or not os.path.exists(src_path):
        _update_job(job, status="error", stage="error", error="Kaynak dosya bulunamadi")
        return

    try:
        _update_job(job, status="dubbing", stage="dub_prepare", phase_message="Dublaj hazirlaniyor", progress_percent=3)
        working_src = src_path
        if not src_path.lower().endswith(".mp4"):
            converted = os.path.join(DOWNLOAD_DIR, f"{job_id}_source.mp4")
            _update_job(job, status="dubbing", stage="dub_prepare", phase_message="Video uyumlu formata cevriliyor", progress_percent=8)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", src_path,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "192k",
                    converted,
                ],
                check=True,
                capture_output=True,
            )
            working_src = converted

        base_name = os.path.splitext(os.path.basename(src_path))[0]
        dubbed_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_dub_{tgt_lang}.mp4")

        if dub_engine.startswith("krillin"):
            from krillin_client import dub_video as krillin_dub

            llm = "ollama" if dub_engine == "krillin_ollama" else "deepseek"
            krillin_dub(
                working_src,
                dubbed_path,
                origin_lang=src_lang,
                target_lang=tgt_lang,
                llm=llm,
                on_progress=lambda p: _update_job(
                    job,
                    status="dubbing",
                    stage="dub_krillin",
                    phase_message="KrillinAI dublaj yapiyor",
                    progress_percent=p,
                ),
            )
        else:
            from dub import dub_video

            dub_video(
                working_src,
                dubbed_path,
                src_lang=src_lang,
                tgt_lang=tgt_lang,
                on_progress=lambda p, msg: _update_job(
                    job,
                    status="dubbing",
                    stage="dub_fast",
                    phase_message=msg,
                    progress_percent=p,
                ),
            )

        _update_job(job, status="done", stage="dub_done", phase_message="Dublaj tamamlandi", progress_percent=100)
        job["file"] = dubbed_path
        _finalize_filename(job, dubbed_path, suffix=f"_dub_{tgt_lang}")
    except Exception as e:
        _update_job(job, status="error", stage="error", error=f"Dublaj hatasi: {e}")


_apply_env(_read_env())
_load_jobs()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    env = _read_env()
    result = {}
    for k in _MANAGED_KEYS:
        v = env.get(k, os.environ.get(k, ""))
        shown = v if k in ("COLAB_URL", "LIPSYNC_URL") else (_mask(v) if v else "")
        result[k] = {"masked": shown, "set": bool(v)}
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    to_save = {}
    for k in _MANAGED_KEYS:
        v = data.get(k, "").strip()
        if v and "*" not in v:
            to_save[k] = v
        elif not v:
            to_save[k] = ""
    _write_env(to_save)
    _apply_env(to_save)
    return jsonify({"ok": True})


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = _ytdlp_cmd() + ["--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout.strip().splitlines()[0])
        best_by_height = {}
        for fmt in info.get("formats", []):
            height = fmt.get("height")
            if height and fmt.get("vcodec", "none") != "none":
                tbr = fmt.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = fmt

        formats = []
        for height, fmt in best_by_height.items():
            formats.append({"id": fmt["format_id"], "label": f"{height}p", "height": height})
        formats.sort(key=lambda item: item["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")
    dub = bool(data.get("dub"))
    dub_engine = data.get("dub_engine", "fast")
    src_lang = data.get("src_lang", "en")
    tgt_lang = data.get("tgt_lang", "tr")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    now = _now_ts()
    jobs[job_id] = {
        "status": "downloading",
        "stage": "queued",
        "phase_message": "Kuyruga alindi",
        "progress_percent": 0,
        "created_at": now,
        "started_at": now,
        "updated_at": now,
        "url": url,
        "title": title,
        "format": format_choice,
    }
    _save_jobs()

    thread = threading.Thread(
        target=run_download,
        args=(job_id, url, format_choice, format_id, dub, dub_engine, src_lang, tgt_lang),
    )
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/dub/<job_id>", methods=["POST"])
def dub_existing_file(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "Job tamamlanmadi"}), 400
    src_file = job.get("file", "")
    if not src_file or not os.path.exists(src_file):
        return jsonify({"error": "Kaynak dosya bulunamadi"}), 400

    data = request.json or {}
    dub_engine = data.get("dub_engine", "fast")
    src_lang = data.get("src_lang", "en")
    tgt_lang = data.get("tgt_lang", "tr")

    now = _now_ts()
    job["started_at"] = now
    job["updated_at"] = now
    _save_jobs()
    thread = threading.Thread(target=run_dub_existing, args=(job_id, dub_engine, src_lang, tgt_lang))
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "stage": job.get("stage"),
        "phase_message": job.get("phase_message"),
        "progress_percent": job.get("progress_percent", 0),
        "error": job.get("error"),
        "filename": job.get("filename"),
        "format": job.get("format"),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "elapsed_sec": _elapsed_seconds(job),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/media/<job_id>")
def stream_media(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    mime, _ = mimetypes.guess_type(job["file"])
    return send_file(job["file"], as_attachment=False, mimetype=mime or "application/octet-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
