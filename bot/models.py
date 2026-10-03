from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Copy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["MARKETING", "EDUCATIONAL", "INSTITUTIONAL"]
    language: Literal["en", "si", "ta"]
    topic: str = Field(min_length=2, max_length=160)
    headline: str = Field(min_length=2, max_length=160)
    supporting: str = Field(default="", max_length=280)
    items: list[str] = Field(default_factory=list, max_length=3)
    cta: str = Field(default="", max_length=70)
    caption: str = Field(min_length=2, max_length=1700)
    hashtags: list[str] = Field(default_factory=list, max_length=12)
    visual_brief: str = Field(min_length=2, max_length=1200)
    visual_kind: Literal["photo", "illustration", "screenshot", "premises", "none"]
    screenshot_index: int = Field(default=0, ge=0)

    @field_validator("items")
    @classmethod
    def short_items(cls, value):
        if any(len(x) > 85 for x in value):
            raise ValueError("List items must be short")
        return value

    @field_validator("hashtags")
    @classmethod
    def valid_tags(cls, value):
        import re
        if any(not re.fullmatch(r"#[\w]+", x, re.UNICODE) for x in value):
            raise ValueError("Hashtags need # and letters/numbers only, no joiners")
        return list(dict.fromkeys(value))

    @property
    def full_caption(self):
        return self.caption + ("\n\n" + " ".join(self.hashtags) if self.hashtags else "")


class ComplianceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class MessageIntent(BaseModel):
    action: Literal["idea","revision"]
