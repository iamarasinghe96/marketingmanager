"""Local, inspectable ChatGPT handoff files. Never call an image generator."""
import re
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageOps
from bot.models import Copy


def reference_requirements(campaign, references, instructions=""):
    rules = []
    for index, reference in enumerate(references, 1):
        name = f"style_reference_{index}.jpg"
        rules.append(
            f"STYLE REFERENCE ATTACHED ({name}): follow its layout approach, composition, mood and visual treatment as closely as possible "
            f"while keeping all {campaign.name if campaign.style == 'clinic' else 'LushNote'} brand rules (logo, colours, typography, text control). "
            "Do not copy any text, logos or brand names from the reference.")
        if reference.get("description"):
            rules.append(f"Style description for {name} (visual guidance only): {reference['description']}")
    if references and instructions:
        rules.append("Owner's exact reference instructions:\n" + instructions)
    if campaign.style == "clinic":
        rules.append("EDUCATIONAL / INSTITUTIONAL separation and medical communication rules take priority over the reference. Never copy doctor portraits, names or credentials into any artwork.")
    return "\n\n".join(rules)


def style_instructions(text):
    """Keep exact visual directions without reattaching the idea's unsafe copy."""
    lines = []
    pattern = r"\b(?:layout|background|composition|colou?r|mood|style|make it look like|take .* from|like this)\b"
    for line in text.splitlines():
        if re.search(pattern,line,re.I):
            clauses = re.split(r"(?<=[.!?;])\s+",line)
            lines.extend(clause for clause in clauses if re.search(pattern,clause,re.I))
    return "\n".join(lines)


def filled_prompt(campaign, draft, references):
    copy = Copy.model_validate_json(draft["copy"])
    width, height = campaign.story_size if draft["kind"] == "story" else campaign.post_size
    special = copy.special_requirements
    directions = draft.get("reference_instructions")
    reference_rules = reference_requirements(campaign, references, directions if directions is not None else style_instructions(draft["idea"]))
    special = "\n\n".join(x for x in (special, reference_rules) if x) or "NONE"
    common = {"CAMPAIGN PURPOSE": copy.purpose or copy.topic, "HEADLINE": copy.headline,
              "CTA": copy.cta or "NONE", "PRIMARY VISUAL": copy.visual_brief, "SPECIAL REQUIREMENTS": special}
    if campaign.style == "lushnote":
        values = {**common, "SUPPORTING COPY": copy.supporting or "NONE", "URL": copy.url or campaign.website or "NONE"}
    else:
        educational = copy.category == "EDUCATIONAL"
        values = {**common, "CAMPAIGN TYPE": copy.category, "FORMAT": f"{width} × {height} {draft['kind'].upper()}",
                  "LANGUAGE": {"en": "ENGLISH", "si": "SINHALA", "ta": "TAMIL"}[copy.language],
                  "SUBHEAD": copy.supporting or "NONE", "BODY": copy.body or "NONE", "LIST": "\n".join(copy.items) or "NONE",
                  # Owner decision: phone and address in the bottom band of every clinic post.
                  "PHONE": campaign.phone or "NONE",
                  "ADDRESS": campaign.address or "NONE",
                  "WEBSITE / EMAIL": "NONE" if educational else copy.url or campaign.website or "NONE",
                  # Owner decision: no doctor attribution in artwork; the clinic logo is the only branding.
                  "DOCTOR ATTRIBUTION": "NONE",
                  # Owner decision: the disclaimer goes in the caption, not the artwork.
                  "EDUCATIONAL DISCLAIMER": "NONE"}
    original = campaign.brand_prompt
    marker = "\nCAMPAIGN INPUT\n"
    if marker not in original:
        raise ValueError("brand_prompt.md needs its CAMPAIGN INPUT section")
    start = original.index(marker) + len(marker)
    # Keep every word outside the input fields. The closing FINAL section is intact.
    end = original.find("\n======================================================================", start + 72)
    if end < 0:
        end = len(original)
    block = original[start:end]
    for name, value in values.items():
        pattern = r"(?m)^" + re.escape(name) + r":\n.*?(?=\n[A-Z][A-Z /-]+:\n|\Z)"
        replacement = name + ":\n" + value + "\n"
        block, count = re.subn(pattern, lambda match: replacement, block, count=1, flags=re.S)
        if count != 1:
            raise ValueError(f"Missing CAMPAIGN INPUT field: {name}")
    brand = original[:start] + block + original[end:]
    ratio = "9:16" if draft["kind"] == "story" else "1:1 square" if width == height else "4:5"
    override = (f"OUTPUT FORMAT FOR THIS REQUEST: one finished {width} × {height} {draft['kind']} ({ratio}). "
                "This size overrides generic post sizes elsewhere in the unchanged brand prompt. "
                "Use only the filled CAMPAIGN INPUT text and attached permitted assets. No watermarks.\n\n")
    if campaign.style == "clinic":
        override += ("IMPORTANT: Place the attached clinic logo on the design. Put the clinic phone number and address "
                     f"({campaign.phone} · {campaign.address}) small and quiet in a simple bottom band, exactly as written. "
                     "Do NOT add any disclaimer, 'general health information' or 'not medical advice' note, other fine print, "
                     "doctor name or credentials anywhere in the artwork. The disclaimer is published in the caption.\n\n")
    override += ("DESIGN RESTRAINT (most important): this must look like a calm, human-made Canva/Figma post, not an AI poster. "
                 "One headline, one short line, at most 3 short list items as plain text, and ONE visual. "
                 "NO icons, NO icon circles or coloured badges, NO icon grid, NO infographic, NO sparkles, NO glossy effects, "
                 "NO stock-photo person smiling at the sky. Do not add list items, labels or text beyond the CAMPAIGN INPUT. "
                 "Generous empty space; when unsure, remove elements.\n\n")
    return override + brand


def build_bundle(campaign, draft, references, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    files = []
    prompt = directory / "prompt.txt"
    prompt.write_text(filled_prompt(campaign, draft, references), encoding="utf-8", newline="\n")
    files.append(prompt)
    copy = Copy.model_validate_json(draft["copy"])
    assets = []
    if True:  # Every post carries the official logo.
        assets.append((campaign.asset(campaign.logo_path), "logo" + Path(campaign.logo_path).suffix))
    for index, source in enumerate(campaign.files(campaign.screenshots), 1):
        assets.append((source, f"screenshot_{index}{source.suffix}"))
    if copy.category == "INSTITUTIONAL" and copy.visual_kind == "premises" and campaign.premises_path:
        # Export the actual building, cropped to exclude the neighbouring signs.
        source = campaign.asset(campaign.premises_path)
        target = directory / "premises.jpg"
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            box = tuple(round(v * image.size[i % 2]) for i, v in enumerate(campaign.premises_crop))
            image.crop(box).convert("RGB").save(target, quality=95)
        files.append(target)
    for source, name in assets:
        target = directory / name
        shutil.copyfile(source, target)
        files.append(target)
    for index, reference in enumerate(references, 1):
        target = directory / f"style_reference_{index}.jpg"
        with Image.open(reference["path"]) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image.save(target, quality=95)
        files.append(target)
    archive = directory / f"{campaign.slug}-{draft['kind']}-v{draft['version']}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for file in files:
            bundle.write(file, file.name)
    return archive, files
