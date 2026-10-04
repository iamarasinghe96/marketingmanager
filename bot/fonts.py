"""Download open-source Google Fonts into this project only."""
from pathlib import Path
from urllib.parse import quote
import httpx
from bot.config import ROOT

FONTS = {
    "Montserrat": "montserrat/Montserrat[wght].ttf",
    "Inter": "inter/Inter[opsz,wght].ttf",
    "Oswald": "oswald/Oswald[wght].ttf",
    "NotoSansSinhala": "notosanssinhala/NotoSansSinhala[wdth,wght].ttf",
    "NotoSansTamil": "notosanstamil/NotoSansTamil[wdth,wght].ttf",
    "AbhayaLibre": "abhayalibre/AbhayaLibre-ExtraBold.ttf",
}


def download():
    folder = ROOT / "fonts"
    folder.mkdir(exist_ok=True)
    with httpx.Client(timeout=60,follow_redirects=True) as client:
        for name,remote in FONTS.items():
            target = folder / (name + ".ttf")
            if target.exists() and target.stat().st_size > 10000:
                continue
            response = client.get("https://raw.githubusercontent.com/google/fonts/main/ofl/" + quote(remote,safe="/"))
            response.raise_for_status()
            if response.content[:4] not in {b"\x00\x01\x00\x00",b"OTTO",b"ttcf"}:
                raise ValueError(f"Download did not contain a valid font: {name}")
            temporary = target.with_suffix(".download")
            temporary.write_bytes(response.content)
            temporary.replace(target)
            licence = client.get("https://raw.githubusercontent.com/google/fonts/main/ofl/" + remote.split("/")[0] + "/OFL.txt")
            licence.raise_for_status()
            (folder / (name + "-OFL.txt")).write_text(licence.text,encoding="utf-8")
            print(f"Downloaded {name}")


if __name__ == "__main__":
    download()
