from __future__ import annotations

import os
from pathlib import Path
from datetime import time
from zoneinfo import ZoneInfo
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parents[1]
LAYOUTS = {"type", "photo", "split", "phone", "circle", "blue", "numbered", "contact",
           "story_type", "story_photo", "story_panel"}


class Campaign(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    name: str
    wordmark: str = ""
    aliases: list[str] = Field(default_factory=list)
    enabled: bool = True
    style: Literal["lushnote", "clinic"]
    timezone: str
    language_rules: dict
    post_size: tuple[int, int]
    story_size: tuple[int, int]
    plan_time: str
    post_time: str
    story_time: str
    facebook_page_id: str
    instagram_account_id: str
    linkedin_org_urn: str = ""
    palette: dict[str, str]
    fonts: dict[str, str]
    logo_path: str
    premises_path: str = ""
    premises_crop: tuple[float, float, float, float] = (0, 0, 0.56, 1)
    screenshots: list[str] = Field(default_factory=list)
    reference_posts: list[str] = Field(default_factory=list)
    website: str = ""
    phone: str = ""
    address: str = ""
    content_mix: dict[str, int]
    rss_queries: list[str] = Field(default_factory=list)
    hashtag_rules: dict
    banned_phrases: list[str] = Field(default_factory=list)
    required_footer_rules: dict
    approved_facts: list[str] = Field(default_factory=list)
    feed_layouts: list[str]
    story_layouts: list[str]

    @field_validator("timezone")
    @classmethod
    def valid_zone(cls, value):
        ZoneInfo(value)
        return value

    @field_validator("plan_time", "post_time", "story_time")
    @classmethod
    def valid_time(cls, value):
        time.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def valid_assets_and_layouts(self):
        if len(self.feed_layouts) < 6 or len(self.story_layouts) < 3:
            raise ValueError("Campaigns need at least 6 feed and 3 story layouts")
        if any(x not in LAYOUTS for x in self.feed_layouts + self.story_layouts):
            raise ValueError("Unknown layout name")
        if not self.content_mix or any(v < 1 for v in self.content_mix.values()):
            raise ValueError("Content mix weights must be positive")
        if not self.language_rules.get("rotation") or not set(self.language_rules["rotation"]) <= {"en", "si", "ta"}:
            raise ValueError("Language rotation must use en, si or ta")
        for size in (self.post_size, self.story_size):
            if min(size) < 320 or max(size) > 2160:
                raise ValueError("Image dimensions must be between 320 and 2160")
        for colour in self.palette.values():
            import re
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour):
                raise ValueError("Palette must contain six-digit hex colours")
        return self

    def asset(self, path: str) -> Path:
        target = (ROOT / path).resolve()
        if not target.is_relative_to(ROOT):
            raise ValueError("Asset must be inside the project")
        return target

    def files(self, patterns: list[str]) -> list[Path]:
        files = []
        for pattern in patterns:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                raise ValueError("Asset pattern must be relative to the project")
            files.extend(sorted(ROOT.glob(pattern)))
        return files

    @property
    def brand_prompt(self):
        # Read at generation time so brand edits do not require code changes.
        return (ROOT / "campaigns" / self.slug / "brand_prompt.md").read_text(encoding="utf-8")


def load_secrets(path: Path | None = None) -> dict[str, str]:
    values = {}
    path = path or ROOT / "secrets.txt"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, sep, value = line.partition("=")
                if not sep:
                    raise ValueError("secrets.txt needs KEY=value on each line")
                value = value.split(" #", 1)[0].strip().strip('"').strip("'")
                values[key.strip()] = value
    for key in set(values) | {"DRY_RUN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_CHAT_ID", "GROQ_API_KEY", "GEMINI_API_KEY"}:
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def load_config():
    settings = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    secrets = load_secrets()
    if secrets.get("DRY_RUN"):
        if secrets["DRY_RUN"].lower() not in {"true", "false"}:
            raise ValueError("DRY_RUN must be true or false")
        settings["dry_run"] = secrets["DRY_RUN"].lower() == "true"
    campaigns = {}
    for folder in sorted((ROOT / "campaigns").iterdir()):
        if folder.is_dir() and (folder / "campaign.yaml").exists():
            campaign = Campaign(slug=folder.name, **yaml.safe_load((folder / "campaign.yaml").read_text(encoding="utf-8")))
            _ = campaign.brand_prompt
            if not campaign.asset(campaign.logo_path).is_file():
                raise ValueError(f"Missing logo for {campaign.name}")
            campaigns[campaign.slug] = campaign
    if not campaigns:
        raise ValueError("No campaigns found")
    return settings, campaigns, secrets
