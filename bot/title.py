"""Type a Sinhala/Tamil title onto a finished image with a real font.

AI image tools break Sinhala and Tamil letters, so on those days ChatGPT leaves the title area
empty and the bot renders the title here (Chromium shapes the script correctly), then pastes it in.
"""
import html
import io

from PIL import Image, ImageFilter

from bot.render import font_css, rendering_browser

FAMILIES = {"si": "Abhaya Libre", "ta": "Noto Sans Tamil"}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
{fonts}
html,body{{margin:0;background:transparent}}
#t{{width:{width}px;box-sizing:border-box;font-family:'{family}';font-weight:800;color:{color};line-height:1.3;
   padding:0.32em 0 0.18em 0;white-space:normal;word-break:keep-all;overflow-wrap:normal;font-size:{start}px}}
</style></head><body><div id="t">{text}</div>
<script>
// Largest size where the title fits the box, wrapping only between words.
// The padding keeps tall vowel signs (e.g. the top of වි) and descenders inside the image.
const t=document.getElementById('t');let size={start};
while(size>18&&(t.scrollHeight>{height}||t.scrollWidth>{width})){{size-=2;t.style.fontSize=size+'px';}}
</script></body></html>"""


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
        return light[px, py] < 175 or edges[px, py] > 40

    right = sw
    streak = 0
    for col in range(sw):
        if sum(busy(col, row) for row in range(sh)) / sh > 0.12:
            streak += 1
            if streak >= 3:
                right = col - 5  # stop with a small gap before the photo
                break
        else:
            streak = 0
    bottom = sh
    streak = 0
    for row in range(sh):
        if sum(busy(col, row) for col in range(max(1, right))) / max(1, right) > 0.12:
            streak += 1
            if streak >= 3:
                bottom = row - 2
                break
        else:
            streak = 0
    # If the area is mostly busy, keep the requested box rather than shrinking to nothing.
    width = right * 4 if right >= sw * 0.5 else w
    height = bottom * 4 if bottom >= sh * 0.5 else h
    return x, y, width, height


async def render_title(text, language, color, width, height):
    """Return a transparent PNG (PIL image) of the title wrapped by words to fit width×height."""
    words = " ".join(text.split())
    page_html = PAGE.format(fonts=font_css(), family=FAMILIES[language], color=color, width=width, height=height,
                            start=min(150, height), text=html.escape(words))
    async with rendering_browser() as browser:
        page = await browser.new_page(viewport={"width": width, "height": max(height, 200)}, device_scale_factor=2)
        await page.set_content(page_html)
        await page.evaluate("document.fonts.ready")
        await page.wait_for_timeout(100)
        png = await page.locator("#t").screenshot(omit_background=True)
        await page.close()
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    # Rendered at 2x for crisp edges; scale back to the box size.
    return image.resize((max(1, image.width // 2), max(1, image.height // 2)), Image.Resampling.LANCZOS)


async def add_title(base, text, language, color, box):
    """Paste the rendered title into the plain part of box=(x, y, w, h) fractions, top-left aligned."""
    pixels = tuple(round(v * base.size[i % 2]) for i, v in enumerate(box))
    x, y, w, h = calm_box(base, pixels)
    title = await render_title(text, language, color, w, h)
    canvas = base.convert("RGBA")
    canvas.alpha_composite(title, (x, y))
    return canvas.convert("RGB")
