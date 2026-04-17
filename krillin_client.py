import os
import shutil
import socket
import subprocess
import tempfile
import time

import requests
from faster_whisper import WhisperModel

KRILLIN_DIR = r"C:\Users\umuti\Projects\KrillinAI"
KRILLIN_EXE = os.path.join(KRILLIN_DIR, "krillin-server.exe")
KRILLIN_HOST = "127.0.0.1"
KRILLIN_PORT = 8888
BASE = f"http://{KRILLIN_HOST}:{KRILLIN_PORT}"

_proc = None
_current_llm = None
_lang_detect_model = None
LOG_PATH = os.path.join(KRILLIN_DIR, "krillin-server.log")
CONFIG_PATH = os.path.join(KRILLIN_DIR, "config", "config.toml")

LLM_PRESETS = {
    "deepseek": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "deepseek/deepseek-chat",
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "llama3.2:latest",
    },
}

VOICE_MAP = {
    "tr": ["tr-TR-EmelNeural", "tr-TR-AhmetNeural"],
    "en": ["en-US-JennyNeural", "en-US-GuyNeural"],
    "de": ["de-DE-KatjaNeural", "de-DE-ConradNeural"],
    "es": ["es-ES-ElviraNeural", "es-ES-AlvaroNeural"],
    "fr": ["fr-FR-DeniseNeural", "fr-FR-HenriNeural"],
    "it": ["it-IT-ElsaNeural", "it-IT-DiegoNeural"],
    "ru": ["ru-RU-SvetlanaNeural", "ru-RU-DmitryNeural"],
    "ar": ["ar-SA-ZariyahNeural", "ar-SA-HamedNeural"],
    "ja": ["ja-JP-NanamiNeural", "ja-JP-KeitaNeural"],
    "zh": ["zh-CN-XiaoxiaoNeural", "zh-CN-YunxiNeural"],
    "pt": ["pt-BR-FranciscaNeural", "pt-BR-AntonioNeural"],
}


def _collect_error_fragments(node, out: list[str], depth: int = 0) -> None:
    if depth > 6 or node is None:
        return
    if isinstance(node, str):
        text = node.strip()
        if text:
            out.append(text)
        return
    if isinstance(node, list):
        for item in node:
            _collect_error_fragments(item, out, depth + 1)
        return
    if not isinstance(node, dict):
        return

    keys = (
        "msg", "message", "reason", "detail", "error_msg", "err_msg", "errMsg",
        "errorMessage", "fail_reason", "failed_reason", "failure_reason", "cause",
    )
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            out.append(value.strip())

    for value in node.values():
        if isinstance(value, (dict, list)):
            _collect_error_fragments(value, out, depth + 1)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _extract_error_message(payload: dict) -> str:
    fragments: list[str] = []
    _collect_error_fragments(payload, fragments)
    fragments = _dedupe_keep_order(fragments)
    if fragments:
        return " | ".join(fragments[:12])
    return "Bilinmeyen KrillinAI hatasi"


def _latest_log_hint() -> str:
    try:
        if not os.path.exists(LOG_PATH):
            return ""
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-100:]
        important = [ln.strip() for ln in lines if ("error" in ln.lower() or "failed" in ln.lower())]
        if important:
            tail = " || ".join(important[-3:])
            return f" | log: {tail[:1200]}"
    except Exception:
        pass
    return ""


def _is_failed_state(task_data: dict) -> bool:
    state_fields = ("status", "state", "task_status", "process_status", "task_state", "phase")
    failed_values = {"failed", "error", "fail", "aborted", "cancelled", "canceled"}
    for field in state_fields:
        value = task_data.get(field)
        if isinstance(value, str) and value.strip().lower() in failed_values:
            return True
    return False


def _get_lang_detect_model() -> WhisperModel:
    global _lang_detect_model
    if _lang_detect_model is None:
        _lang_detect_model = WhisperModel("small", device="cpu", compute_type="int8")
    return _lang_detect_model


def _detect_source_lang(video_path: str) -> str:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            sample_wav = os.path.join(tmp, "sample.wav")
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "0", "-t", "35", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", sample_wav],
                check=True,
                capture_output=True,
            )
            model = _get_lang_detect_model()
            _, info = model.transcribe(sample_wav, beam_size=1, vad_filter=True)
            lang = (getattr(info, "language", "") or "").strip().lower()
            if len(lang) == 2:
                return lang
    except Exception:
        pass
    return "en"


def _write_config(llm: str) -> None:
    cfg = LLM_PRESETS[llm]
    if llm == "deepseek":
        api_key = os.environ.get(cfg["api_key_env"], "").strip()
        model = os.environ.get(cfg["model_env"], "").strip() or cfg["default_model"]
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY ayarli degil. KrillinAI + DeepSeek icin key gir.")
    else:
        api_key = cfg["api_key"]
        model = cfg["model"]

    tmpl = (
        "[app]\n"
        "    segment_duration = 5\n    transcribe_parallel_num = 1\n    translate_parallel_num = 3\n"
        "    transcribe_max_attempts = 3\n    translate_max_attempts = 5\n"
        "    max_sentence_length = 70\n    proxy = \"\"\n\n"
        "[server]\n    host = \"127.0.0.1\"\n    port = 8888\n\n"
        f"[llm]\n    base_url = \"{cfg['base_url']}\"\n    api_key = \"{api_key}\"\n"
        f"    model = \"{model}\"\n    json = false\n\n"
        "[transcribe]\n    provider = \"fasterwhisper\"\n    enable_gpu_acceleration = false\n"
        "    [transcribe.fasterwhisper]\n        model = \"medium\"\n\n"
        "[tts]\n    provider = \"edge-tts\"\n"
    )
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(tmpl)


def _port_open() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        try:
            s.connect((KRILLIN_HOST, KRILLIN_PORT))
            return True
        except OSError:
            return False


def _stop_server() -> None:
    global _proc
    subprocess.run(["taskkill", "/F", "/IM", "krillin-server.exe"], capture_output=True)
    _proc = None
    for _ in range(10):
        if not _port_open():
            return
        time.sleep(0.5)


def ensure_server(llm: str = "deepseek") -> None:
    global _proc, _current_llm
    if _port_open() and _current_llm == llm:
        return
    if _port_open() and _current_llm != llm:
        _stop_server()
    _write_config(llm)
    _current_llm = llm
    if not os.path.exists(KRILLIN_EXE):
        raise RuntimeError(f"KrillinAI binary bulunamadi: {KRILLIN_EXE}")
    log_file = open(LOG_PATH, "ab")
    shim_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krillin_shim")
    env = os.environ.copy()
    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    _proc = subprocess.Popen(
        [KRILLIN_EXE],
        cwd=KRILLIN_DIR,
        stdout=log_file,
        stderr=log_file,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    for _ in range(60):
        if _port_open():
            return
        time.sleep(1)
    raise RuntimeError("KrillinAI server baslatilamadi (60s timeout)")


def upload_file(video_path: str) -> str:
    with open(video_path, "rb") as f:
        r = requests.post(f"{BASE}/api/file", files={"file": (os.path.basename(video_path), f)}, timeout=300)
    r.raise_for_status()
    data = r.json()
    if data.get("error") != 0:
        raise RuntimeError(f"Yukleme hatasi: {data.get('msg')}")
    return data["data"]["file_path"][0]


def start_task(server_url: str, origin_lang: str = "en", target_lang: str = "tr", tts_voice: str | None = None) -> str:
    if not tts_voice:
        tts_voice = VOICE_MAP.get(target_lang, ["tr-TR-EmelNeural"])[0]
    payload = {
        "url": server_url,
        "target_lang": target_lang,
        "bilingual": 2,
        "translation_subtitle_pos": 1,
        "tts": 1,
        "tts_voice_code": tts_voice,
        "modal_filter": 2,
        "embed_subtitle_video_type": "none",
        "language": target_lang,
    }
    if origin_lang and origin_lang != "auto":
        payload["origin_lang"] = origin_lang
    r = requests.post(f"{BASE}/api/capability/subtitleTask", json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("error") != 0:
        raise RuntimeError(f"Task baslatma hatasi: {data.get('msg')}")
    return data["data"]["task_id"]


_STEP_PERCENTS = {0: 5, 1: 15, 2: 35, 3: 55, 4: 85, 5: 95, 6: 99}


def poll_task(task_id: str, on_progress=None, timeout_sec: int = 1800) -> dict:
    deadline = time.time() + timeout_sec
    last_reported = -1.0
    while time.time() < deadline:
        r = requests.get(f"{BASE}/api/capability/subtitleTask", params={"taskId": task_id}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error") != 0:
            raise RuntimeError(f"Task hata: {_extract_error_message(data)}{_latest_log_hint()}")
        d = data.get("data") or {}
        if _is_failed_state(d):
            raise RuntimeError(f"Task basarisiz: {_extract_error_message(data)}{_latest_log_hint()}")

        pct = float(d.get("process_percent") or 0)
        step = int(d.get("last_success_step_num") or 0)
        synth = _STEP_PERCENTS.get(step, min(95, step * 15))
        pct = max(pct, synth)
        if on_progress and pct > last_reported:
            on_progress(pct)
            last_reported = pct

        if (d.get("process_percent") or 0) >= 100 and d.get("speech_download_url"):
            return d
        time.sleep(3)
    raise TimeoutError("KrillinAI task zaman asimi")


def download_result(speech_url: str, out_path: str) -> None:
    full = f"{BASE}{speech_url}"
    with requests.get(full, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(out_path, "wb") as f:
            shutil.copyfileobj(r.raw, f)


def dub_video(video_path: str, out_path: str, origin_lang: str = "auto", target_lang: str = "tr",
              on_progress=None, llm: str = "deepseek", gender: str = "female") -> None:
    ensure_server(llm)
    if origin_lang == "auto":
        origin_lang = _detect_source_lang(video_path)

    server_url = upload_file(video_path)
    all_voices = VOICE_MAP.get(target_lang, ["tr-TR-EmelNeural", "tr-TR-AhmetNeural"])
    primary_idx = 1 if gender == "male" else 0
    voices = [all_voices[primary_idx]] + [v for i, v in enumerate(all_voices) if i != primary_idx]
    last_err = None
    for voice in voices:
        try:
            task_id = start_task(server_url, origin_lang=origin_lang, target_lang=target_lang, tts_voice=voice)
            result = poll_task(task_id, on_progress=on_progress)
            speech_url = result.get("speech_download_url")
            if not speech_url:
                raise RuntimeError("Dublaj cikti URL'si yok")
            video_url = speech_url.replace("tts_final_audio.wav", "video_with_tts.mp4")
            download_result(video_url, out_path)
            return
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if ("srttfiletospeech" not in msg and "srttfile" not in msg and
                    "tts" not in msg and "speech" not in msg):
                break

    raise RuntimeError(str(last_err) if last_err else "KrillinAI dublaj basarisiz")
