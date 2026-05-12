"""
lipsync_client.py - Sync.so API ile dudak senkronu.

SYNC_API_KEY env degiskeni set ise dub.py bu modulu kullanir.
Ucretli servis: https://sync.so
"""

import os
import time
from collections.abc import Callable

import requests

_API = "https://api.sync.so/v2"


def _api_key() -> str:
    key = os.environ.get("SYNC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("SYNC_API_KEY is not set.")
    return key


def _upload_to_tmp(path: str) -> str:
    """Sync.so URL bekledigi icin dosyayi gecici bir host'a yuklemek gerekir.
    Pratik cozum: file.io (gecici, 1 indirme sonra silinir) veya catbox.moe.
    Burada catbox.moe kullaniyoruz - kalici, basit, key gerektirmez."""
    with open(path, "rb") as f:
        r = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": (os.path.basename(path), f)},
            timeout=600,
        )
    r.raise_for_status()
    url = r.text.strip()
    if not url.startswith("http"):
        raise RuntimeError(f"Catbox upload failed: {url[:200]}")
    return url


def lipsync_video(video_path: str, audio_path: str, out_path: str,
                  on_progress: Callable[[float, str], None] | None = None) -> None:
    if on_progress:
        on_progress(94, "Lipsync: uploading files")
    video_url = _upload_to_tmp(video_path)
    audio_url = _upload_to_tmp(audio_path)

    if on_progress:
        on_progress(95, "Lipsync: sending to Sync.so")
    r = requests.post(
        f"{_API}/generate",
        headers={"x-api-key": _api_key(), "Content-Type": "application/json"},
        json={
            "model": "lipsync-2",
            "input": [
                {"type": "video", "url": video_url},
                {"type": "audio", "url": audio_url},
            ],
            "options": {"output_format": "mp4"},
        },
        timeout=120,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Sync.so error {r.status_code}: {r.text[:300]}")
    job_id = r.json().get("id")
    if not job_id:
        raise RuntimeError("Sync.so did not return job_id")

    deadline = time.time() + 1800
    output_url = None
    while time.time() < deadline:
        rs = requests.get(
            f"{_API}/generate/{job_id}",
            headers={"x-api-key": _api_key()},
            timeout=30,
        )
        if rs.status_code != 200:
            time.sleep(4); continue
        data = rs.json()
        status = (data.get("status") or "").lower()
        if status in ("completed", "complete", "succeeded"):
            output_url = data.get("outputUrl") or data.get("output_url")
            break
        if status in ("failed", "error", "canceled"):
            raise RuntimeError(f"Sync.so failed: {data.get('errorMessage') or data}")
        if on_progress:
            on_progress(96, f"Lipsync: {status or 'waiting'}")
        time.sleep(5)

    if not output_url:
        raise RuntimeError("Sync.so timed out")

    if on_progress:
        on_progress(98, "Lipsync: downloading output")
    with requests.get(output_url, stream=True, timeout=600) as rd:
        rd.raise_for_status()
        with open(out_path, "wb") as f:
            for chunk in rd.iter_content(chunk_size=1 << 16):
                if chunk:
                    f.write(chunk)
