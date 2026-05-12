"""
create_icon.py
SubClip ICO dosyasini olusturur ve masaustu kisayolunu gunceller.
Kullanim: python create_icon.py
"""

import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

BASE     = os.path.dirname(os.path.abspath(__file__))
ICO_OUT  = os.path.join(BASE, "static", "subclip.ico")
VBS_PATH = os.path.join(BASE, "start_reclip.vbs")

# Renk paleti (skill: modern-minimal, yuksek kontrast)
BG      = (37, 99, 235, 255)    # #2563EB
BG_DARK = (29, 78, 216, 255)    # #1D4ED8
FG      = (255, 255, 255, 255)  # beyaz
ACCENT  = (147, 197, 253, 255)  # #93C5FD


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Windows sistem fontlarindan sans serif bir font bul."""
    candidates = [
        "C:/Windows/Fonts/segoeuib.ttf",
        "C:/Windows/Fonts/seguisb.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def lerp_color(a: tuple[int, int, int, int], b: tuple[int, int, int, int], t: float) -> tuple[int, int, int, int]:
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
        int(a[3] + (b[3] - a[3]) * t),
    )


def draw_bg(draw: ImageDraw.ImageDraw, size: int, radius: int) -> None:
    """Yuksek kontrastli, minimal zemin."""
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=BG)
    # Ustten hafif vurgu
    h = max(2, int(size * 0.34))
    draw.rounded_rectangle(
        [0, 0, size - 1, h],
        radius=radius,
        fill=(BG_DARK[0], BG_DARK[1], BG_DARK[2], 36),
    )


def draw_subtitle_glyph(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Play + subtitle bar metaforu."""
    # Sol-orta play ucu
    tri_w = max(4, int(size * 0.22))
    tri_h = max(6, int(size * 0.26))
    cx = int(size * 0.36)
    cy = int(size * 0.45)
    tri = [
        (cx - tri_w // 2, cy - tri_h // 2),
        (cx - tri_w // 2, cy + tri_h // 2),
        (cx + tri_w // 2, cy),
    ]
    draw.polygon(tri, fill=FG)

    # Sagda iki altyazi satiri
    bar_x = int(size * 0.50)
    bar_w = int(size * 0.34)
    bar_h = max(2, int(size * 0.07))
    gap = max(2, int(size * 0.05))
    y1 = int(size * 0.36)
    y2 = y1 + bar_h + gap
    r = max(1, bar_h // 2)
    draw.rounded_rectangle([bar_x, y1, bar_x + bar_w, y1 + bar_h], radius=r, fill=ACCENT)
    draw.rounded_rectangle([bar_x, y2, bar_x + int(bar_w * 0.82), y2 + bar_h], radius=r, fill=ACCENT)


def make_image(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Yuvarlatilmis mavi zemin
    corner = max(3, int(size * 0.22))
    draw_bg(draw, size, corner)

    if size >= 24:
        draw_subtitle_glyph(draw, size)
    else:
        # 16x16 gibi boyutlarda tek, guclu siluet: play
        tri_w = max(4, int(size * 0.34))
        tri_h = max(6, int(size * 0.46))
        cx = size // 2
        cy = size // 2
        tri = [
            (cx - tri_w // 2, cy - tri_h // 2),
            (cx - tri_w // 2, cy + tri_h // 2),
            (cx + tri_w // 2, cy),
        ]
        draw.polygon(tri, fill=FG)

    return img


def create_ico() -> str:
    sizes = [256, 128, 64, 48, 32, 16]
    images = [make_image(s) for s in sizes]
    os.makedirs(os.path.dirname(ICO_OUT), exist_ok=True)
    images[0].save(
        ICO_OUT,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )
    print(f"[OK] ICO olusturuldu: {ICO_OUT}")
    return ICO_OUT


def create_shortcut(ico_path: str) -> None:
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk_path = os.path.join(desktop, "SubClip.lnk")

    # Path'lerdeki tek tirnaklari temizle
    def q(p): return p.replace("'", "")

    ps_script = f"""
$sh = New-Object -comObject WScript.Shell
$lnk = $sh.CreateShortcut('{q(lnk_path)}')
$lnk.TargetPath = 'wscript.exe'
$lnk.Arguments = '"{q(VBS_PATH)}"'
$lnk.IconLocation = '{q(ico_path)},0'
$lnk.Description = 'SubClip - Free Media Downloader and Subtitler'
$lnk.WorkingDirectory = '{q(BASE)}'
$lnk.WindowStyle = 7
$lnk.Save()
Write-Output 'done'
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
        capture_output=True, text=True,
    )
    if "done" in result.stdout:
        print(f"[OK] Masaustu kisayolu guncellendi: {lnk_path}")
    else:
        print(f"[HATA] Kisayol olusturulamadi:")
        print(result.stderr.strip() or result.stdout.strip())


if __name__ == "__main__":
    ico = create_ico()
    create_shortcut(ico)
    print("\nBitti. Masaustunde 'SubClip' kisayolunu gorebilirsin.")
    print("(Eski kisayol varsa sil, yeni .lnk dosyasini kullan.)")
