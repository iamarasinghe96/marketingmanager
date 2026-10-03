"""Content approval, style references and finished-artwork handoff."""
import json
import re
import uuid
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageOps
from pydantic import ValidationError
from bot.bundles import reference_requirements,style_instructions
from bot.config import ROOT
from bot.db import stamp, utcnow
from bot.compliance import check, banned_matches
from bot.models import Copy
from bot.scheduling import local_day, choose_layouts, slots_for


class ContentFlow:
    def __init__(self, pipeline):
        self.pipeline = pipeline
        self.store, self.telegram, self.text = pipeline.store, pipeline.telegram, pipeline.text

    def references(self, draft):
        rows = self.store.rows("SELECT path,kind FROM assets WHERE draft_id=? AND kind LIKE 'style_reference_%' ORDER BY id", (draft["id"],))
        return [{"path": row["path"], "description": self.store.get("reference_description:" + row["path"], "")} for row in rows]

    def free_day(self, campaign, start):
        for offset in range(367):
            day = start + timedelta(days=offset)
            if not self.store.one("SELECT id FROM drafts WHERE campaign=? AND day=?", (campaign.slug, day.isoformat())):
                return day
        raise ValueError("No free day in the next year")

    async def create(self, slug, text, kinds, references=()):
        campaign = self.pipeline.campaigns[slug]
        if not campaign.enabled or self.store.paused(slug):
            raise ValueError(f"{campaign.name} is paused. Use /resume first.")
        today = local_day(campaign, utcnow())
        day = self.free_day(campaign, today)
        group = uuid.uuid4().hex[:12]
        # Reserve all selected slots before the first network await.
        drafts = [self.pipeline.reserve(campaign, day, kind, text, "finished", group) for kind in kinds]
        for draft in drafts:
            for index, path in enumerate(references, 1):
                self.store.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,?)",
                                   (draft["id"], 0, f"style_reference_{index}", str(path), stamp()))
        await self.telegram.send(f"Your idea: {campaign.name} · {' / '.join(k.title() for k in kinds)} · {day:%a %d %b %Y} ({campaign.timezone})." +
                                 (" Today's slot is occupied. Use today instead will move the existing drafts to the next free day." if day != today else ""))
        for path in references:
            try:
                description = await self.text.describe_reference(path)
            except Exception:
                description = ""
            self.store.set("reference_description:" + str(path), description)
        for draft in drafts:
            try:
                await self.prepare(draft)
            except Exception as exc:
                await self.failed(self.store.draft(draft["id"]),exc)
        return drafts

    async def failed(self,draft,error):
        if draft["state"] != "failed":
            self.store.transition(draft["id"],"failed",error=str(error))
        prefix = f"c:{draft['id']}:{draft['version']}:"
        message = await self.telegram.send(f"Could not prepare content for {draft['campaign']} {draft['kind']} ({draft['id']}): {error}. Fix the text key/quota, then use Rewrite to try the same reserved slot again.",
                                            [[{"text":"Rewrite","callback_data":prefix+"rewrite"},{"text":"New idea","callback_data":prefix+"new"}]])
        self.telegram.map_message(message["message_id"],draft)

    async def prepare(self, draft, revision=None):
        self.pipeline.ensure_revisable(draft)
        campaign = self.pipeline.campaigns[draft["campaign"]]
        history = self.pipeline.history(draft)
        previous = json.loads(draft["copy"]) if draft["copy"] else None
        layouts = ((draft["feed_layout"], draft["story_layout"], draft["feed_variant"], draft["story_variant"])
                   if previous else choose_layouts(campaign, history))
        if draft["state"] != "generating":
            self.store.transition(draft["id"], "generating", approved_version=None, content_approved_version=None)
        references = self.references(draft)
        context = dict(revision or {})
        context["style_reference_guidance"] = reference_requirements(campaign, references)
        context["format"] = draft["kind"]
        correction, copy, reasons = "", None, []
        warnings = []
        original_bans = banned_matches(draft["idea"], campaign.banned_phrases)
        if original_bans:
            warnings.append("Changed the idea's prohibited wording (" + ", ".join(original_bans) + ") to neutral, compliant wording. The brand rules prohibit guarantees, cure claims, fear-based targeting and unsupported claims.")
        exact = {}
        for match in re.finditer(r"(?im)^\s*(headline|supporting copy|subhead|body|caption|cta|url)\s*:\s*(.+)$",draft["idea"]):
            key,value = match.group(1).lower(),match.group(2).strip()
            if len(value)>1 and (value[0],value[-1]) in {('"','"'),("'","'"),('“','”')}:
                value = value[1:-1]
            exact[{"supporting copy":"supporting","subhead":"supporting"}.get(key,key)] = value
        if revision and revision.get("headline"):
            exact["headline"] = revision["headline"]
        if revision and revision.get("visual"):
            exact["visual_brief"] = revision["visual"]
        for attempt in range(3):
            copy = await self.text.write(campaign, date.fromisoformat(draft["day"]), history, draft["idea"], context, previous, correction)
            # Enforce exact text locally when safe, rather than trusting model memory.
            if exact:
                try:
                    candidate = Copy.model_validate({**copy.model_dump(), **exact})
                    exact_failures = await check(campaign, candidate, self.text)
                except ValidationError:
                    exact_failures = ["Supplied exact text exceeds the readable campaign field limits"]
                if not exact_failures:
                    copy = candidate
                else:
                    warnings.append("Rewrote supplied exact text to comply: " + "; ".join(exact_failures))
                    for key, original in exact.items():
                        if original != getattr(copy, key):
                            warnings.append(f"{key}: ‘{original}’ → ‘{getattr(copy, key)}’")
            if previous and revision and revision.get("caption") and not any(revision.get(key) for key in ("feedback", "headline", "visual", "photo")):
                if not re.search(r"on[ -]?image|headline|artwork|supporting|graphic", revision["caption"], re.I):
                    copy = Copy.model_validate({**previous, "caption": copy.caption, "hashtags": copy.hashtags, "changes": copy.changes})
            if campaign.style == "clinic" and (copy.category == "EDUCATIONAL" or not campaign.website) and copy.url:
                warnings.append(f"Removed URL ‘{copy.url}’: educational posts cannot promote clinic contacts, and institutional URLs must be explicitly configured.")
                copy = copy.model_copy(update={"url": ""})
            reasons = await check(campaign, copy, self.text)
            if not reasons:
                break
            correction = "Correct these failures, and explain every change to owner wording: " + "; ".join(reasons)
        warnings.extend(copy.changes)
        directions = style_instructions(draft["idea"]) if references else ""
        if directions:
            try:
                guidance = await self.text.reference_guidance(campaign,copy,directions)
                revised = copy.model_copy(update={"special_requirements":"\n".join(x for x in (copy.special_requirements,guidance.instructions) if x)})
                direction_failures = await check(campaign,revised,self.text)
                reasons.extend(direction_failures)
                self.store.set("reference_instructions:"+draft["id"],guidance.instructions)
                warnings.extend(guidance.changes)
                if guidance.instructions != directions and not guidance.changes:
                    warnings.append(f"Reference directions: ‘{directions}’ → ‘{guidance.instructions}’. Adjusted to meet the campaign's brand and medical rules.")
            except Exception:
                reasons.append("Reference instruction compliance could not be checked. Rewrite/retry before approval.")
        self.store.execute("UPDATE image_requests SET status='superseded' WHERE draft_id=? AND status='pending'", (draft["id"],))
        draft = self.store.save_version(draft["id"], copy, None, None, layouts, reasons, list(dict.fromkeys(warnings)), awaiting_content=True)
        await self.telegram.content_review(campaign, draft)
        self.store.set("content_sent:" + draft["id"], draft["version"])
        return draft

    async def approve(self, draft):
        if draft["state"] != "awaiting_content":
            raise ValueError("Use the newest content review card")
        campaign = self.pipeline.campaigns[draft["campaign"]]
        if local_day(campaign, utcnow()).isoformat() > draft["day"]:
            raise ValueError("This draft expired at local midnight")
        reasons = json.loads(draft["compliance"]) + await check(campaign, Copy.model_validate_json(draft["copy"]), self.text)
        if reasons:
            raise ValueError("Content approval blocked. Rewrite first: " + "; ".join(dict.fromkeys(reasons)))
        self.store.transition(draft["id"], "awaiting_image", content_approved_version=draft["version"])
        return await self.request(self.store.draft(draft["id"]))

    async def request(self, draft):
        campaign = self.pipeline.campaigns[draft["campaign"]]
        if draft["content_approved_version"] != draft["version"]:
            raise ValueError("Approve the content before requesting finished artwork")
        return await self.pipeline.images.request(campaign,draft)

    async def receive(self, draft, path):
        self.pipeline.ensure_revisable(draft)
        if draft["state"] not in {"awaiting_image", "in_review", "approved", "failed"} or draft["content_approved_version"] != draft["version"]:
            raise ValueError("Approve content and reply to its handoff before sending finished artwork")
        campaign = self.pipeline.campaigns[draft["campaign"]]
        copy = Copy.model_validate_json(draft["copy"])
        target = ROOT / "out" / "drafts" / draft["id"] / f"v{draft['version']+1}" / (draft["kind"] + ".png")
        target.parent.mkdir(parents=True, exist_ok=True)
        size = campaign.story_size if draft["kind"] == "story" else campaign.post_size
        warnings = json.loads(draft["warnings"])
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            if abs(image.width / image.height - size[0] / size[1]) > 0.02:
                raise ValueError("Finished artwork has the wrong aspect ratio. Send " + ("9:16" if draft["kind"] == "story" else "1:1 square" if size[0] == size[1] else "4:5") + "; the original has been saved.")
            image = image.resize(size, Image.Resampling.LANCZOS)
            image.save(target)
        reasons = await check(campaign, copy, self.text)
        request = self.store.one("SELECT id FROM image_requests WHERE draft_id=? AND status='pending'",(draft["id"],))
        if request:
            await self.pipeline.images.submit(request["id"],path)
        else:
            self.store.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,?)",
                               (draft["id"], draft["version"], "finished_original", str(path), stamp()))
        self.store.transition(draft["id"], "generating", approved_version=None)
        layouts = (draft["feed_layout"], draft["story_layout"], draft["feed_variant"], draft["story_variant"])
        draft = self.store.save_version(draft["id"], copy, target if draft["kind"] == "post" else None, target if draft["kind"] == "story" else None, layouts, reasons, warnings)
        self.store.execute("UPDATE drafts SET content_approved_version=version WHERE id=?", (draft["id"],))
        draft = self.store.draft(draft["id"])
        await self.telegram.review(campaign, draft)
        self.store.set("review_sent:" + draft["id"], draft["version"])
        return draft

    async def use_today(self, draft):
        campaign = self.pipeline.campaigns[draft["campaign"]]
        today = local_day(campaign, utcnow())
        if draft["day"] == today.isoformat():
            raise ValueError("This idea is already scheduled for today")
        group = self.store.rows("SELECT * FROM drafts WHERE idea_group=?", (draft["idea_group"],)) if draft["idea_group"] else [draft]
        existing = self.store.rows("SELECT * FROM drafts WHERE campaign=? AND day=?", (campaign.slug, today.isoformat()))
        for row in group + existing:
            self.pipeline.ensure_revisable(row,clear_prepared=False)
        # Moving the idea releases its old day; that day can accept today's draft.
        free = today + timedelta(days=1)
        for _ in range(367):
            occupants = self.store.rows("SELECT id FROM drafts WHERE campaign=? AND day=?",(campaign.slug,free.isoformat()))
            if all(row["id"] in {item["id"] for item in group} for row in occupants):
                break
            free += timedelta(days=1)
        else:
            raise ValueError("No free day in the next year")
        moved = []
        with self.store.transaction():
            # Vacate the idea day first, so a swap obeys the UNIQUE slot constraint.
            for row in group:
                self.store.conn.execute("UPDATE drafts SET day=? WHERE id=?",("moving:"+row["id"],row["id"]))
            for rows, day in ((existing, free), (group, today)):
                post, story = slots_for(self.pipeline.campaigns, campaign, day, self.pipeline.settings["minimum_publish_gap_minutes"])
                for row in rows:
                    due = post if row["kind"] == "post" else story
                    self.store.conn.execute("UPDATE drafts SET day=?,reminded=0,updated_at=? WHERE id=?", (day.isoformat(), stamp(), row["id"]))
                    self.store.conn.execute("UPDATE schedules SET due_at=? WHERE draft_id=?", (due.isoformat(), row["id"]))
                    self.store.conn.execute("UPDATE ideas_queue SET day=? WHERE draft_id=?", (day.isoformat(), row["id"]))
                    self.store.conn.execute("UPDATE history SET day=? WHERE draft_id=?", (day.isoformat(), row["id"]))
                    self.store.conn.execute("UPDATE image_requests SET reminded=0 WHERE draft_id=? AND status='pending'", (row["id"],))
                    moved.append(self.store.draft(row["id"]))
        await self.telegram.send(f"{campaign.name}: your idea is now scheduled for {today:%a %d %b %Y}." +
                                 (f" Existing drafts moved to {free:%a %d %b %Y}." if existing else "") + " Approvals stay with their current content; today's passed slots publish only after final approval.")
        for row in moved:
            if row["state"] == "awaiting_content":
                await self.telegram.content_review(campaign, row)
            elif row["state"] == "in_review":
                await self.telegram.review(campaign, row)
        return moved
