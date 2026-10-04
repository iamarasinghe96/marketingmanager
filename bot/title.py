"""Type a Sinhala/Tamil title onto a finished image with a real font.

AI image tools break Sinhala and Tamil letters, so on those days ChatGPT leaves the title area
empty and the bot renders the title here (Chromium shapes the script correctly), then pastes it in.
"""
import base64
import html
import io

from PIL import Image, ImageFilter

from bot.config import ROOT
from bot.render import rendering_browser

FAMILIES = {"si": "Abhaya Libre", "ta": "Noto Sans Tamil"}
FILES = {"si": "AbhayaLibre.ttf", "ta": "NotoSansTamil.ttf"}


def embedded_font(language):
    """The font travels inside the page itself: browsers may refuse file:// fonts and silently
    fall back to a system font (which is what produced the wrong Sinhala look)."""
    path = ROOT / "fonts" / FILES[language]
    if not path.is_file():
        raise ValueError(f"Font file missing: {path.name}. Run update.bat (it downloads the fonts).")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return (f"@font-face{{font-family:'{FAMILIES[language]}';src:url(data:font/ttf;base64,{data}) format('truetype');"
            "font-weight:100 900;}}")

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
{fonts}
html,body{{margin:0;background:transparent}}
#box{{width:{width}px;height:{height}px;display:flex;flex-direction:column;justify-content:{justify}}}
#t{{width:{width}px;box-sizing:border-box;font-family:'{family}';font-weight:{weight};color:{color};line-height:{line_height};
   text-align:{align};padding:0.32em 0 0.18em 0;white-space:pre-line;word-break:keep-all;overflow-wrap:normal;font-size:{start}px}}
.hl{{color:{highlight_color}}}
</style></head><body><div id="box"><div id="t">{text}</div></div>
<script>
// Largest size where the title fits the box, wrapping only between words.
// The padding keeps tall vowel signs (e.g. the top of වි) and descenders inside the image.
const t=document.getElementById('t');let size={start};
while(size>18&&(t.scrollHeight>{height}||t.scrollWidth>{width})){{size-=2;t.style.fontSize=size+'px';}}
</script></body></html>"""

JUSTIFY = {"top": "flex-start", "middle": "center", "bottom": "flex-end"}


def calm_box(image, box):
    """Shrink box=(x, y, w, h) in pixels to the plain, light part, so the title stays off the photo.

    Columns (then rows) are kept from the left/top while they are mostly light and free of edges.
    """
    x, y, w, h = box
    region = image.convert("L").crop((x, y, x + w, y + h))
    small = region.resize((max(1, w // 4), max(1, h // 4)))
    edges = small.filter(ImageFilter.FIND_EDGES).load()
    light = small.load()
    sw, sh = small.size

    def busy(px, py):
        return light[px, py] < 140 or edges[px, py] > 60

    right = sw
    streak = 0
    for col in range(sw):
        if sum(busy(col, row) for row in range(sh)) / sh > 0.25:
            streak += 1
            if streak >= 3:
                right = col - 5  # stop with a small gap before the photo
                break
        else:
            streak = 0
    bottom = sh
    streak = 0
    for row in range(sh):
        if sum(busy(col, row) for col in range(max(1, right))) / max(1, right) > 0.25:
            streak += 1
            if streak >= 3:
                bottom = row - 2
                break
        else:
            streak = 0
    # If the area is mostly busy, keep the requested box rather than shrinking to nothing.
    width = right * 4 if right >= sw * 0.7 else w
    height = bottom * 4 if bottom >= sh * 0.7 else h
    return x, y, width, height


def marked(text, highlight):
    """HTML-escape the title and wrap the highlighted words in a coloured span."""
    out = html.escape(text)
    for word in sorted({w.strip() for w in highlight if w.strip()}, key=len, reverse=True):
        out = out.replace(html.escape(word), f'<span class="hl">{html.escape(word)}</span>')
    return out


async def render_title(text, language, color, width, height, align="left", valign="top", line_height=1.3,
                       highlight=(), highlight_color="", weight=800):
    """Return a transparent PNG (PIL image) of the title wrapped by words to fit width×height."""
    words = "\n".join(" ".join(line.split()) for line in text.replace("\\n", "\n").splitlines() if line.strip())
    page_html = PAGE.format(fonts=embedded_font(language), family=FAMILIES[language], color=color, width=width, height=height,
                            start=min(320, height), text=marked(words, highlight), align=align, justify=JUSTIFY[valign],
                            line_height=line_height, highlight_color=highlight_color or color, weight=weight)
    async with rendering_browser() as browser:
        page = await browser.new_page(viewport={"width": width, "height": max(height, 200)}, device_scale_factor=2)
        await page.set_content(page_html)
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(100)
        loaded = await page.evaluate(f"document.fonts.check(\"40px '{FAMILIES[language]}'\", {words!r})")
        if not loaded:
            raise ValueError(f"The {FAMILIES[language]} font did not load, so the title was not added.")
        # Re-run the fit now that the real font is in place.
        await page.evaluate(f"(()=>{{const t=document.getElementById('t');let s={min(320, height)};t.style.fontSize=s+'px';"
                            f"while(s>18&&(t.scrollHeight>{height}||t.scrollWidth>{width})){{s-=2;t.style.fontSize=s+'px';}}}})()")
        png = await page.locator("#box").screenshot(omit_background=True)
        await page.close()
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    # Rendered at 2x for crisp edges; scale back to the box size.
    return image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)


async def add_title(base, text, language, color, box, scale=1.0, fill=False, **style):
    """Paste the rendered title into box=(x, y, w, h) fractions. Normally only the plain part of the box is
    used; fill=True uses the whole box so the title can be as large as possible."""
    pixels = tuple(round(v * base.size[i % 2]) for i, v in enumerate(box))
    x, y, w, h = pixels if fill else calm_box(base, pixels)
    if fill:
        # Tighter lines leave more height for bigger letters (padding still protects tall vowel signs).
        style["line_height"] = min(style.get("line_height", 1.3), 1.05)
    # "Bigger" grows the box but never wider than the empty title area (so it stays off the photo);
    # the extra room goes downwards. "Smaller" shrinks it.
    w = max(60, min(round(w * scale), pixels[2], base.size[0] - x - 20))
    h = max(40, min(round(h * scale), base.size[1] - y - 20))
    title = await render_title(text, language, color, w, h, **style)
    canvas = base.convert("RGBA")
    canvas.alpha_composite(title, (x, y))
    return canvas.convert("RGB")
