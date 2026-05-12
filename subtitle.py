"""
subtitle.py - SRT uretimi + opsiyonel videoya gomme.

Iki mod:
  - sidecar: sadece .srt dosyasi
  - burn:    .srt + ffmpeg ile videoya gomulu .mp4

Transkripsiyon: OpenRouter STT > Local Whisper.
Ceviri opsiyonel; kapatilirsa altyazi orijinal dilde.
"""

import base64
import os
import subprocess
import tempfile
from collections.abc import Callable

import requests

from dub import (
    _extract_audio,
    _transcribe_local,
    _translate_google,
    _translate_openrouter,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _subprocess_kwargs() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}

MAX_LINE_CHARS = 42
MAX_LINES = 2
MAX_DUR = 6.0
# Maksimum okunabilir karakter/saniye (altyazi standardi ~17 cps).
_MAX_CPS = 17.0
_MIN_DUR = 1.0
_MIN_GAP = 0.05


def _audio_duration(path: str) -> float | None:
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True,
            text=True,
            **_subprocess_kwargs(),
        )
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _transcribe_openrouter(wav_path: str, src_lang: str) -> tuple[list, str]:
    """OpenRouter STT endpoint'i ile coarse segmentli transkripsiyon.
    Endpoint text dondurdugu icin, zamanlamayi chunk penceresiyle kurar."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set.")

    model = os.environ.get("OPENROUTER_STT_MODEL", "openai/whisper-large-v3-turbo")
    chunk_sec = int(os.environ.get("OPENROUTER_STT_CHUNK_SEC", "30"))
    chunk_sec = max(10, min(120, chunk_sec))

    total = _audio_duration(wav_path) or 0.0
    if total <= 0:
        raise RuntimeError("Could not determine audio duration.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/reclip",
        "X-Title": "ReClip",
    }

    segments: list[tuple[float, float, str]] = []
    detected = src_lang if src_lang != "auto" else "auto"

    with tempfile.TemporaryDirectory() as tmp:
        offset = 0.0
        idx = 0
        while offset < total - 0.05:
            dur = min(float(chunk_sec), total - offset)
            chunk_path = os.path.join(tmp, f"or_chunk_{idx}.flac")

            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", wav_path,
                    "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}",
                    "-ac", "1", "-ar", "16000",
                    "-c:a", "flac", "-compression_level", "8",
                    chunk_path,
                ],
                check=True,
                capture_output=True,
                **_subprocess_kwargs(),
            )

            with open(chunk_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")

            payload: dict = {
                "model": model,
                "input_audio": {
                    "data": b64,
                    "format": "flac",
                },
            }
            if src_lang != "auto":
                payload["language"] = src_lang

            resp = requests.post(
                "https://openrouter.ai/api/v1/audio/transcriptions",
                headers=headers,
                json=payload,
                timeout=240,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OpenRouter STT error {resp.status_code}: {resp.text[:300]}")

            body = resp.json()
            text = (body.get("text") or "").strip()
            if text:
                segments.append((offset, offset + dur, text))

            offset += dur
            idx += 1

    if not segments:
        raise RuntimeError("OpenRouter STT returned no text.")
    return segments, detected


def _enforce_read_time(segments: list) -> list:
    """Ceviri sonrasi metin uzadiysa segment suresini sonraki baslangici
    asmadan uzat (okuma hizi normalize)."""
    if not segments:
        return segments
    out = [list(seg[:3]) for seg in segments]
    for i in range(len(out)):
        s, e, t = out[i]
        if not t:
            continue
        ideal = max(_MIN_DUR, len(t) / _MAX_CPS)
        if e - s >= ideal:
            continue
        next_s = out[i + 1][0] if i + 1 < len(out) else s + ideal
        new_e = min(s + ideal, max(e, next_s - _MIN_GAP))
        if new_e > e:
            out[i][1] = new_e
    return [tuple(seg) for seg in out]


def _fmt_time(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms == 1000:
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _norm_hex(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) != 6:
        return fallback
    try:
        int(raw, 16)
    except ValueError:
        return fallback
    return f"#{raw.upper()}"


def _hex_to_ass(value: str, alpha: int = 0) -> str:
    """#RRGGBB -> &HAABBGGRR"""
    v = _norm_hex(value, "#FFFFFF")[1:]
    rr, gg, bb = v[0:2], v[2:4], v[4:6]
    aa = max(0, min(255, int(alpha)))
    return f"&H{aa:02X}{bb}{gg}{rr}"


def burn_subtitles_into_video(
    video_path: str,
    srt_path: str,
    burned_out: str,
    font_size: int = 22,
    font_name: str = "Arial",
    text_color: str = "#FFFFFF",
    outline_color: str = "#000000",
    bg_color: str = "#000000",
    bg_opacity: int = 0,
) -> None:
    # Windows'ta ffmpeg subtitles filtresi mutlak path'leri sevmiyor (drive letter).
    # Cozum: SRT'nin bulundugu dizinde calis, dosya adini relatif ver.
    srt_dir = os.path.dirname(os.path.abspath(srt_path))
    srt_name = os.path.basename(srt_path)
    video_abs = os.path.abspath(video_path)
    burn_abs = os.path.abspath(burned_out)

    safe_font = (font_name or "Arial").strip()[:64] or "Arial"
    primary = _hex_to_ass(text_color, alpha=0)
    outline = _hex_to_ass(outline_color, alpha=0)
    box_alpha = int(round((100 - max(0, min(100, int(bg_opacity)))) * 255 / 100))
    back = _hex_to_ass(bg_color, alpha=box_alpha)
    border_style = 3 if int(bg_opacity) > 0 else 1
    outline_px = 1 if border_style == 3 else 2

    vf = (
        f"subtitles={srt_name}:force_style="
        f"'FontName={safe_font},Fontsize={int(font_size)},"
        f"PrimaryColour={primary},OutlineColour={outline},"
        f"BackColour={back},BorderStyle={border_style},"
        f"Outline={outline_px},Shadow=0,MarginV=24'"
    )
    r = subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_abs,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            burn_abs,
        ],
        cwd=srt_dir,
        capture_output=True,
        **_subprocess_kwargs(),
    )
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"ffmpeg subtitle burn error: {err}")


def _wrap(text: str) -> str:
    words = text.split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= MAX_LINE_CHARS:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= MAX_LINES:
                break
    if cur and len(lines) < MAX_LINES:
        lines.append(cur)
    if len(lines) >= MAX_LINES and len(words) > sum(len(l.split()) for l in lines):
        # tasan kismi son satira sigdir (kesilmesin)
        used = sum(len(l.split()) for l in lines)
        rest = " ".join(words[used:])
        lines[-1] = lines[-1] + " " + rest
    return "\n".join(lines)


def _split_long(segments: list) -> list:
    """Cok uzun segmentleri yaklasik esit parcalara bol (okunabilirlik icin)."""
    out: list[tuple[float, float, str]] = []
    for seg in segments:
        s, e, t = seg[0], seg[1], seg[2]
        t = (t or "").strip()
        if not t:
            continue
        dur = max(0.0, e - s)
        if dur <= MAX_DUR and len(t) <= MAX_LINE_CHARS * MAX_LINES:
            out.append((s, e, t))
            continue
        words = t.split()
        if not words:
            out.append((s, e, t))
            continue
        chunks_by_dur = max(1, int(round(dur / MAX_DUR)))
        chunks_by_len = max(1, (len(t) + MAX_LINE_CHARS * MAX_LINES - 1) // (MAX_LINE_CHARS * MAX_LINES))
        n = max(chunks_by_dur, chunks_by_len)
        per = max(1, (len(words) + n - 1) // n)
        chunks = [" ".join(words[i:i + per]) for i in range(0, len(words), per)]
        if not chunks:
            out.append((s, e, t))
            continue
        # Karakter agirlikli zamanlama: her chunk uzunlugu kadar sure alir.
        # Esit zaman dilimine bolmek "merhaba" ile "dunya merhaba dunya"ya ayni
        # sureyi verirdi -> okuma hizi uyumsuzlugu. Karakter orani daha gercekci.
        total_chars = sum(len(c) for c in chunks) or 1
        cur = s
        for i, c in enumerate(chunks):
            if i == len(chunks) - 1:
                ce = e
            else:
                ce = cur + dur * (len(c) / total_chars)
            out.append((cur, ce, c))
            cur = ce
    return out


def write_srt(segments: list, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, e, t) in enumerate(segments, 1):
            if e - s < 0.4:
                e = s + 0.4
            f.write(f"{i}\n{_fmt_time(s)} --> {_fmt_time(e)}\n{_wrap(t)}\n\n")


def generate_subtitles(
    video_path: str,
    srt_out: str,
    burned_out: str | None = None,
    src_lang: str = "auto",
    tgt_lang: str = "tr",
    translate: bool = False,
    font_size: int = 22,
    font_name: str = "Arial",
    text_color: str = "#FFFFFF",
    outline_color: str = "#000000",
    bg_color: str = "#000000",
    bg_opacity: int = 0,
    on_progress: Callable[[float, str], None] | None = None,
    existing_segments: list | None = None,
) -> None:
    use_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "src.wav")
        if on_progress:
            on_progress(8, "Extracting audio")
        _extract_audio(video_path, wav)

        if existing_segments is not None:
            if on_progress:
                on_progress(25, "Using existing segments")
            segs = existing_segments
            detected = src_lang if src_lang != "auto" else "en"
        else:
            if on_progress:
                on_progress(25, "Transcribing")
            if use_openrouter:
                segs, detected = _transcribe_openrouter(wav, src_lang)
            else:
                segs, detected = _transcribe_local(wav)

        if not segs:
            raise RuntimeError("No speech detected")

        # Diarize dict formatini tuple'a indir
        if isinstance(segs[0], dict):
            segs = [(s["start"], s["end"], s["text"]) for s in segs]

        source = detected if src_lang == "auto" else src_lang

        if translate and source != tgt_lang:
            if on_progress:
                on_progress(55, "Translating")
            if use_openrouter:
                segs = _translate_openrouter(segs, source, tgt_lang)
            else:
                segs = _translate_google(segs, source, tgt_lang)

        segs = _split_long(segs)
        segs = _enforce_read_time(segs)

        if on_progress:
            on_progress(78, "Writing SRT")
        write_srt(segs, srt_out)

        if burned_out:
            if on_progress:
                on_progress(88, "Burning subtitles into video")
            burn_subtitles_into_video(
                video_path,
                srt_out,
                burned_out,
                font_size=font_size,
                font_name=font_name,
                text_color=text_color,
                outline_color=outline_color,
                bg_color=bg_color,
                bg_opacity=bg_opacity,
            )

        if on_progress:
            on_progress(100, "Subtitles complete")
