import re
from dataclasses import dataclass


@dataclass
class Reply:
    action: str
    photo: str = ""
    caption: str = ""
    feedback: str = ""
    headline: str = ""
    visual: str = ""


def parse_reply(text: str) -> Reply:
    text = text.strip()
    lowered = text.casefold().strip(" .!")
    if lowered in {"approve", "approved", "ok", "okay", "👍"}:
        return Reply("approve")
    if lowered in {"skip", "cancel"}:
        return Reply("skip")
    if re.match(r"^idea\s*[:\-]", text, re.I):
        return Reply("idea",feedback=re.sub(r"^idea\s*[:\-]\s*", "",text,flags=re.I))
    matches = list(re.finditer(r"(?im)^\s*(photo|caption|headline|visual)\s*[:\-]\s*", text))
    if matches:
        changes = {}
        for i, match in enumerate(matches):
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            changes[match.group(1).lower()] = text[match.end():end].strip()
        return Reply("revise",**changes)
    return Reply("revise",feedback=text)


def detect_campaign(text, campaigns):
    found = []
    explicit = []
    generic = {"clinic", "allergy", "asthma", "colombo"}
    for campaign in campaigns.values():
        names = set(campaign.aliases + [campaign.slug,campaign.name])
        matched = [name for name in names if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)",text,re.I)]
        if matched:
            found.append(campaign.slug)
        if any(name.casefold() not in generic for name in matched):
            explicit.append(campaign.slug)
    if len(explicit) == 1 and not re.search(r"\b(?:and|or)\s+(?:clinic|allergy|asthma|colombo)\b",text,re.I):
        return explicit[0]
    return found[0] if len(found) == 1 else None


def detect_formats(text):
    if re.search(r"\bboth\b|\bpost\s*(?:and|&|\+)\s*story\b", text, re.I):
        return ["post", "story"]
    post = bool(re.search(r"\bposts?\b|\bfeed\b", text, re.I))
    story = bool(re.search(r"\bstor(?:y|ies)\b", text, re.I))
    return (["post"] if post else []) + (["story"] if story else [])
