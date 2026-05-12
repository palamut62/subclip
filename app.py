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
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

_MANAGED_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
]


def _normalize_lang(value: str | None, fallback: str) -> str:
    raw = (value or "").strip().lower().replace("_", "-")
    if not raw:
        return fallback
    if raw == "auto":
        return "auto"
    if len(raw) == 2 and raw.isalpha():
        return raw
    if "-" in raw:
        head = raw.split("-", 1)[0]
        if len(head) == 2 and head.isalpha():
            return head
    return fallback


def _ytdlp_cmd() -> list[str]:
    return [sys.executable, "-m", "yt_dlp"]


def _subprocess_kwargs() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


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
                if job.get("status") in {"downloading", "subtitling"}:
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
    _update_job(job, status="downloading", stage="download", phase_message="Preparing download", progress_percent=0)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
        errors="replace",
        **_subprocess_kwargs(),
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
                 src_lang: str = "auto",
                 subtitle: bool = False, subtitle_mode: str = "sidecar",
                 subtitle_tgt_lang: str = "tr", subtitle_font_size: int = 22,
                 subtitle_font_family: str = "Arial",
                 subtitle_color: str = "#FFFFFF",
                 subtitle_outline_color: str = "#000000",
                 subtitle_bg_color: str = "#000000",
                 subtitle_bg_opacity: int = 0) -> None:
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

        # Indirme bittikten sonra altyazi istendiyse uygula
        if subtitle and format_choice != "audio" and chosen.endswith(".mp4"):
            try:
                from subtitle import generate_subtitles

                _update_job(job, status="subtitling", stage="sub_prepare",
                            phase_message="Preparing subtitles", progress_percent=3)
                base_name = os.path.splitext(os.path.basename(chosen))[0]
                lang_tag = subtitle_tgt_lang
                srt_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_{lang_tag}.srt")
                burned_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_sub_{lang_tag}.mp4") \
                    if subtitle_mode in ("burn", "both") else None

                sub_translate = src_lang != subtitle_tgt_lang and src_lang != "auto"
                generate_subtitles(
                    chosen, srt_path,
                    burned_out=burned_path,
                    src_lang=src_lang, tgt_lang=subtitle_tgt_lang,
                    translate=sub_translate, font_size=subtitle_font_size,
                    font_name=subtitle_font_family,
                    text_color=subtitle_color,
                    outline_color=subtitle_outline_color,
                    bg_color=subtitle_bg_color,
                    bg_opacity=subtitle_bg_opacity,
                    on_progress=lambda p, msg: _update_job(
                        job, status="subtitling", stage="sub_run",
                        phase_message=msg, progress_percent=p,
                    ),
                )
                job["subtitle_file"] = srt_path
                if burned_path and os.path.exists(burned_path):
                    try:
                        os.remove(chosen)
                    except OSError:
                        pass
                    chosen = burned_path
            except Exception as e:
                _update_job(job, status="error", stage="error", error=f"Subtitle error: {e}")
                return

        final_stage = "sub_done" if subtitle and format_choice != "audio" else "download_done"
        _update_job(job, status="done", stage=final_stage, phase_message="Tamamlandi", progress_percent=100)
        job["file"] = chosen
        _finalize_filename(job, chosen)
    except Exception as e:
        _update_job(job, status="error", stage="error", error=str(e))


def run_subtitle(job_id: str, mode: str = "sidecar", src_lang: str = "auto",
                 tgt_lang: str = "tr", translate: bool = True, font_size: int = 22,
                 font_name: str = "Arial", text_color: str = "#FFFFFF",
                 outline_color: str = "#000000", bg_color: str = "#000000",
                 bg_opacity: int = 0, reuse_existing_srt: bool = False) -> None:
    """mode: 'sidecar' (sadece SRT), 'burn' (videoya goM), 'both' (ikisi de)."""
    job = jobs[job_id]
    src_path = job.get("file", "")
    if not src_path or not os.path.exists(src_path):
        _update_job(job, status="error", stage="error", error="Source file not found")
        return
    if not src_path.lower().endswith(".mp4"):
        _update_job(job, status="error", stage="error", error="Altyazi sadece video icin destekleniyor")
        return

    try:
        from subtitle import burn_subtitles_into_video, generate_subtitles

        _update_job(job, status="subtitling", stage="sub_prepare",
                    phase_message="Preparing subtitles", progress_percent=3)

        if reuse_existing_srt:
            srt_path = job.get("subtitle_file", "")
            if not srt_path or not os.path.exists(srt_path):
                _update_job(job, status="error", stage="error", error="Mevcut SRT bulunamadi")
                return

            if mode in ("burn", "both"):
                base_name = os.path.splitext(os.path.basename(src_path))[0]
                burned_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_sub_styled.mp4")
                _update_job(job, status="subtitling", stage="sub_run",
                            phase_message="Applying subtitle style", progress_percent=70)
                burn_subtitles_into_video(
                    src_path,
                    srt_path,
                    burned_path,
                    font_size=font_size,
                    font_name=font_name,
                    text_color=text_color,
                    outline_color=outline_color,
                    bg_color=bg_color,
                    bg_opacity=bg_opacity,
                )
                job["file"] = burned_path
                _finalize_filename(job, burned_path, suffix="_sub_styled")

            _update_job(job, status="done", stage="sub_done",
                        phase_message="Subtitles complete", progress_percent=100)
            return

        base_name = os.path.splitext(os.path.basename(src_path))[0]
        lang_tag = tgt_lang if translate else (src_lang if src_lang != "auto" else "src")
        srt_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_{lang_tag}.srt")
        burned_path = None
        if mode in ("burn", "both"):
            burned_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_sub_{lang_tag}.mp4")

        generate_subtitles(
            src_path,
            srt_path,
            burned_out=burned_path,
            src_lang=src_lang,
            tgt_lang=tgt_lang,
            translate=translate,
            font_size=font_size,
            font_name=font_name,
            text_color=text_color,
            outline_color=outline_color,
            bg_color=bg_color,
            bg_opacity=bg_opacity,
            on_progress=lambda p, msg: _update_job(
                job, status="subtitling", stage="sub_run",
                phase_message=msg, progress_percent=p,
            ),
        )

        job["subtitle_file"] = srt_path
        if burned_path and os.path.exists(burned_path):
            # Gomulu videoyu yeni "ana dosya" yap (kullanici Save'de bunu indirir)
            job["file"] = burned_path
            _finalize_filename(job, burned_path, suffix=f"_sub_{lang_tag}")

        _update_job(job, status="done", stage="sub_done",
                    phase_message="Subtitles complete", progress_percent=100)
    except Exception as e:
        _update_job(job, status="error", stage="error", error=f"Subtitle error: {e}")


_apply_env(_read_env())
_load_jobs()


@app.route("/")
def index():
    from flask import make_response
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/api/settings", methods=["GET"])
def get_settings():
    env = _read_env()
    result = {}
    for k in _MANAGED_KEYS:
        v = env.get(k, os.environ.get(k, ""))
        shown = v if k == "OPENROUTER_MODEL" else (_mask(v) if v else "")
        result[k] = {"masked": shown, "set": bool(v)}
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    to_save = {}
    clearable_blank_keys = {
        "OPENROUTER_MODEL",
    }
    for k in _MANAGED_KEYS:
        if k not in data:
            continue
        v = data.get(k, "").strip()
        if "*" in v:
            continue
        if not v and k not in clearable_blank_keys:
            continue
        to_save[k] = v
    if to_save:
        _write_env(to_save)
        _apply_env(to_save)
    return jsonify({"ok": True})


@app.route("/api/settings/<key>", methods=["DELETE"])
def clear_setting(key):
    if key not in _MANAGED_KEYS:
        return jsonify({"error": "unknown key"}), 400
    _write_env({key: ""})
    if key in os.environ:
        del os.environ[key]
    return jsonify({"ok": True})


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = _ytdlp_cmd() + ["--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, **_subprocess_kwargs())
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
    src_lang = _normalize_lang(data.get("src_lang", "en"), "auto")
    subtitle = bool(data.get("subtitle"))
    subtitle_mode = data.get("subtitle_mode", "sidecar")
    subtitle_tgt_lang = data.get("subtitle_tgt_lang", "tr")
    subtitle_font_size = int(data.get("subtitle_font_size", 22))
    subtitle_font_family = str(data.get("subtitle_font_family", "Arial"))
    subtitle_color = str(data.get("subtitle_color", "#FFFFFF"))
    subtitle_outline_color = str(data.get("subtitle_outline_color", "#000000"))
    subtitle_bg_color = str(data.get("subtitle_bg_color", "#000000"))
    subtitle_bg_opacity = int(data.get("subtitle_bg_opacity", 0))

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
        args=(job_id, url, format_choice, format_id, src_lang,
              subtitle, subtitle_mode, subtitle_tgt_lang, subtitle_font_size,
              subtitle_font_family, subtitle_color, subtitle_outline_color,
              subtitle_bg_color, subtitle_bg_opacity),
    )
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/subtitle/<job_id>", methods=["POST"])
def subtitle_existing(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    src_file = job.get("file", "")
    # Disk'te dosya varsa job state ne olursa olsun devam et
    if not src_file or not os.path.exists(src_file):
        candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}*.mp4"))
        if candidates:
            src_file = max(candidates, key=os.path.getmtime)
            job["file"] = src_file
        else:
            return jsonify({"error": "Source file not found"}), 400
    if not src_file.lower().endswith(".mp4"):
        return jsonify({"error": "Altyazi sadece MP4 icin destekleniyor"}), 400

    data = request.json or {}
    mode = data.get("mode", "sidecar")
    if mode not in ("sidecar", "burn", "both"):
        return jsonify({"error": "Gecersiz mode"}), 400
    src_lang = _normalize_lang(data.get("src_lang", "auto"), "auto")
    tgt_lang = _normalize_lang(data.get("tgt_lang", "tr"), "tr")
    translate = bool(data.get("translate", True))
    font_size = int(data.get("font_size", 22))
    font_name = str(data.get("font_name", "Arial"))
    text_color = str(data.get("text_color", "#FFFFFF"))
    outline_color = str(data.get("outline_color", "#000000"))
    bg_color = str(data.get("bg_color", "#000000"))
    bg_opacity = int(data.get("bg_opacity", 0))
    restyle_only = bool(data.get("restyle_only", False))

    now = _now_ts()
    job.pop("error", None)
    job["started_at"] = now
    job["updated_at"] = now
    job["status"] = "subtitling"
    job["stage"] = "sub_prepare"
    job["phase_message"] = "Preparing subtitles"
    job["progress_percent"] = 0
    _save_jobs()
    thread = threading.Thread(
        target=run_subtitle,
        args=(job_id, mode, src_lang, tgt_lang, translate, font_size,
              font_name, text_color, outline_color, bg_color, bg_opacity, restyle_only),
    )
    thread.daemon = True
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/subtitle/<job_id>", methods=["DELETE"])
def delete_subtitle(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    srt = job.pop("subtitle_file", None)
    if srt and os.path.exists(srt):
        try:
            os.remove(srt)
        except OSError:
            pass
    return jsonify({"ok": True})


@app.route("/api/subtitle-file/<job_id>")
def download_subtitle(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    srt = job.get("subtitle_file", "")
    if not srt or not os.path.exists(srt):
        return jsonify({"error": "SRT yok"}), 404
    base = os.path.splitext(job.get("filename") or os.path.basename(srt))[0]
    return send_file(srt, as_attachment=True, download_name=f"{base}.srt")


@app.route("/api/subtitle-vtt/<job_id>")
def stream_subtitle_vtt(job_id):
    """HTML5 <track> icin SRT'yi WebVTT'ye cevirip dondur."""
    from flask import Response
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Not found"}), 404
    srt = job.get("subtitle_file", "")
    if not srt or not os.path.exists(srt):
        return jsonify({"error": "SRT yok"}), 404
    with open(srt, encoding="utf-8") as f:
        body = f.read()
    # SRT -> VTT: zaman damgalarinda ',' yerine '.', basa WEBVTT
    body = re.sub(
        r"(\d{2}:\d{2}:\d{2}),(\d{3})",
        r"\1.\2",
        body,
    )
    vtt = "WEBVTT\n\n" + body
    return Response(vtt, mimetype="text/vtt")


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
        "has_subtitle": bool(job.get("subtitle_file") and os.path.exists(job.get("subtitle_file", ""))),
        "started_at": job.get("started_at"),
        "updated_at": job.get("updated_at"),
        "elapsed_sec": _elapsed_seconds(job),
    })


@app.route("/api/jobs")
def list_jobs():
    out = []
    for jid, job in jobs.items():
        f = job.get("file", "")
        if not f:
            candidates = glob.glob(os.path.join(DOWNLOAD_DIR, f"{jid}*.mp4")) + \
                         glob.glob(os.path.join(DOWNLOAD_DIR, f"{jid}*.mp3"))
            if candidates:
                f = max(candidates, key=os.path.getmtime)
                job["file"] = f
                if not job.get("filename"):
                    job["filename"] = os.path.basename(f)
        if not f or not os.path.exists(f):
            continue
        out.append({
            "job_id": jid,
            "title": job.get("title", ""),
            "filename": job.get("filename") or os.path.basename(f),
            "format": job.get("format", ""),
            "stage": job.get("stage", ""),
            "status": job.get("status", ""),
            "error": job.get("error", ""),
            "url": job.get("url", ""),
            "created_at": job.get("created_at", 0),
            "has_subtitle": bool(job.get("subtitle_file") and os.path.exists(job.get("subtitle_file", ""))),
        })
    out.sort(key=lambda j: j["created_at"], reverse=True)
    return jsonify(out)


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def delete_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    f = job.get("file", "")
    if f and os.path.exists(f):
        try:
            os.remove(f)
        except OSError:
            pass
    jobs.pop(job_id, None)
    _save_jobs()
    return jsonify({"ok": True})


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "File not ready"}), 404
    f = job.get("file", "")
    if not f or not os.path.exists(f):
        return jsonify({"error": "File not ready"}), 404
    return send_file(f, as_attachment=True, download_name=job.get("filename") or os.path.basename(f))


@app.route("/api/media/<job_id>")
def stream_media(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "File not ready"}), 404
    f = job.get("file", "")
    if not f or not os.path.exists(f):
        return jsonify({"error": "File not ready"}), 404
    mime, _ = mimetypes.guess_type(f)
    return send_file(f, as_attachment=False, mimetype=mime or "application/octet-stream")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
