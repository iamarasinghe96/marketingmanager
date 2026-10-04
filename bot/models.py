from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Hashtag characters: letters/digits plus Sinhala and Tamil vowel signs (which \\w alone misses).
# Joiners (ZWJ/ZWNJ) are excluded on purpose: those tags render inconsistently across platforms.
TAG_CHARS = r"\w\u0D80-\u0DFF\u0B80-\u0BFF"
TAG = r"#[" + TAG_CHARS + r"]+(?:[\u200c\u200d][" + TAG_CHARS + r"]+)*"


class Copy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["MARKETING", "EDUCATIONAL", "INSTITUTIONAL"]
    language: Literal["en", "si", "ta"]
    topic: str = Field(min_length=2, max_length=160)
    headline: str = Field(min_length=2, max_length=160)
    supporting: str = Field(default="", max_length=280)
    body: str = Field(default="", max_length=280)
    items: list[str] = Field(default_factory=list, max_length=3)
    cta: str = Field(default="", max_length=70)
    caption: str = Field(min_length=2, max_length=1700)
    hashtags: list[str] = Field(default_factory=list, max_length=12)
    visual_brief: str = Field(min_length=2, max_length=1200)
    visual_kind: Literal["photo", "illustration", "screenshot", "premises", "none"]
    screenshot_index: int = Field(default=0, ge=0)
    purpose: str = Field(default="", max_length=400)
    url: str = Field(default="", max_length=200)
    special_requirements: str = Field(default="", max_length=1800)
    changes: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="before")
    @classmethod
    def hashtags_out_of_caption(cls, data):
        """Models often put hashtags inside the caption; move them to the hashtag list."""
        import re
        if isinstance(data, dict) and isinstance(data.get("caption"), str) and "#" in data["caption"]:
            found = re.findall(TAG, data["caption"])
            data = {**data, "caption": re.sub(r"[ \t]*" + TAG, "", data["caption"]).strip(),
                    "hashtags": list(data.get("hashtags") or []) + found}
        return data

    @field_validator("items")
    @classmethod
    def short_items(cls, value):
        if any(len(x) > 85 for x in value):
            raise ValueError("List items must be short")
        return value

    @field_validator("hashtags", mode="before")
    @classmethod
    def valid_tags(cls, value):
        """Repair what models usually get wrong (missing #, spaces, punctuation).

        Tags that need invisible joiners (common in Sinhala) are dropped: they break across platforms.
        """
        import re
        if isinstance(value, str):
            value = re.split(r"[\s,]+", value)
        tags = []
        for tag in value or []:
            tag = str(tag).strip()
            if not tag or re.search(r"[\u200c\u200d]", tag):
                continue
            tag = "#" + re.sub(r"[^" + TAG_CHARS + "]", "", tag.lstrip("#"))
            if re.fullmatch(TAG, tag):
                tags.append(tag)
        return list(dict.fromkeys(tags))

    @property
    def full_caption(self):
        return self.caption + ("\n\n" + " ".join(self.hashtags) if self.hashtags else "")


class ComplianceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class MessageIntent(BaseModel):
    action: Literal["idea","revision"]


class ReferenceGuidance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str = Field(max_length=1800)
    changes: list[str] = Field(default_factory=list, max_length=8)
