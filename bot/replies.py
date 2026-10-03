import re
from dataclasses import dataclass


@dataclass
class Reply:
    action: str
    photo: str = ""
    caption: str = ""
    feedback: str = ""


def parse_reply(text: str) -> Reply:
    text = text.strip()
    lowered = text.casefold().strip(" .!")
    if lowered in {"approve", "approved", "ok", "okay", "👍"}:
        return Reply("approve")
    if lowered in {"skip", "cancel"}:
        return Reply("skip")
    if re.match(r"^idea\s*[:\-]", text, re.I):
        return Reply("idea",feedback=re.sub(r"^idea\s*[:\-]\s*", "",text,flags=re.I))
    matches = list(re.finditer(r"(?im)^\s*(photo|caption)\s*[:\-]\s*", text))
    if matches:
        changes = {}
        for i, match in enumerate(matches):
            end = matches[i+1].start() if i+1 < len(matches) else len(text)
            changes[match.group(1).lower()] = text[match.end():end].strip()
        return Reply("revise",**changes)
    return Reply("revise",feedback=text)


def detect_campaign(text, campaigns):
    found = []
    for campaign in campaigns.values():
        names = set(campaign.aliases + [campaign.slug,campaign.name])
        if any(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)",text,re.I) for name in names):
            found.append(campaign.slug)
    return found[0] if len(found) == 1 else None
