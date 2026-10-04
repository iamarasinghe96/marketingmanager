"""Type a Sinhala/Tamil title onto a finished image with a real font.

AI image tools break Sinhala and Tamil letters, so on those days ChatGPT leaves the title area
empty and the bot renders the title here (Chromium shapes the script correctly), then pastes it in.
"""
import html
import io

from PIL import Image

from bot.render import font_css, rendering_browser

FAMILIES = {"si": "Noto Sans Sinhala", "ta": "Noto Sans Tamil"}

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
{fonts}
html,body{{margin:0;background:transparent}}
#t{{width:{width}px;font-family:'{family}';font-weight:800;color:{color};line-height:1.32;
   white-space:normal;word-break:keep-all;overflow-wrap:normal;font-size:{start}px}}
</style></head><body><div id="t">{text}</div>
<script>
// Largest size where the title fits the box, wrapping only between words.
const t=document.getElementById('t');let size={start};
while(size>18&&(t.scrollHeight>{height}||t.scrollWidth>{width})){{size-=2;t.style.fontSize=size+'px';}}
</script></body></html>"""


async def render_title(text, language, color, width, height):
    """Return a transparent PNG (PIL image) of the title wrapped by words to fit width×height."""
    words = " ".join(text.split())
    page_html = PAGE.format(fonts=font_css(), family=FAMILIES[language], color=color, width=width, height=height,
                            start=min(160, height), text=html.escape(words))
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
    """Paste the rendered title into box=(x, y, w, h) fractions of the image, top-left aligned."""
    x, y, w, h = (round(v * base.size[i % 2]) for i, v in enumerate(box))
    title = await render_title(text, language, color, w, h)
    canvas = base.convert("RGBA")
    canvas.alpha_composite(title, (x, y))
    return canvas.convert("RGB")
