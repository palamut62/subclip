"""
elevenlabs_client.py - ElevenLabs TTS + Instant Voice Clone

Iki mod:
  - "preset": cinsiyete gore onceden tanimli ElevenLabs voice_id kullan
  - "clone":  konusmacinin orijinal sesinden ornek alip Instant Voice Clone ile
              voice_id uret, dublaj sonunda voice'u sil (cleanup)

API key: ELEVENLABS_API_KEY env degiskeni.
Model:   eleven_multilingual_v2 (29 dil, klonlama icin onerilen).
"""

import os
import subprocess
import tempfile

import requests

_API = "https://api.elevenlabs.io/v1"
_MODEL = "eleven_multilingual_v2"
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

# Eski "premade" starter pack ID'leri - ucretsiz hesaplarda da calisma
# ihtimali en yuksek olanlar. ElevenLabs library voice'lari free'de 402 doner.
_FALLBACK_VOICES = {
    "female": "21m00Tcm4TlvDq8ikWAM",  # Rachel
    "male":   "pNInz6obpgDQGcFmaJgB",  # Adam
    "child":  "jBpfuIE2acCO8z3wKNLl",  # Gigi
}

_VOICE_CACHE: dict[str, str] = {}


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ELEVENLABS_API_KEY is not set. Add an ElevenLabs API key in Settings."
        )
    return key


def _subprocess_kwargs() -> dict:
    return {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}


def _fetch_account_voices() -> list[dict]:
    try:
        r = requests.get(
            f"{_API}/voices",
            headers={"xi-api-key": _api_key()},
            timeout=30,
        )
        if r.status_code != 200:
            return []
        return r.json().get("voices") or []
    except Exception:
        return []


def _pick_voice_for_gender(voices: list[dict], gender: str) -> str | None:
    """Hesabin voice listesinden cinsiyete uygun, free'de calisacak bir voice sec.
    Oncelik: category 'premade' veya 'generated' (library/professional degil)."""
    target = gender.lower()
    if target == "child":
        target_genders = {"female"}  # cocuk yoksa kadin
    elif target == "male":
        target_genders = {"male"}
    else:
        target_genders = {"female"}

    safe_categories = {"premade", "generated", "cloned"}
    candidates = []
    for v in voices:
        cat = (v.get("category") or "").lower()
        if cat not in safe_categories:
            continue
        labels = v.get("labels") or {}
        g = (labels.get("gender") or "").lower()
        if g in target_genders:
            candidates.append((cat, v))
    # premade > generated > cloned siralamasi
    order = {"premade": 0, "generated": 1, "cloned": 2}
    candidates.sort(key=lambda x: order.get(x[0], 9))
    if candidates:
        return candidates[0][1].get("voice_id")
    # cinsiyet eslesmezse herhangi bir guvenli kategoriden al
    for v in voices:
        cat = (v.get("category") or "").lower()
        if cat in safe_categories:
            return v.get("voice_id")
    return None


def preset_voice_id(gender: str) -> str:
    key = (gender or "female").lower()
    if key in _VOICE_CACHE:
        return _VOICE_CACHE[key]
    voices = _fetch_account_voices()
    vid = _pick_voice_for_gender(voices, key) if voices else None
    if not vid:
        vid = _FALLBACK_VOICES.get(key) or _FALLBACK_VOICES["female"]
    _VOICE_CACHE[key] = vid
    return vid


def extract_speaker_sample(src_wav: str, ranges: list[tuple[float, float]],
                           out_wav: str, max_seconds: float = 60.0) -> bool:
    """Konusmacinin segmentlerinden ornek ses kes (clone icin).
    En uzun bolgeleri secip toplam <= max_seconds tutar."""
    if not ranges:
        return False
    sorted_ranges = sorted(ranges, key=lambda r: r[1] - r[0], reverse=True)
    picked: list[tuple[float, float]] = []
    total = 0.0
    for s, e in sorted_ranges:
        d = max(0.0, e - s)
        if d < 1.0:
            continue
        take = min(d, max_seconds - total)
        if take < 1.0:
            continue
        picked.append((s, s + take))
        total += take
        if total >= max_seconds:
            break
    if not picked:
        return False
    picked.sort()
    select = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in picked)
    cmd = [
        "ffmpeg", "-y", "-i", src_wav,
        "-af", f"aselect='{select}',asetpts=N/SR/TB",
        "-ac", "1", "-ar", "22050",
        out_wav,
    ]
    r = subprocess.run(cmd, capture_output=True, **_subprocess_kwargs())
    return r.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 5000


def clone_voice(sample_wav: str, name: str) -> str:
    """Instant Voice Clone -> voice_id."""
    with open(sample_wav, "rb") as f:
        files = {"files": (os.path.basename(sample_wav), f, "audio/wav")}
        data = {"name": name, "description": "ReClip auto-clone"}
        r = requests.post(
            f"{_API}/voices/add",
            headers={"xi-api-key": _api_key()},
            files=files, data=data, timeout=120,
        )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs clone error {r.status_code}: {r.text[:300]}")
    return r.json()["voice_id"]


def delete_voice(voice_id: str) -> None:
    try:
        requests.delete(
            f"{_API}/voices/{voice_id}",
            headers={"xi-api-key": _api_key()},
            timeout=30,
        )
    except Exception:
        pass


def tts(text: str, voice_id: str, out_path: str) -> None:
    """Single segment TTS -> mp3 file. Retries on rate limit / transient errors."""
    import time

    payload = {
        "text": text,
        "model_id": _MODEL,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.0},
    }
    last_err: str = ""
    for attempt in range(4):
        try:
            r = requests.post(
                f"{_API}/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": _api_key(),
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                },
                json=payload, timeout=180,
            )
        except Exception as exc:
            last_err = f"network: {exc}"
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code == 200:
            with open(out_path, "wb") as f:
                f.write(r.content)
            return
        # Quota / auth → retrying won't help, fail immediately
        if r.status_code in (401, 402, 403):
            raise RuntimeError(f"ElevenLabs TTS error {r.status_code}: {r.text[:300]}")
        # Rate limit / server error → backoff and retry
        last_err = f"{r.status_code}: {r.text[:200]}"
        wait = 2.0 * (attempt + 1)
        if r.status_code == 429:
            try:
                wait = max(wait, float(r.headers.get("retry-after", "0")))
            except ValueError:
                pass
        time.sleep(wait)
    raise RuntimeError(f"ElevenLabs TTS error after retries — {last_err}")
