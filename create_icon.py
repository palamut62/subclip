"""
create_icon.py
Reclip ICO dosyasini olusturur ve masaustu kisayolunu gunceller.
Kullanim: python create_icon.py
"""

import os
import subprocess
from PIL import Image, ImageDraw, ImageFont

BASE     = os.path.dirname(os.path.abspath(__file__))
ICO_OUT  = os.path.join(BASE, "static", "reclip.ico")
VBS_PATH = os.path.join(BASE, "start_reclip.vbs")

# Renk paleti (uygulama temasina uygun)
BG     = (42, 42, 38, 255)     # #2a2a26 - koyu zemin
FG     = (244, 241, 235, 255)  # #f4f1eb - krem beyaz
ACCENT = (232, 93, 42, 255)    # #e85d2a - turuncu
WHITE  = (255, 255, 255, 255)


def find_font(size: int) -> ImageFont.FreeTypeFont:
    """Windows sistem fontlarindan en iyisini bul (serif tercihli)."""
    candidates = [
        "C:/Windows/Fonts/georgiab.ttf",    # Georgia Bold - serif, ideal
        "C:/Windows/Fonts/georgia.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",    # Segoe UI Bold
        "C:/Windows/Fonts/seguisb.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_down_arrow(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int) -> None:
    """Turuncu daire icinde beyaz asagi ok."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    sw = max(2, r // 4)      # ok govde genisligi
    sh = int(r * 0.52)       # ok govde yuksekligi (yukari dogru)
    aw = int(r * 0.60)       # ok ucu yari genisligi
    ah = int(r * 0.48)       # ok ucu yuksekligi
    # Govde
    draw.rectangle([cx - sw // 2, cy - sh, cx + sw // 2, cy + 2], fill=WHITE)
    # Ucgen ok ucu
    draw.polygon([(cx - aw, cy - 2), (cx + aw, cy - 2), (cx, cy + ah)], fill=WHITE)


def make_image(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Yuvarlatilmis koyu zemin
    corner = max(3, int(size * 0.22))
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=corner, fill=BG)

    if size >= 48:
        # Buyuk boyutlar: "R" harfi + turuncu ok rozeti
        font_size = int(size * 0.62)
        font = find_font(font_size)

        # "R" metnini sol-yukari hizala (rozetle capismamasi icin)
        bbox = draw.textbbox((0, 0), "R", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = int(size * 0.10) - bbox[0]
        ty = int(size * 0.05) - bbox[1]
        draw.text((tx, ty), "R", fill=FG, font=font)

        # Sag alt kose - turuncu download rozeti
        r = int(size * 0.20)
        ax = size - int(size * 0.17)
        ay = size - int(size * 0.17)
        draw_down_arrow(draw, ax, ay, r)

    else:
        # Kucuk boyutlar (16, 32): sadece "R", rozetle ugrastirma
        font_size = int(size * 0.68)
        font = find_font(font_size)
        bbox = draw.textbbox((0, 0), "R", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = (size - tw) // 2 - bbox[0]
        ty = (size - th) // 2 - bbox[1]
        draw.text((tx, ty), "R", fill=FG, font=font)

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
    lnk_path = os.path.join(desktop, "ReClip.lnk")

    # Path'lerdeki tek tirnaklari temizle
    def q(p): return p.replace("'", "")

    ps_script = f"""
$sh = New-Object -comObject WScript.Shell
$lnk = $sh.CreateShortcut('{q(lnk_path)}')
$lnk.TargetPath = 'wscript.exe'
$lnk.Arguments = '"{q(VBS_PATH)}"'
$lnk.IconLocation = '{q(ico_path)},0'
$lnk.Description = 'ReClip - Free Media Downloader and Dubber'
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
    print("\nBitti. Masaustunde 'ReClip' kisayolunu gorebilirsin.")
    print("(Eski kisayol varsa sil, yeni .lnk dosyasini kullan.)")
