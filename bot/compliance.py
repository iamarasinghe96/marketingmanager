import re
import unicodedata
from bot.models import Copy

UNAVAILABLE = "Medical compliance service unavailable. Revise/retry before approval."


def normalise(text):
    text = unicodedata.normalize("NFKC",text).casefold()
    text = re.sub(r"[\u200b-\u200f\ufeff]", "",text)
    return re.sub(r"[^\w%]+", " ",text,flags=re.UNICODE).strip()


def banned_matches(text, phrases):
    normal = normalise(text)
    compact = normal.replace(" ", "")
    return [p for p in phrases if normalise(p) in normal or normalise(p).replace(" ", "") in compact]


def deterministic_check(campaign, copy: Copy):
    reasons = []
    supported = campaign.language_rules.get("supported",campaign.language_rules["rotation"])
    if copy.language not in supported:
        reasons.append("Language is not enabled for this campaign")
    on_image = " ".join([copy.headline,copy.supporting,copy.body,copy.cta,*copy.items])
    text = " ".join([on_image,copy.full_caption,copy.visual_brief,copy.special_requirements,copy.purpose,copy.url])
    if copy.url and copy.url != campaign.website:
        reasons.append("URL must match the supplied campaign website, or be empty")
    hits = banned_matches(text,campaign.banned_phrases)
    if hits:
        reasons.append("Banned wording: " + ", ".join(hits))
    if campaign.language_rules.get("no_em_dash") and "—" in text:
        reasons.append("Em dashes are not allowed")
    if "#" in on_image or "#" in copy.caption:
        reasons.append("Hashtags must be in the separate caption hashtag list")
    if len(copy.hashtags) > campaign.hashtag_rules["max_count"]:
        reasons.append("Too many hashtags")
    if campaign.style == "lushnote":
        if copy.category != "MARKETING":
            reasons.append("LushNote must use the MARKETING category")
        # New copy is prompted to end with a full stop. Exact owner wording wins.
    else:
        if copy.category == "MARKETING":
            reasons.append("Clinic content must be EDUCATIONAL or INSTITUTIONAL")
        if re.search(r"\b\d+(?:[.,]\d+)?\s*%|\b(?:study|studies|research)\s+(?:shows|proves)",text,re.I):
            reasons.append("Statistics or research claims need supplied evidence")
        if copy.category == "EDUCATIONAL":
            # Owner decision: the clinic name, phone and address may appear on every post.
            # Booking pitches, prices, web addresses and emails stay out of educational posts.
            allowed = [campaign.name,"Allergy & Asthma Centre",campaign.phone,campaign.address,
                       re.sub(r"\D","",campaign.phone or "")]
            checked = text
            for item in allowed:
                if item:
                    checked = re.sub(re.escape(item),"",checked,flags=re.I)
            identifiers = ["book an appointment", "book now", "book your", "WhatsApp"]
            if any(normalise(x) in normalise(checked) for x in identifiers):
                reasons.append("Educational copy contains booking promotion")
            if re.search(r"https?://|www\.|\b(?:LKR|Rs\.?)\s*\d|\b\d{9,12}\b|[\w.+-]+@[\w.-]+",checked,re.I):
                reasons.append("Educational copy contains a website, contact or price")
            if copy.cta or copy.visual_kind == "premises":
                reasons.append("Educational posts cannot use clinic CTAs or premises")
        elif copy.category == "INSTITUTIONAL":
            if re.search(r"\b(?:dr\.?|doctor|MBBS|MRCP|MRCPCH|DCH|MD|paediatrician|allergist)\b|වෛද්‍ය|மருத்துவர்",text,re.I):
                reasons.append("Institutional copy must not identify a doctor, qualifications or title")
        if copy.visual_kind == "screenshot":
            reasons.append("Clinic content cannot use app screenshots")
    if copy.visual_kind == "premises" and not campaign.premises_path:
        reasons.append("No premises photograph supplied")
    if copy.visual_kind == "screenshot" and not campaign.files(campaign.screenshots):
        reasons.append("No screenshots supplied")
    return reasons


async def check(campaign, copy, text_client):
    reasons = deterministic_check(campaign,copy)
    if campaign.style == "clinic":
        try:
            verdict = await text_client.compliance(campaign,copy)
            if not verdict.passed:
                reasons.extend(verdict.reasons or ["LLM medical compliance check did not pass"])
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Compliance check unavailable: %s",str(exc)[:500])
            reasons.append(UNAVAILABLE)
    return list(dict.fromkeys(reasons))
