"""
lipsync_client.py - Colab Wav2Lip endpoint'ine video+ses gonderir,
lip-sync'li video geri alir.

LIPSYNC_URL env degiskeni set ise dub.py bu modulu kullanir.
"""

import os
from collections.abc import Callable

import requests


def lipsync_video(
    video_path: str,
    audio_path: str,
    out_path: str,
    on_progress: Callable[[float, str], None] | None = None,
) -> None:
    url = os.environ["LIPSYNC_URL"].rstrip("/")

    if on_progress:
        on_progress(92, "Lip-sync icin Colab'a gonderiliyor")

    with open(video_path, "rb") as vf, open(audio_path, "rb") as af:
        files = {
            "video": (os.path.basename(video_path), vf, "video/mp4"),
            "audio": (os.path.basename(audio_path), af, "audio/wav"),
        }
        resp = requests.post(f"{url}/lipsync", files=files, timeout=3600, stream=True)

    if resp.status_code != 200:
        raise RuntimeError(f"Lip-sync API hatasi {resp.status_code}: {resp.text[:200]}")

    if on_progress:
        on_progress(98, "Lip-sync videosu indiriliyor")

    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
