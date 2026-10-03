from __future__ import annotations
import html
import os
from contextlib import asynccontextmanager
from pathlib import Path

from bot.config import ROOT
from bot.fonts import FONTS


class TextOverflow(ValueError):
    pass


def largest_fitting_size(minimum,maximum,measure):
    """Binary search with the browser's shaped-text measurement as the predicate."""
    if not measure(minimum):
        raise TextOverflow("Text is too long at its minimum readable size")
    low, high = minimum,maximum
    while low < high:
        mid = (low + high + 1) // 2
        if measure(mid):
            low = mid
        else:
            high = mid - 1
    return low


FIT_SCRIPT = """() => {
  const problems = [];
  for (const element of document.querySelectorAll('[data-fit]')) {
    const min = Number(element.dataset.min), max = Number(element.dataset.max);
    const fits = (size) => {
      element.style.fontSize = size + 'px';
      return element.scrollHeight <= element.clientHeight + 1 && element.scrollWidth <= element.clientWidth + 1;
    };
    if (!fits(min)) { problems.push(element.id || element.className); continue; }
    let low = min, high = max;
    while (low < high) { const mid = Math.ceil((low + high) / 2); if (fits(mid)) low = mid; else high = mid - 1; }
    fits(low);
  }
  for (const element of document.querySelectorAll('[data-bounds]')) {
    if (element.scrollHeight > element.clientHeight + 2 || element.scrollWidth > element.clientWidth + 2) problems.push(element.className);
    const box = element.getBoundingClientRect();
    if (box.left < 0 || box.top < 0 || box.right > innerWidth+1 || box.bottom > innerHeight+1) problems.push(element.className + ':canvas');
  }
  return problems;
}"""


def font_css():
    families = {"Montserrat":"Montserrat","Inter":"Inter","Oswald":"Oswald",
                "NotoSansSinhala":"Noto Sans Sinhala","NotoSansTamil":"Noto Sans Tamil"}
    return "\n".join(f"@font-face{{font-family:'{families[name]}';src:url('{(ROOT/'fonts'/f'{name}.ttf').as_uri()}');font-weight:100 900;}}" for name in FONTS)


def image_tag(path,css_class="visual-image"):
    return f'<img class="{css_class}" src="{html.escape(Path(path).resolve().as_uri(),quote=True)}" alt="">' if path else ""


def select_visual(campaign,copy,provided,out_dir):
    if copy.category == "EDUCATIONAL" and copy.visual_kind == "premises":
        raise ValueError("Educational premises imagery is forbidden")
    if copy.visual_kind == "screenshot":
        screenshots = campaign.files(campaign.screenshots)
        return screenshots[copy.screenshot_index % len(screenshots)] if screenshots else None
    if copy.visual_kind == "premises":
        from PIL import Image
        x,y,w,h = campaign.premises_crop
        with Image.open(campaign.asset(campaign.premises_path)) as img:
            width,height = img.size
            box = tuple(round(value) for value in (x*width,y*height,(x+w)*width,(y+h)*height))
            if min(box[:2]) < 0 or box[2] > width or box[3] > height or w <= 0 or h <= 0:
                raise ValueError("Invalid premises crop")
            target = out_dir / "premises-crop.png"
            img.crop(box).save(target)
            return target
    if provided and Path(provided).suffix.lower() in {".png",".jpg",".jpeg",".webp"}:
        from PIL import Image,ImageOps
        # Decode oversized uploads before starting Chromium. Keep the original
        # untouched and give the browser a bounded composition copy instead.
        with Image.open(provided) as img:
            if img.width>1600 or img.height>2400 or img.getexif().get(274,1)!=1:
                img.draft("RGB",(1600,2400))
                img.thumbnail((1600,2400),Image.Resampling.LANCZOS)
                ImageOps.exif_transpose(img,in_place=True)
                target = out_dir/"composition-visual.png"
                img.save(target)
                return target
    return provided


def compose_html(campaign,copy,layout,variant,visual,kind):
    width,height = campaign.post_size if kind == "post" else campaign.story_size
    educational = copy.category == "EDUCATIONAL"
    institutional = copy.category == "INSTITUTIONAL"
    if educational and campaign.style != "clinic":
        raise ValueError("Unexpected educational category")
    escape = html.escape
    palette = campaign.palette
    footer_depth = 250 if educational else 175 if institutional else 88
    header_top = 130 if kind == "story" else 64
    footer_bottom = 175 if kind == "story" else 48
    top = header_top + (60 if educational else 115)
    bottom = height-footer_depth-footer_bottom-32
    work_height = bottom-top
    story = kind == "story"
    inset = 76
    text_x, text_y, text_w, text_h = inset,top,width-2*inset,work_height
    image_box = (inset,top+work_height*.48,width-2*inset,work_height*.5)
    background,foreground = palette["background"],palette["primary"]
    align = "left"
    motif = ""
    visual_class = "rounded"
    if layout in {"type","blue","story_type"}:
        visual = None
        text_y = top + work_height*(.08 + .025*(variant%4))
        text_h = work_height*.8
        text_w = width-inset*2-24*(variant%3)
        if variant % 2:
            align = "center"
            text_x = (width-text_w)/2
        if layout == "blue":
            background,foreground = palette["primary"],"#FFFFFF"
        motif = f'<div class="rule" style="top:{bottom-20}px;left:{inset}px;width:{130+variant*16}px"></div>'
    elif layout in {"photo","numbered","story_photo"}:
        text_h = work_height*.45
        image_box = (inset,top+work_height*.51,width-2*inset,work_height*.46)
        if layout == "numbered":
            # Sequence marker is a layout device explicitly permitted by the brand.
            motif = f'<div class="sequence" style="top:{top}px">01.</div>'
            text_x += 170
            text_w -= 170
    elif layout in {"split","phone"}:
        text_w = width*.47-inset
        text_h = work_height*.87
        text_y += work_height*.06
        image_box = (width*.54,top,width*.40,work_height*.96)
        if variant % 2 and layout == "split":
            image_box = (inset,top,width*.40,work_height*.96)
            text_x = width*.53
        if layout == "phone":
            visual_class = "phone" if copy.visual_kind == "screenshot" else "rounded"
    elif layout == "circle":
        text_h = work_height*.46
        side = min(width*.65,work_height*.49)
        image_box = ((width-side)/2+(variant%3-1)*50,top+work_height*.5,side,side)
        visual_class = "circle"
    elif layout in {"contact","story_panel"}:
        image_box = (0,top+work_height*.38,width,work_height*.65)
        text_h = work_height*.34
        visual_class = "full"
        if layout == "contact":
            background = palette["accent"]
    if not visual and layout not in {"type","blue","story_type"}:
        # Maintain each layout's alignment while letting its typography fill the space.
        text_h = work_height*.83
        text_w = width-inset*2
        text_x = inset
        if layout == "circle":
            align = "center"
        motif = f'<div class="rule" style="top:{bottom-12}px;left:{inset}px;width:{160+variant*12}px"></div>'
    multilingual = copy.language != "en"
    if multilingual and layout in {"split","phone","numbered"}:
        # Native scripts need wider text boxes, independent line breaks and more leading.
        text_x,text_w,text_h,text_y = inset,width-2*inset,work_height*.51,top
        image_box = (inset,top+work_height*.56,width-2*inset,work_height*.42)
        motif = ""
    language_font = campaign.fonts.get("sinhala" if copy.language == "si" else "tamil") if multilingual else campaign.fonts["headline"]
    head_max = 132 if story else 120
    if layout in {"type","blue","story_type"}:
        head_max = 164 if not multilingual else 112
    head_min = 50 if multilingual else 48
    head_height = text_h * (.48 if copy.supporting or copy.body or copy.items else .70 if copy.cta else .90)
    body_height = text_h*.26
    cta_height = text_h*.12
    header = ""
    if not educational:
        logo = image_tag(campaign.asset(campaign.logo_path),"logo")
        wordmark = '<span class="wordmark">' + escape(campaign.wordmark or campaign.name.upper()) + '</span>' if campaign.style == "lushnote" else ""
        header = f'<header style="top:{header_top}px">{logo}{wordmark}</header>'
    if educational:
        rules = campaign.required_footer_rules
        footer = '<div class="attribution">' + escape(rules.get("attribution","")) + '</div><div class="disclaimer">' + escape(rules.get("disclaimer","")) + '</div>'
    elif institutional:
        footer = f'<div class="phone-number">{escape(campaign.phone)}</div><div class="address">{escape(campaign.address)}</div>'
    else:
        footer = f'<div class="url">{escape(campaign.website)}</div>'
    body = escape("\n".join(part for part in (copy.supporting,copy.body) if part)).replace("\n","<br>")
    if copy.items:
        body += '<ul>' + ''.join('<li>' + escape(item) + '</li>' for item in copy.items) + '</ul>'
    body_html = f'<div id="support" class="support" data-fit data-min="{26 if not multilingual else 30}" data-max="{36 if not multilingual else 40}">{body}</div>' if body else ''
    cta_html = f'<div id="cta" class="cta" data-fit data-min="25" data-max="32">{escape(copy.cta)}</div>' if copy.cta else ''
    x,y,w,h = image_box
    visual_html = f'<div class="visual {visual_class}" style="left:{x}px;top:{y}px;width:{w}px;height:{h}px">{image_tag(visual)}</div>' if visual else ""
    muted = "#FFFFFF" if background == palette["primary"] else palette["body"]
    return f'''<!doctype html><html lang="{copy.language}"><meta charset="utf-8"><style>
{font_css()}
*{{box-sizing:border-box}}html,body{{margin:0;width:{width}px;height:{height}px;background:{background};color:{foreground};overflow:hidden}}
body{{font-family:'{campaign.fonts['body']}','Noto Sans Sinhala','Noto Sans Tamil',sans-serif}}
header{{position:absolute;left:{inset}px;display:flex;gap:16px;align-items:center;height:70px}}
.logo{{height:{100 if institutional else 70}px;width:auto;max-width:390px;object-fit:contain}} .wordmark{{font-family:'{campaign.fonts['wordmark']}';font-weight:700;font-size:58px;letter-spacing:-1px}}
.text{{position:absolute;left:{text_x}px;top:{text_y}px;width:{text_w}px;height:{text_h}px;text-align:{align}}}
h1{{margin:0;height:{head_height}px;max-width:100%;font-family:'{language_font}','Noto Sans Sinhala','Noto Sans Tamil',sans-serif;font-weight:800;letter-spacing:{'0' if multilingual else '-0.055em'};line-height:{'1.35' if multilingual else '1.04'};text-wrap:balance;overflow-wrap:normal;white-space:pre-line}}
.support{{height:{body_height}px;margin-top:16px;font-family:'{language_font if multilingual else campaign.fonts['body']}';font-weight:400;line-height:1.45;color:{muted};text-wrap:pretty}}
ul{{padding-left:34px;margin:8px 0}}li{{margin-bottom:3px}}.cta{{height:{cta_height}px;margin-top:20px;font-weight:600;color:{foreground}}}
.visual{{position:absolute;overflow:hidden}}.visual-image{{width:100%;height:100%;object-fit:cover;object-position:center}}
.rounded{{border-radius:32px}}.circle{{border-radius:50%}}.phone{{border:11px solid #202427;border-radius:46px;background:white}}
.phone .visual-image{{object-fit:contain}}.rule{{position:absolute;height:8px;background:{palette['accent']}}}
.sequence{{position:absolute;left:{inset}px;font-family:Montserrat;font-size:105px;font-weight:800;color:{palette['accent']}}}
footer{{position:absolute;left:{inset}px;bottom:{footer_bottom}px;width:{width-2*inset}px;height:{footer_depth}px;display:flex;flex-direction:column;justify-content:flex-end;gap:12px;color:{foreground}}}
.attribution{{font-size:23px;line-height:1.45;font-family:Inter,'Noto Sans Sinhala';font-weight:500}}
.disclaimer{{font-size:23px;line-height:1.55;font-family:'Noto Sans Sinhala';font-weight:400}}
.phone-number{{font-size:48px;font-weight:700;line-height:1.1}}.address{{font-size:28px;line-height:1.4}}.url{{font-size:30px;font-weight:600;text-align:{'right' if variant%2 else 'left'}}}
</style><body>{header}{visual_html}{motif}<div class="text" data-bounds>
<h1 id="headline" data-fit data-min="{head_min}" data-max="{head_max}">{escape(copy.headline)}</h1>
{body_html}{cta_html}
</div><footer data-bounds>{footer}</footer></body></html>'''


@asynccontextmanager
async def rendering_browser():
    from playwright.async_api import async_playwright
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",str(ROOT/".playwright"))
    for name in FONTS:
        if not (ROOT/"fonts"/f"{name}.ttf").is_file():
            raise ValueError("Fonts missing. Run python -m bot.fonts (installer does this automatically).")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True,args=["--disable-background-networking","--disable-extensions","--renderer-process-limit=1"])
        try:
            yield browser
        finally:
            await browser.close()


async def render_one(browser,campaign,copy,layout,variant,visual,target,kind):
    size = campaign.post_size if kind == "post" else campaign.story_size
    source = target.with_suffix(".html")
    source.write_text(compose_html(campaign,copy,layout,variant,visual,kind),encoding="utf-8")
    context = await browser.new_context(viewport={"width":size[0],"height":size[1]},device_scale_factor=1)
    try:
        page = await context.new_page()
        await page.goto(source.resolve().as_uri(),wait_until="load")
        await page.evaluate("document.fonts.ready")
        await page.evaluate("Promise.all([...document.images].map(img => img.decode()))")
        problems = await page.evaluate(FIT_SCRIPT)
        if problems:
            raise TextOverflow("Copy exceeds readable layout: " + ", ".join(problems))
        await page.screenshot(path=str(target),type="png")
    finally:
        await context.close()


async def render_pair(campaign,copy,layouts,visuals,out_dir):
    feed_layout,story_layout,feed_variant,story_variant = layouts
    feed_visual = select_visual(campaign,copy,visuals.get("post"),out_dir)
    story_visual = select_visual(campaign,copy,visuals.get("story"),out_dir)
    # Missing generated imagery explicitly becomes a typography layout.
    actual_layouts = (feed_layout,story_layout,feed_variant,story_variant)
    async with rendering_browser() as browser:
        for kind,layout,variant,visual in (("post",feed_layout,feed_variant,feed_visual),("story",story_layout,story_variant,story_visual)):
            await render_one(browser,campaign,copy,layout,variant,visual,out_dir / f"{kind}.png",kind)
    return out_dir/"post.png",out_dir/"story.png",actual_layouts
