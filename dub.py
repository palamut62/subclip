"""
dub.py - video dublaj motoru (ElevenLabs TTS)

Transkripsiyon:
  GROQ_API_KEY       -> Groq Whisper API (hizli)
  Hicbiri yoksa      -> local Whisper CPU (yavas)

Ceviri:
  OPENROUTER_API_KEY -> OpenRouter LLM batch
  Hicbiri yoksa      -> Google Translate

TTS: ElevenLabs (gercek/klonlanmis insan sesi).
"""

import os
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from deep_translator import GoogleTranslator
from faster_whisper import WhisperModel

import elevenlabs_client as eleven

_MODEL = None
_GROQ_CHUNK_MIN = 20       # Groq'a gonderilecek chunk suresi (dakika)
_TRANSLATE_BATCH = 25      # tek LLM / Google Translate cagrisindaki cumle sayisi
_TTS_CONCURRENCY = 2       # ElevenLabs paralel istek limiti (dusuk = daha az rate limit riski)
_MAX_TTS_SPEED = 1.9
_MIN_TTS_SPEED = 0.85
_TTS_WARN_LIMIT = 5
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    if _NO_WINDOW and "creationflags" not in kwargs:
        kwargs["creationflags"] = _NO_WINDOW
    return subprocess.run(cmd, **kwargs)


@dataclass
class TtsReport:
    ok: int = 0
    failed: int = 0
    failures: list[str] | None = None

    def note_failure(self, idx: int, text: str, exc: Exception) -> None:
        self.failed += 1
        if self.failures is None:
            self.failures = []
        if len(self.failures) < _TTS_WARN_LIMIT:
            preview = " ".join((text or "").split())[:80]
            self.failures.append(f"#{idx + 1}: {preview} ({exc})")

    def summary(self) -> str:
        if not self.failed:
            return ""
        examples = "; ".join(self.failures or [])
        extra = f" Examples: {examples}" if examples else ""
        return f"{self.failed} TTS segments could not be generated.{extra}"

_LANG_NAMES = {
    "tr": "Turkish", "en": "English", "de": "German", "es": "Spanish",
    "fr": "French",  "it": "Italian", "ru": "Russian", "ar": "Arabic",
    "ja": "Japanese", "zh": "Chinese", "pt": "Portuguese",
}


# ---------------------------------------------------------------------------
# Segment birlestirme - kisa boslukla ayrilmis parcalari tek TTS'e topla
# ---------------------------------------------------------------------------

_MERGE_MAX_GAP = 0.7        # iki segment arasi bu kadar veya daha az ise birlestir
_MERGE_MAX_DUR = 14.0       # birlesik segment bu kadar saniyeyi gecmesin
_MERGE_MAX_CHARS = 400      # ElevenLabs icin makul karakter limiti


def _merge_close_segments(segments: list) -> list:
    """[(start, end, text), ...] -> ardarda gelen yakin segmentleri birlestir.
    Whisper cumleyi orta yerinden boldugunde olusan kesik kesik dublajin coz."""
    if not segments:
        return segments
    out: list = []
    cs, ce, ct = segments[0]
    for s, e, t in segments[1:]:
        gap = s - ce
        merged_dur = e - cs
        merged_chars = len(ct) + 1 + len(t)
        if gap <= _MERGE_MAX_GAP and merged_dur <= _MERGE_MAX_DUR and merged_chars <= _MERGE_MAX_CHARS:
            ct = (ct.rstrip() + " " + t.lstrip()).strip()
            ce = e
        else:
            out.append((cs, ce, ct))
            cs, ce, ct = s, e, t
    out.append((cs, ce, ct))
    return out


def _merge_close_segments_dict(segments: list) -> list:
    """Diarized segment dict listesi icin (speaker degisirse birlestirme)."""
    if not segments:
        return segments
    out: list = []
    cur = dict(segments[0])
    for s in segments[1:]:
        gap = s["start"] - cur["end"]
        merged_dur = s["end"] - cur["start"]
        merged_chars = len(cur["text"]) + 1 + len(s["text"])
        if (s.get("speaker") == cur.get("speaker")
                and gap <= _MERGE_MAX_GAP
                and merged_dur <= _MERGE_MAX_DUR
                and merged_chars <= _MERGE_MAX_CHARS):
            cur["text"] = (cur["text"].rstrip() + " " + s["text"].lstrip()).strip()
            cur["end"] = s["end"]
        else:
            out.append(cur)
            cur = dict(s)
    out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Yardimci - ffprobe ile sure al
# ---------------------------------------------------------------------------

def _audio_duration(path: str) -> float | None:
    try:
        r = _run(
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
    _run(
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

def _transcribe_elevenlabs_scribe(wav_path: str, src_lang: str, diarize: bool = True) -> tuple[list, str]:
    """ElevenLabs Scribe ile transkripsiyon + (opsiyonel) diarization.
    diarize=True ise segment dict'leri konusmaci ID'si ile doner."""
    import requests

    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set (required for Scribe).")

    mp3_path = wav_path.replace(".wav", "_scribe.mp3")
    _run([
        "ffmpeg", "-y", "-i", wav_path,
        "-ac", "1", "-ar", "16000", "-b:a", "64k", mp3_path,
    ], check=True, capture_output=True)

    try:
        with open(mp3_path, "rb") as f:
            data = {"model_id": "scribe_v1", "diarize": "true" if diarize else "false"}
            if src_lang and src_lang != "auto":
                data["language_code"] = src_lang
            resp = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": api_key},
                files={"file": ("audio.mp3", f, "audio/mpeg")},
                data=data, timeout=1800,
            )
        if resp.status_code != 200:
            raise RuntimeError(f"Scribe error {resp.status_code}: {resp.text[:300]}")
        payload = resp.json()
        lang = (payload.get("language_code") or "en").lower()[:2]

        # Word-level -> segment'lere topla (konusmaci ya da uzun susma sinirinda boel).
        words = payload.get("words") or []
        segments: list = []
        cur_words: list[str] = []
        cur_start = None
        cur_end = None
        cur_spk = None
        SILENCE_BREAK = 0.8

        def flush() -> None:
            if cur_words and cur_start is not None and cur_end is not None:
                text = " ".join(cur_words).strip()
                if text:
                    if diarize:
                        segments.append({"start": cur_start, "end": cur_end, "text": text,
                                         "speaker": cur_spk or "SPK0"})
                    else:
                        segments.append((cur_start, cur_end, text))

        for w in words:
            if w.get("type") not in (None, "word"):
                continue
            text = (w.get("text") or "").strip()
            if not text:
                continue
            s = float(w.get("start") or 0.0)
            e = float(w.get("end") or s)
            spk = w.get("speaker_id") or "SPK0"
            if cur_start is None:
                cur_start, cur_end, cur_spk = s, e, spk
                cur_words = [text]
                continue
            same_spk = (spk == cur_spk)
            gap = s - cur_end
            if not same_spk or gap > SILENCE_BREAK:
                flush()
                cur_words = [text]; cur_start, cur_end, cur_spk = s, e, spk
            else:
                cur_words.append(text); cur_end = e
        flush()
        return segments, lang
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
        while offset < total - 0.05:
            dur = min(chunk_sec, total - offset)
            # FLAC: kayipsiz, MP3'teki encoder delay padding'i yok -> chunk
            # sinirinda timestamp drift birikmiyor. 16 kHz mono ~= 8-12 MB / 20 dk.
            chunk_path = os.path.join(tmp, f"chunk_{idx}.flac")

            _run([
                "ffmpeg", "-y", "-i", wav_path,
                "-ss", f"{offset:.3f}", "-t", f"{dur:.3f}",
                "-ac", "1", "-ar", "16000",
                "-c:a", "flac", "-compression_level", "8",
                chunk_path,
            ], check=True, capture_output=True)

            extra: dict = {}
            if src_lang != "auto":
                extra["language"] = src_lang

            with open(chunk_path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model="whisper-large-v3-turbo",
                    file=f,
                    response_format="verbose_json",
                    **extra,
                )

            if getattr(resp, "language", None):
                detected_lang = resp.language

            for seg in getattr(resp, "segments", None) or []:
                if isinstance(seg, dict):
                    s = seg.get("start", 0)
                    e = seg.get("end", 0)
                    t = (seg.get("text") or "").strip()
                else:
                    s = getattr(seg, "start", 0) or 0
                    e = getattr(seg, "end", 0) or 0
                    t = (getattr(seg, "text", "") or "").strip()
                if t:
                    all_segments.append((s + offset, e + offset, t))

            # Nominal chunk_sec yerine gercek dur ile ilerle: son parca kisaysa
            # ofset bir sonraki dongude tutarsizlasmaz.
            offset += dur
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
            result = tr.translate_batch(chunk) or []
            # Batch uzunluk uyumsuzlugu -> zip sessizce hizalamayi bozardi;
            # bu durumda parca parca ceviri yap.
            if len(result) != len(chunk):
                result = []
                for t in chunk:
                    try:
                        result.append(tr.translate(t) or t)
                    except Exception:
                        result.append(t)
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

    def _call(chunk: list[str]) -> dict[int, str]:
        numbered = "\n".join(f"{j + 1}|{t}" for j, t in enumerate(chunk))
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
        )
        out_map: dict[int, str] = {}
        content = (resp.choices[0].message.content or "").strip()
        for line in content.split("\n"):
            if "|" in line:
                num_str, _, text = line.partition("|")
                try:
                    out_map[int(num_str.strip())] = text.strip()
                except ValueError:
                    pass
        return out_map

    def _translate_chunk(chunk: list[str]) -> list[str]:
        """LLM cagrisi + eksik satirlari recursive olarak bolerek tamamla."""
        if not chunk:
            return []
        try:
            out_map = _call(chunk)
        except Exception:
            out_map = {}
        # Eksikleri topla
        missing_idx = [j for j in range(len(chunk)) if not out_map.get(j + 1)]
        if not missing_idx:
            return [out_map[j + 1] for j in range(len(chunk))]
        # Tum batch bostan dondu veya buyuk eksik: ikiye bol
        if len(chunk) > 1 and len(missing_idx) > len(chunk) // 2:
            mid = len(chunk) // 2
            left = _translate_chunk(chunk[:mid])
            right = _translate_chunk(chunk[mid:])
            return left + right
        # Az sayida eksik: tek tek tekrar dene
        result = [out_map.get(j + 1, "") for j in range(len(chunk))]
        for j in missing_idx:
            try:
                single = _call([chunk[j]])
                result[j] = single.get(1) or chunk[j]
            except Exception:
                result[j] = chunk[j]
        return result

    translated: list[str] = []
    for i in range(0, len(texts), _TRANSLATE_BATCH):
        translated.extend(_translate_chunk(texts[i:i + _TRANSLATE_BATCH]))

    return [(s, e, tr) for (s, e, _), tr in zip(segments, translated)]


# ---------------------------------------------------------------------------
# TTS - ElevenLabs (thread pool, paralel REST)
# ---------------------------------------------------------------------------

def _tts_all(segments: list, voice_id: str, workdir: str) -> TtsReport:
    """segments: [(start, end, text), ...] -> tek voice_id ile sentez."""
    report = TtsReport(failures=[])

    def one(item: tuple[int, str]) -> None:
        idx, text = item
        try:
            eleven.tts(text, voice_id, os.path.join(workdir, f"seg_{idx}.mp3"))
            report.ok += 1
        except Exception as exc:
            report.note_failure(idx, text, exc)

    jobs = [(i, t) for i, (_, _, t) in enumerate(segments) if t.strip()]
    with ThreadPoolExecutor(max_workers=_TTS_CONCURRENCY) as ex:
        list(ex.map(one, jobs))
    return report


def _tts_all_multi(segments_with_voice: list, workdir: str) -> TtsReport:
    """segments_with_voice: [(start, end, text, voice_id), ...]"""
    report = TtsReport(failures=[])

    def one(item: tuple[int, str, str]) -> None:
        idx, text, voice_id = item
        try:
            eleven.tts(text, voice_id, os.path.join(workdir, f"seg_{idx}.mp3"))
            report.ok += 1
        except Exception as exc:
            report.note_failure(idx, text, exc)

    jobs = [(i, t, v) for i, (_, _, t, v) in enumerate(segments_with_voice) if t.strip()]
    with ThreadPoolExecutor(max_workers=_TTS_CONCURRENCY) as ex:
        list(ex.map(one, jobs))
    return report


# ---------------------------------------------------------------------------
# Hiz ayarlama (TTS pencereye sigmiyorsa)
# ---------------------------------------------------------------------------

def _trim_silence(mp3: str, workdir: str, idx: int) -> str:
    """ElevenLabs TTS cikislarindaki ~150-300ms leading silence'i kes.
    Sadece bastan kesiyoruz; sonu birakiyoruz ki segment sonu klips/clip olmasin.
    Cok kucuk bir fade-in ile concat sinirinda klik onleniyor."""
    out = os.path.join(workdir, f"seg_{idx}_trim.mp3")
    r = _run([
        "ffmpeg", "-y", "-i", mp3,
        "-af",
        "silenceremove=start_periods=1:start_silence=0.1:start_threshold=-50dB:"
        "detection=peak,"
        "afade=t=in:st=0:d=0.015",
        out,
    ], capture_output=True)
    if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
        return out
    return mp3


def _speed_adjust(mp3: str, window_sec: float, workdir: str, idx: int,
                  hard_cap_sec: float | None = None) -> str:
    """Sadece TTS parcasi pencereye sigmiyorsa hizlandir. Yavaslatma yok —
    yavaslatma drift'e yol aciyordu. TTS doga hizinda kalirsa orijinal
    ritmiyle uyumlu duyuluyor (hem trans hem TTS ayni dilsel onset'lere sahip)."""
    dur = _audio_duration(mp3)
    if dur is None or dur <= 0 or window_sec <= 0:
        return mp3
    # Pencereye sigiyorsa dokunma — dogal hizda kalsin
    if dur <= window_sec * 1.05:
        return mp3
    target = hard_cap_sec if (hard_cap_sec and hard_cap_sec > 0) else window_sec
    speed = min(_MAX_TTS_SPEED, max(1.0, dur / target))
    if abs(speed - 1.0) < 0.03:
        return mp3
    out = os.path.join(workdir, f"seg_{idx}_fit.mp3")
    r = _run(
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
        if gap < -0.2:
            start_sec = cursor
            gap = 0.0
        if gap > 0.05:
            sil = os.path.join(workdir, f"sil_{len(pieces)}.wav")
            _run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", "anullsrc=r=44100:cl=stereo",
                "-t", f"{gap:.3f}",
                "-c:a", "pcm_s16le", sil,
            ], capture_output=True, check=True)
            pieces.append(sil)

        seg_wav = os.path.join(workdir, f"seg_{len(pieces)}_out.wav")
        r = _run([
            "ffmpeg", "-y", "-i", mp3_path,
            "-af", "afade=t=in:st=0:d=0.015,areverse,afade=t=in:st=0:d=0.015,areverse",
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
            seg_wav,
        ], capture_output=True)
        if r.returncode == 0 and os.path.exists(seg_wav) and os.path.getsize(seg_wav) > 0:
            pieces.append(seg_wav)
            cursor = start_sec + (_audio_duration(seg_wav) or 0.0)
        else:
            cursor = start_sec

    if not pieces:
        raise RuntimeError("Failed to build TTS segments")

    list_file = os.path.join(workdir, "pieces.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in pieces:
            f.write(f"file '{p.replace(chr(92), '/')}'\n")

    _run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", out_wav,
    ], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Ana fonksiyon
# ---------------------------------------------------------------------------

def dub_video(video_path: str, out_path: str, keep_original_volume: float = 0.15,
              src_lang: str = "auto", tgt_lang: str = "tr", gender: str = "female",
              voice_mode: str = "preset",
              on_progress: Callable[[float, str], None] | None = None) -> dict:
    """
    ElevenLabs ile dublaj.
      voice_mode="preset" -> cinsiyete gore varsayilan ElevenLabs voice
      voice_mode="clone"  -> orijinal sesi klonla, dublaj sonunda sil

    Returns dict with: {segments, source_lang, tgt_lang}
    """
    use_groq = bool(os.environ.get("GROQ_API_KEY"))
    use_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

    def final_mix() -> None:
        if on_progress:
            on_progress(96, "Finalizing video")
        _run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", dub_wav,
            "-filter_complex",
            f"[0:a]volume={keep_original_volume}[a0];[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path,
        ], check=True, capture_output=True)

    with tempfile.TemporaryDirectory() as tmp:
        wav = os.path.join(tmp, "src.wav")
        if on_progress:
            on_progress(8, "Extracting audio")
        _extract_audio(video_path, wav)

        # --- Transcription (Groq > Local) ---
        if on_progress:
            on_progress(22, "Transcribing")
        if use_groq:
            segments, detected_lang = _transcribe_groq(wav, src_lang)
        else:
            segments, detected_lang = _transcribe_local(wav)

        if not segments:
            raise RuntimeError("No speech detected")

        # Whisper kucuk parcalara bolmus olabilir → bitisik segmentleri birlestir
        # ki TTS surekli/dogal konussun, kesik kesik olmasin.
        segments = _merge_close_segments(segments)

        source = detected_lang if src_lang == "auto" else src_lang

        # --- Ceviri ---
        if source != tgt_lang:
            if on_progress:
                on_progress(48, "Translating")
            if use_openrouter:
                segments = _translate_openrouter(segments, source, tgt_lang)
            else:
                segments = _translate_google(segments, source, tgt_lang)

        # --- Voice (clone veya preset) ---
        cloned_voice_id: str | None = None
        if voice_mode == "clone":
            if on_progress:
                on_progress(62, "Cloning speaker voice")
            sample = os.path.join(tmp, "voice_sample.wav")
            ranges = [(s, e) for s, e, _ in segments]
            if eleven.extract_speaker_sample(wav, ranges, sample):
                try:
                    cloned_voice_id = eleven.clone_voice(sample, f"reclip_{os.path.basename(out_path)}")
                except Exception as exc:
                    if on_progress:
                        on_progress(63, f"Clone failed, falling back to preset voice: {exc}")
        voice_id = cloned_voice_id or eleven.preset_voice_id(gender)

        # --- TTS ---
        if on_progress:
            on_progress(68, "Generating speech (ElevenLabs)")
        try:
            tts_report = _tts_all(segments, voice_id, tmp)
        finally:
            if cloned_voice_id:
                eleven.delete_voice(cloned_voice_id)
        if tts_report.failed and on_progress:
            on_progress(76, tts_report.summary())

        # --- Hiz ayarla + gecerli segmentleri topla ---
        if on_progress:
            on_progress(82, "Preparing segments")
        valid: list[tuple[float, str]] = []
        for idx, (start, end, _) in enumerate(segments):
            mp3 = os.path.join(tmp, f"seg_{idx}.mp3")
            if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
                continue
            next_start = segments[idx + 1][0] if idx + 1 < len(segments) else start + (end - start) * 2
            # Pencere = orijinal cumle + sonraki sessizlik. Sessizligi trimlemiyoruz;
            # ElevenLabs'in dogal onset'i Whisper start ile hizali.
            window = max(0.6, next_start - start)
            mp3 = _speed_adjust(mp3, window, tmp, idx, hard_cap_sec=window)
            valid.append((start, mp3))

        if not valid:
            detail = tts_report.summary()
            raise RuntimeError(f"No valid TTS segments found. {detail}".strip())

        valid.sort(key=lambda x: x[0])

        dub_wav = os.path.join(tmp, "dub.wav")
        if on_progress:
            on_progress(90, "Merging audio segments")
        _build_dub_track(valid, tmp, dub_wav)

        # --- Final mix ---
        if os.environ.get("SYNC_API_KEY"):
            if on_progress:
                on_progress(94, "Sending to Sync.so for lipsync")
            from lipsync_client import lipsync_video

            try:
                lipsync_video(video_path, dub_wav, out_path, on_progress=on_progress)
            except Exception as exc:
                if on_progress:
                    on_progress(
                        95,
                        f"Lipsync unavailable, continuing with normal dub: {exc}",
                    )
                final_mix()
        else:
            final_mix()
        if on_progress:
            on_progress(100, "Dub complete")
        return {
            "segments": segments,
            "source_lang": source,
            "tgt_lang": tgt_lang,
        }


# ---------------------------------------------------------------------------
# Multi-speaker: analiz + finalize
# ---------------------------------------------------------------------------

def _guess_gender_from_pitch(f0_hz: float | None) -> str:
    if not f0_hz or f0_hz <= 0:
        return "female"
    if f0_hz < 160:
        return "male"
    if f0_hz > 255:
        return "child"
    return "female"


def _extract_speaker_audio(wav_path: str, segments: list, workdir: str) -> dict[str, str]:
    """Her speaker icin o speaker'a ait tum segmentlerden tek bir wav birlestir (pitch icin)."""
    by_spk: dict[str, list] = {}
    for s in segments:
        by_spk.setdefault(s["speaker"], []).append((s["start"], s["end"]))

    paths: dict[str, str] = {}
    for spk, ranges in by_spk.items():
        ranges.sort()
        select = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ranges)
        out = os.path.join(workdir, f"spk_{spk}.wav")
        _run(
            ["ffmpeg", "-y", "-i", wav_path, "-af", f"aselect='{select}',asetpts=N/SR/TB",
             "-ac", "1", "-ar", "16000", out],
            capture_output=True,
        )
        if os.path.exists(out) and os.path.getsize(out) > 1000:
            paths[spk] = out
    return paths


def _mean_pitch(wav_path: str) -> float | None:
    try:
        import librosa
        import numpy as np

        y, sr = librosa.load(wav_path, sr=16000, mono=True)
        if len(y) < sr // 4:
            return None
        f0, voiced, _ = librosa.pyin(y, fmin=60, fmax=500, sr=sr)
        vals = f0[voiced] if voiced is not None else f0
        vals = vals[~np.isnan(vals)] if hasattr(vals, "size") else []
        if len(vals) < 10:
            return None
        return float(np.median(vals))
    except Exception:
        return None


def analyze_speakers(video_path: str, workdir: str, src_lang: str = "auto",
                     on_progress: Callable[[float, str], None] | None = None) -> dict:
    """Videoyu transcribe + diarize eder, her speaker icin cinsiyet tahmini yapar.
    Returns: {segments, source_lang, speakers: [{id, segment_count, total_seconds, pitch_hz, gender_guess}]}
    """
    if not os.environ.get("ELEVENLABS_API_KEY"):
        raise RuntimeError("Multi-speaker mode requires ELEVENLABS_API_KEY (Scribe diarization).")

    wav = os.path.join(workdir, "analyze.wav")
    if on_progress:
        on_progress(8, "Extracting audio")
    _extract_audio(video_path, wav)

    if on_progress:
        on_progress(25, "Transcribe + diarize (ElevenLabs Scribe)")
    segments, detected = _transcribe_elevenlabs_scribe(wav, src_lang, diarize=True)
    if not segments or not isinstance(segments[0], dict):
        raise RuntimeError("Scribe diarization returned no results.")

    # Ayni konusmacinin ardarda kisa parcalarini birlestir → kesik kesik bitsin
    segments = _merge_close_segments_dict(segments)

    source = detected if src_lang == "auto" else src_lang

    if on_progress:
        on_progress(70, "Estimating speaker gender")
    spk_audios = _extract_speaker_audio(wav, segments, workdir)

    by_spk: dict[str, list] = {}
    for s in segments:
        by_spk.setdefault(s["speaker"], []).append(s)

    speakers = []
    for spk_id, segs in sorted(by_spk.items()):
        total = sum(s["end"] - s["start"] for s in segs)
        pitch = _mean_pitch(spk_audios[spk_id]) if spk_id in spk_audios else None
        speakers.append({
            "id": spk_id,
            "segment_count": len(segs),
            "total_seconds": round(total, 1),
            "pitch_hz": round(pitch, 1) if pitch else None,
            "gender_guess": _guess_gender_from_pitch(pitch),
        })

    if on_progress:
        on_progress(90, "Analysis complete")
    return {
        "segments": segments,
        "source_lang": source,
        "speakers": speakers,
        "audio_path": wav,
    }


def finalize_multi_voice(video_path: str, out_path: str, analysis: dict,
                         voices_map: dict[str, str], tgt_lang: str = "tr",
                         keep_original_volume: float = 0.15,
                         voice_mode: str = "preset",
                         on_progress: Callable[[float, str], None] | None = None) -> None:
    """analysis['segments'] + voices_map (speaker_id -> gender str) -> final dublaj.
    voice_mode='clone' ise her konusmacinin sesini ayri ayri klonlar."""
    segments = analysis["segments"]
    source = analysis["source_lang"]
    src_wav = analysis.get("audio_path")
    use_openrouter = bool(os.environ.get("OPENROUTER_API_KEY"))

    # speaker -> voice_id (clone veya preset)
    cloned_ids: list[str] = []
    speaker_voice: dict[str, str] = {}

    def resolve_voices(workdir: str) -> None:
        if voice_mode == "clone" and src_wav and os.path.exists(src_wav):
            by_spk: dict[str, list] = {}
            for s in segments:
                by_spk.setdefault(s["speaker"], []).append((s["start"], s["end"]))
            for spk, ranges in by_spk.items():
                sample = os.path.join(workdir, f"clone_{spk}.wav")
                if eleven.extract_speaker_sample(src_wav, ranges, sample):
                    try:
                        vid = eleven.clone_voice(sample, f"reclip_{spk}")
                        speaker_voice[spk] = vid
                        cloned_ids.append(vid)
                        continue
                    except Exception:
                        pass
                speaker_voice[spk] = eleven.preset_voice_id(voices_map.get(spk, "female"))
        else:
            for s in segments:
                spk = s["speaker"]
                if spk not in speaker_voice:
                    speaker_voice[spk] = eleven.preset_voice_id(voices_map.get(spk, "female"))

    def voice_for(spk: str) -> str:
        return speaker_voice.get(spk) or eleven.preset_voice_id("female")

    # Dict segmentleri tuple'a cevir (ceviri icin)
    seg_tuples = [(s["start"], s["end"], s["text"]) for s in segments]

    if source != tgt_lang:
        if on_progress:
            on_progress(35, "Translating")
        if use_openrouter:
            seg_tuples = _translate_openrouter(seg_tuples, source, tgt_lang)
        else:
            seg_tuples = _translate_google(seg_tuples, source, tgt_lang)

    with tempfile.TemporaryDirectory() as tmp:
        if on_progress:
            on_progress(50, "Preparing voices" + (" (cloning)" if voice_mode == "clone" else ""))
        resolve_voices(tmp)

        # Her segmente voice ekle (speaker bilgisi orijinal segments'te)
        seg_with_voice = [
            (s["start"], s["end"], t_tr, voice_for(s["speaker"]))
            for s, (_, _, t_tr) in zip(segments, seg_tuples)
        ]

        if on_progress:
            on_progress(55, "Multi-voice TTS (ElevenLabs)")
        try:
            tts_report = _tts_all_multi(seg_with_voice, tmp)
        finally:
            for vid in cloned_ids:
                eleven.delete_voice(vid)
        if tts_report.failed and on_progress:
            on_progress(70, tts_report.summary())

        if on_progress:
            on_progress(80, "Preparing segments")
        valid: list[tuple[float, str]] = []
        for idx, (start, end, _, _) in enumerate(seg_with_voice):
            mp3 = os.path.join(tmp, f"seg_{idx}.mp3")
            if not os.path.exists(mp3) or os.path.getsize(mp3) == 0:
                continue
            next_start = seg_with_voice[idx + 1][0] if idx + 1 < len(seg_with_voice) else start + (end - start) * 2
            # Pencere = orijinal cumle + sonraki sessizlik. Sessizligi trimlemiyoruz;
            # ElevenLabs'in dogal onset'i Whisper start ile hizali.
            window = max(0.6, next_start - start)
            mp3 = _speed_adjust(mp3, window, tmp, idx, hard_cap_sec=window)
            valid.append((start, mp3))
        if not valid:
            detail = tts_report.summary()
            raise RuntimeError(f"No valid TTS segments found. {detail}".strip())
        valid.sort(key=lambda x: x[0])

        dub_wav = os.path.join(tmp, "dub.wav")
        if on_progress:
            on_progress(90, "Merging audio segments")
        _build_dub_track(valid, tmp, dub_wav)

        if on_progress:
            on_progress(96, "Finalizing video")
        _run([
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", dub_wav,
            "-filter_complex",
            f"[0:a]volume={keep_original_volume}[a0];[a0][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path,
        ], check=True, capture_output=True)
        if on_progress:
            on_progress(100, "Dub complete")
