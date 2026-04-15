import os
import uuid
import glob
import json
import subprocess
import threading
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

# Ayarlar - .env dosyasini okuyup os.environ'a yukle
_MANAGED_KEYS = ["COLAB_URL", "GROQ_API_KEY", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"]


def _read_env() -> dict[str, str]:
    """Dosyadan key=value satirlarini oku."""
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
    """Mevcut .env dosyasini koru, sadece managed key'leri guncelle/ekle."""
    lines: list[str] = []
    existing_keys: set[str] = set()

    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.partition("=")[0].strip()
                    if k in data:
                        # Guncellenecek satiri degistir
                        if data[k]:  # bos deger varsa satiri at
                            lines.append(f"{k}={data[k]}\n")
                        existing_keys.add(k)
                        continue
                lines.append(line if line.endswith("\n") else line + "\n")

    # Dosyada olmayan yeni key'leri ekle
    for k, v in data.items():
        if k not in existing_keys and v:
            lines.append(f"{k}={v}\n")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)


def _apply_env(data: dict[str, str]) -> None:
    """os.environ'u aninda guncelle (yeniden baslatma gerekmez)."""
    for k, v in data.items():
        if v:
            os.environ[k] = v
        elif k in os.environ:
            del os.environ[k]


# Uygulama baslarken .env yukle
_apply_env(_read_env())


def run_download(job_id, url, format_choice, format_id, dub=False, dub_engine="fast", src_lang="auto", tgt_lang="tr"):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        # Popen kullan - uzun videolarda timeout kopması önlemek için
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _, stderr = proc.communicate()  # thread içinde bekliyoruz, süresiz
        if proc.returncode != 0:
            job["status"] = "error"
            job["error"] = stderr.strip().split("\n")[-1] if stderr.strip() else "Download failed"
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        if dub and format_choice != "audio" and chosen.endswith(".mp4"):
            try:
                job["status"] = "dubbing"
                dubbed_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_tr.mp4")
                if dub_engine.startswith("krillin"):
                    from krillin_client import dub_video as krillin_dub
                    llm = "ollama" if dub_engine == "krillin_ollama" else "deepseek"
                    krillin_dub(chosen, dubbed_path, origin_lang=src_lang, target_lang=tgt_lang, llm=llm)
                else:
                    from dub import dub_video
                    dub_video(chosen, dubbed_path, src_lang=src_lang, tgt_lang=tgt_lang)
                try:
                    os.remove(chosen)
                except OSError:
                    pass
                chosen = dubbed_path
            except Exception as e:
                job["status"] = "error"
                job["error"] = f"Dublaj hatası: {e}"
                return

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


def run_dub_existing(job_id, dub_engine="fast", src_lang="auto", tgt_lang="tr"):
    job = jobs[job_id]
    src_path = job.get("file", "")
    if not src_path or not os.path.exists(src_path):
        job["status"] = "error"
        job["error"] = "Kaynak dosya bulunamadi"
        return
    if not src_path.lower().endswith(".mp4"):
        job["status"] = "error"
        job["error"] = "Dublaj sadece MP4 dosyalarda destekleniyor"
        return

    try:
        job["status"] = "dubbing"
        base_name = os.path.splitext(os.path.basename(src_path))[0]
        dubbed_path = os.path.join(DOWNLOAD_DIR, f"{base_name}_dub_{tgt_lang}.mp4")

        if dub_engine.startswith("krillin"):
            from krillin_client import dub_video as krillin_dub

            llm = "ollama" if dub_engine == "krillin_ollama" else "deepseek"
            krillin_dub(src_path, dubbed_path, origin_lang=src_lang, target_lang=tgt_lang, llm=llm)
        else:
            from dub import dub_video

            dub_video(src_path, dubbed_path, src_lang=src_lang, tgt_lang=tgt_lang)

        job["status"] = "done"
        job["file"] = dubbed_path
        safe_title = "".join(c for c in job.get("title", "") if c not in r'\/:*?"<>|').strip()[:20].strip()
        if safe_title:
            job["filename"] = f"{safe_title}_dub_{tgt_lang}.mp4"
        else:
            job["filename"] = os.path.basename(dubbed_path)
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"Dublaj hatasi: {e}"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    env = _read_env()
    # API key'leri maskele - sadece son 4 karakter gorunur
    def mask(v: str) -> str:
        return ("*" * (len(v) - 4) + v[-4:]) if len(v) > 4 else ("*" * len(v))

    result = {}
    for k in _MANAGED_KEYS:
        v = env.get(k, os.environ.get(k, ""))
        shown = v if k == "COLAB_URL" else (mask(v) if v else "")
        result[k] = {"masked": shown, "set": bool(v)}
    return jsonify(result)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.json or {}
    to_save = {}
    for k in _MANAGED_KEYS:
        v = data.get(k, "").strip()
        # Yildiz iceren deger gonderilidiyse degistirme (masked eski deger)
        if v and "*" not in v:
            to_save[k] = v
        elif not v:
            to_save[k] = ""  # silmek icin bos string
    _write_env(to_save)
    _apply_env(to_save)
    return jsonify({"ok": True})


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout.strip().splitlines()[0])

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

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
    src_lang = data.get("src_lang", "auto")
    tgt_lang = data.get("tgt_lang", "tr")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title, "format": format_choice}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id, dub, dub_engine, src_lang, tgt_lang))
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
    if not src_file.lower().endswith(".mp4"):
        return jsonify({"error": "Sadece MP4 dosyaya dublaj eklenebilir"}), 400

    data = request.json or {}
    dub_engine = data.get("dub_engine", "fast")
    src_lang = data.get("src_lang", "auto")
    tgt_lang = data.get("tgt_lang", "tr")

    thread = threading.Thread(
        target=run_dub_existing,
        args=(job_id, dub_engine, src_lang, tgt_lang),
    )
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
        "error": job.get("error"),
        "filename": job.get("filename"),
        "format": job.get("format"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
