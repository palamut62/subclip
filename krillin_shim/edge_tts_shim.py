"""
KrillinAI icin edge-tts wrapper.

KrillinAI `edge-tts --text-file F --voice V --output O --format wav --sample_rate N`
benzeri argumanlar gonderiyor. Gercek edge-tts 7.x CLI'da bu flag'lar yok -
Python API ile sentezle, sonra ffmpeg ile istenen formata/samplerate'e cevir.
"""

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile

import edge_tts


def main() -> int:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--text-file", dest="text_file")
    p.add_argument("-f", "--file", dest="file")
    p.add_argument("-t", "--text")
    p.add_argument("-v", "--voice", required=True)
    p.add_argument("--output", "--write-media", dest="output", required=True)
    p.add_argument("--format", default="mp3")
    p.add_argument("--sample_rate", "--sample-rate", dest="sample_rate", type=int, default=24000)
    p.add_argument("--rate", default="+0%")
    p.add_argument("--volume", default="+0%")
    p.add_argument("--pitch", default="+0Hz")
    args, _ = p.parse_known_args()

    text = args.text
    src = args.text_file or args.file
    if src:
        with open(src, encoding="utf-8") as fh:
            text = fh.read()
    if not text:
        print("edge-tts shim: no text provided", file=sys.stderr)
        return 2

    async def synth() -> None:
        comm = edge_tts.Communicate(
            text.strip() or " ",
            args.voice,
            rate=args.rate,
            volume=args.volume,
            pitch=args.pitch,
        )
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            await comm.save(tmp_path)
            fmt = (args.format or "mp3").lower()
            codec_args: list[str]
            if fmt in ("wav", "pcm"):
                codec_args = ["-ar", str(args.sample_rate), "-ac", "1", "-c:a", "pcm_s16le"]
            else:
                codec_args = ["-ar", str(args.sample_rate), "-ac", "1"]
            subprocess.run(
                ["ffmpeg", "-y", "-i", tmp_path, *codec_args, args.output],
                check=True,
                capture_output=True,
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    asyncio.run(synth())
    return 0


if __name__ == "__main__":
    sys.exit(main())
