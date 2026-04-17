"""
dub.py - video dublaj motoru

Transkripsiyon engine onceligi (.env'e gore):
  COLAB_URL          -> Colab GPU Whisper large-v3 (~5-8 dk / 250 dk video)  [EN IYI]
  GROQ_API_KEY       -> Groq Whisper API (~hizli ama 250 dk'da rate limit var)
  Hicbiri yoksa      -> local Whisper CPU (~80 dk)

Ceviri engine:
  OPENROUTER_API_KEY -> OpenRouter LLM batch (~$0.006 / 250 dk video)
  Hicbiri yoksa      -> Google Translate
"""

import asyncio
import os
import subprocess
import tempfile
from collections.abc import Callable

import edge_tts
from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel

_MODEL = None
_GROQ_CHUNK_MIN = 20       # Groq'a gonderilecek chunk suresi (dakika)
_TRANSLATE_BATCH = 80      # tek LLM / Google Translate cagrisindaki cumle sayisi

VOICE_MAP = {
    "tr": {"female": "tr-TR-EmelNeural", "male": "tr-TR-AhmetNeural"},
    "en": {"female": "en-US-JennyNeural", "male": "en-US-GuyNeural", "child": "en-US-AnaNeural"},
    "de": {"female": "de-DE-KatjaNeural", "male": "de-DE-ConradNeural"},
    "es": {"female": "es-ES-ElviraNeural", "male": "es-ES-AlvaroNeural"},
    "fr": {"female": "fr-FR-DeniseNeural", "male": "fr-FR-HenriNeural"},
    "it": {"female": "it-IT-ElsaNeural", "male": "it-IT-DiegoNeural"},
    "ru": {"female": "ru-RU-SvetlanaNeural", "male": "ru-RU-DmitryNeural"},
    "ar": {"female": "ar-SA-ZariyahNeural", "male": "ar-SA-HamedNeural"},
    "ja": {"female": "ja-JP-NanamiNeural", "male": "ja-JP-KeitaNeural"},
    "zh": {"female": "zh-CN-XiaoxiaoNeural", "male": "zh-CN-YunxiNeural", "child": "zh-CN-XiaoyiNeural"},
    "pt": {"female": "pt-BR-FranciscaNeural", "male": "pt-BR-AntonioNeural"},
}

_LANG_NAMES = {
    "tr": "Turkish", "en": "English", "de": "German", "es": "Spanish",
    "fr": "French",  "it": "Italian", "ru": "Russian", "ar": "Arabic",
    "ja": "Japanese", "zh": "Chinese", "pt": "Portuguese",
}


# ---------------------------------------------------------------------------
# Yardimci - ffprobe ile sure al
# ---------------------------------------------------------------------------

def _audio_duration(path: str) -> float | None:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Ses cikartma
# ---------------------------------------------------------------------------

def _extract_audio(video_path: str, out_wav: str) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000", out_wav],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# Transkripsiyon - Local Whisper (fallback)
# ---------------------------------------------------------------------------

def _get_model():
    global _MODEL
    if _MODEL is None:
        _MODEL = WhisperModel("small", device="cpu", compute_type="int8")
    return _MODEL


def _transcribe_local(wav_path: str) -> tuple[list, str]:
    model = _get_model()
    segments, info = model.transcribe(wav_path, vad_filter=True)
    result = [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]
    return result, info.language


# ---------------------------------------------------------------------------
# Transkripsiyon - Groq Whisper API
# ---------------------------------------------------------------------------

def _transcribe_colab(wav_path: str, src_lang: str) -> tuple[list, str]:
    """
    Colab Whisper API ile transkripsiyon - GPU hizli, rate limit yok.
    Dosyayi 32kbps MP3'e sikistirip tek istekte gonderir.
    """
    import requests

    colab_url = os.environ["COLAB_URL"].rstrip("/")

    # WAV -> kucuk MP3 (upload icin)
    mp3_path = wav_path.replace(".wav", "_upload.mp3")
    subprocess.run([
        "ffmpeg", "-y", "-i", wav_path,
        "-ac", "1", "-ar", "16000", "-b:a", "32k", mp3_path,
    ], check=True, capture_output=True)

    try:
        with open(mp3_path, "rb") as f:
            resp = requests.post(
                f"{colab_url}/transcribe",
                files={"file": ("audio.mp3", f, "audio/mpeg")},
                data={"src_lang": src_lang},
                timeout=900,  # 15 dk - buyuk dosyalar icin
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Colab API hatasi {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Colab transkripsiyon hatasi: {data['error']}")
        segments = [(s["start"], s["end"], s["text"]) for s in data["segments"]]
        return segments, data.get("language", "en")
    finally:
        try:
            os.unlink(mp3_path)
        except OSError:
            pass


def _transcribe_groq(wav_path: str, src_lang: str) -> tuple[list, str]:
    """
    Groq whisper-large-v3-turbo ile transkripsiyon.
    WAV'i 20 dk'lik MP3 chunk'lara boler (Groq 25 MB siniri).
    """
    import openai

    client = openai.OpenAI(
        base_url="https://api.groq.com/openai/v1",
        api_key=os.environ["GROQ_API_KEY"],
    )

    total = _audio_duration(wav_path) or 0.0
    chunk_sec = _GROQ_CHUNK_MIN * 60
    all_segments: list[tuple[float, float, str]] = []
    detected_lang = src_lang if src_lang != "auto" else "en"

    with tempfile.TemporaryDirectory() as tmp:
        offset = 0.0
        idx = 0
        while offset < total:
            dur = min(chunk_sec, total - offset)
            chunk_mp3 = os.path.join(tmp, f"chunk_{idx}.mp3")

            # Chunk'i 32kbps MP3'e cevir (kucuk boyut, Groq siniri altinda)
            subprocess.run([
                "ffmpeg", "-y", "-i", wav_path,
                "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}",
                "-ac", "1", "-ar", "16000", "-b:a", "32k",
                chunk_mp3,
            ], check=True, capture_output=True)

            extra: dict = {}
            if src_lang != "auto":
                extra["language"] = src_lang

            with open(chunk_mp3, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=f,
                    response_format="verbose_json",
                    **extra,
                )

            if getattr(resp, "language", None):
                detected_lang = resp.language

            for seg in getattr(resp, "segments", None) or []:
                s = getattr(seg, "start", None) or seg.get("start", 0)
                e = getattr(seg, "end", None) or seg.get("end", 0)
                t = (getattr(seg, "text", None) or seg.get("text", "")).strip()
                if t:
                    all_segments.append((s + offset, e + offset, t))

            offset += chunk_sec
            idx += 1

    return all_segments, detected_lang


# ---------------------------------------------------------------------------
# Ceviri - Google Translate (fallback)
# ---------------------------------------------------------------------------

def _translate_google(segments: list, src_lang: str, tgt_lang: str) -> list:
    if src_lang == tgt_lang:
        return segments
    texts = [t for _, _, t in segments]
    translated: list[str] = []
    try:
        tr = GoogleTranslator(source=src_lang or "auto", target=tgt_lang)
        for i in range(0, len(texts), _TRANSLATE_BATCH):
            chunk = texts[i:i + _TRANSLATE_BATCH]
            result = tr.translate_batch(chunk)
            translated.extend(r or t for r, t in zip(result, chunk))
    except Exception:
        try:
            tr = GoogleTranslator(source=src_lang or "auto", target=tgt_lang)
        except Exception:
            tr = GoogleTranslator(source="auto", target=tgt_lang)
        for t in texts:
            try:
                translated.append(tr.translate(t) or t)
            except Exception:
                translated.append(t)
    return [(s, e, tr) for (s, e, _), tr in zip(segments, translated)]


# ---------------------------------------------------------------------------
# Ceviri - OpenRouter LLM
# ---------------------------------------------------------------------------

def _translate_openrouter(segments: list, src_lang: str, tgt_lang: str) -> list:
    """
    OpenRouter uzerinden LLM ile batch ceviri.
    Model: OPENROUTER_MODEL env degiskeninden, yoksa deepseek/deepseek-chat-v3-0324.
    """
    import openai

    model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
    client = openai.OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=os.environ["OPENROUTER_API_KEY"],
        default_headers={
            "HTTP-Referer": "https://github.com/reclip",
            "X-Title": "ReClip",
        },
    )

    lang_name = _LANG_NAMES.get(tgt_lang, tgt_lang)
    texts = [t for _, _, t in segments]
    translated: list[str] = []

    for i in range(0, len(texts), _TRANSLATE_BATCH):
        chunk = texts[i:i + _TRANSLATE_BATCH]
        # "N|metin" formati - parsing icin guvenlir
        numbered = "\n".join(f"{j + 1}|{t}" for j, t in enumerate(chunk))

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            f"You are a subtitle translator. Translate each line to {lang_name}. "
                            "Return ONLY the lines in exact format 'N|translation'. "
                            "Preserve line count. No extra text."
                        ),
                    },
                    {"role": "user", "content": numbered},
                ],
                temperature=0.1,
                max_tokens=4096,
            )
            out_map: dict[int, str] = {}
            for line in resp.choices[0].message.content.strip().split("\n"):
                if "|" in line:
                    num_str, _, text = line.partition("|")
                    try:
                        out_map[int(num_str.strip())] = text.strip()
                    except ValueError:
                        pass
            for j, orig in enumerate(chunk):
                translated.append(out_map.get(j + 1, orig))
        except Exception:
            # Bu batch basarisiz olursa orijinali kullan
            translated.extend(chunk)

    return [(s, e, tr) for (s, e, _), tr in zip(segments, translated)]


# ---------------------------------------------------------------------------
# TTS - edge-tts async + concurrent
# ---------------------------------------------------------------------------

async def _tts_all(segments: list, voice: str, workdir: str) -> None:
    sem = asyncio.Semaphore(8)

    async def one(idx: int, text: str) -> None:
        async with sem:
            try:
                comm = edge_tts.Communicate(text, voice)
                await comm.save(os.path.join(workdir, f"seg_{idx}.mp3"))
            except Exception:
                pass

    await asyncio.gather(
        *(one(i, t) for i, (_, _, t) in enumerate(segments) if t.strip())
    )


# ---------------------------------------------------------------------------
# Hiz ayarlama (TTS pencereye sigmiyorsa)
# ---------------------------------------------------------------------------

def _speed_adjust(mp3: str, window_sec: float, workdir: str, idx: int) -> str:
    dur = _audio_duration(mp3)
    if dur is None or dur <= window_sec * 1.05:
        return mp3
    speed = min(1.6, dur / window_sec)
    out = os.path.join(workdir, f"seg_{idx}_fast.mp3")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", mp3, "-filter:a", f"atempo={speed:.3f}", out],
        capture_output=True,
    )
    return out if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0 else mp3


# ---------------------------------------------------------------------------
# Dub track insa - ffmpeg concat (RAM dostu, herhangi bir sure destekler)
# ---------------------------------------------------------------------------

def _build_dub_track(valid: list[tuple[float, str]], workdir: str, out_wav: str) -> None:
    pieces: list[str] = []
    cursor = 0.0

    for start_sec, mp3_path in valid:
        gap = start_sec - cursor
        if gap > 0.05:
            sil = os.path.join(workdir, f"sil_{len(pieces)}.wav")
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{gap:.3f}",
                "-c:a", "pcm_s16le", sil,
            ], capture_output=True, check=True)
            pieces.append(sil)

        seg_wav = os.path.join(workdir, f"seg_{len(pieces)}_out.wav")
        r = subprocess.run([
            "ffmpeg", "-y", "-i", mp3_path,
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
            seg_wav,
        ], capture_output=True)
        if r.returncode == 0 and os.path.exists(seg_wav) and os.path.getsize(seg_wav) > 0:
            pieces.append(seg_wav)
            cursor = start_sec + (_audio_duration(seg_wav) or 0.0)
        else:
            cursor = start_sec

    if not pieces:
        raise RuntimeError("TTS segmentleri oluşturulamadı")

    list_file = os.path.join(workdir, "pieces.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")

    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_wav,
    ], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Ana fonksiyon
# ---------------------------------------------------------------------------

def dub_video(video_path: str, out_path: str, keep_original_volume: float = 0.15,
              src_lang: str = "auto", tgt_lang: str = "tr", gender: str = "female",
              on_progress: Callable[[float, str], None] | None = None) -> None:
    """
    Video dublaj - engine secimi otomatik:
      COLAB_URL           -> Colab GPU Whisper (en hizli, en iyi kalite)
      GROQ_API_KEY        -> Groq Whisper (hizli, kisa videolar icin iyi)
      OPENROUTER_API_KEY  -> OpenRouter LLM ceviri (kaliteli)
    """
    lang_voices = VOICE_MAP.get(tgt_lang, {"female": "tr-TR-EmelNeural"})
    voice = lang_voices.get(gender) or lang_voices.get("female") or next(iter(lang_voices.values()))
    use_colab = bool(os.environ.get("COLAB_URL"))
    use_groq = bool(os.environ.get("GROQ_API_KEY"))
    use_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "src.wav")
        if on_progress:
            on_progress(8, "Ses cikariliyor")
        _extract_audio(video_path, wav)

        # --- Transkripsiyon (oncelik: Colab > Groq > Local) ---
        if on_progress:
            on_progress(22, "Transkripsiyon yapiliyor")
        if use_colab:
            segments, detected_lang = _transcribe_colab(wav, src_lang)
        elif use_groq:
            segments, detected_lang = _transcribe_groq(wav, src_lang)
        else:
            segments, detected_lang = _transcribe_local(wav)

        if not segments:
            raise RuntimeError("Konuşma tespit edilemedi")

        source = detected_lang if src_lang == "auto" else src_lang

        # --- Ceviri ---
        if source != tgt_lang:
            if on_progress:
                on_progress(48, "Ceviri yapiliyor")
            if use_openrouter:
                segments = _translate_openrouter(segments, source, tgt_lang)
            else:
                segments = _translate_google(segments, source, tgt_lang)

        # --- TTS ---
        if on_progress:
            on_progress(68, "Seslendirme uretiliyor")
        asyncio.run(_tts_all(segments, voice, tmp))

        # --- Hiz ayarla + gecerli segmentleri topla ---
        if on_progress:
            on_progress(82, "Parcalar hazirlaniyor")
        valid: list[tuple[float, str]] = []
        for idx, (start, end, _) in enumerate(segments):
            mp3 = os.path.join(tmp, f"seg_{idx}.mp3")
            if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
                continue
            mp3 = _speed_adjust(mp3, max(0.5, end - start), tmp, idx)
            valid.append((start, mp3))

        if not valid:
            raise RuntimeError("Geçerli TTS segmenti bulunamadı")

        valid.sort(key=lambda x: x[0])

        dub_wav = os.path.join(tmp, "dub.wav")
        if on_progress:
            on_progress(90, "Ses parcasi birlestiriliyor")
        _build_dub_track(valid, tmp, dub_wav)

        # --- Final mix ---
        if os.environ.get("LIPSYNC_URL"):
            if on_progress:
                on_progress(94, "Lip-sync Colab'a gonderiliyor")
            from lipsync_client import lipsync_video

            lipsync_video(video_path, dub_wav, out_path, on_progress=on_progress)
        else:
            if on_progress:
                on_progress(96, "Video final hale getiriliyor")
            subprocess.run([
                "ffmpeg", "-y",
                "-i", video_path,
                "-i", dub_wav,
                "-filter_complex",
                f"[0:a]volume={keep_original_volume}[a0];[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]",
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-shortest",
                out_path,
            ], check=True, capture_output=True)
        if on_progress:
            on_progress(100, "Dublaj tamamlandi")
