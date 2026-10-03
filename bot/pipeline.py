from __future__ import annotations
import json
import re
from datetime import date, timedelta
from pathlib import Path

from bot.config import ROOT
from bot.db import utcnow
from bot.models import Copy
from bot.compliance import check
from bot.scheduling import choose_layouts,local_day,slots_for,next_free_day
from bot.render import rendering_browser,render_one,select_visual,TextOverflow


class Pipeline:
    def __init__(self,settings,campaigns,store,text,images,telegram,meta,linkedin):
        self.settings,self.campaigns,self.store = settings,campaigns,store
        self.text,self.images,self.telegram,self.meta,self.linkedin = text,images,telegram,meta,linkedin
        from bot.content import ContentFlow
        self.content = ContentFlow(self)

    def reserve(self,campaign,day,kind,idea="",delivery_mode="visual",idea_group=None):
        post,story = slots_for(self.campaigns,campaign,day,self.settings["minimum_publish_gap_minutes"])
        return self.store.reserve_draft(campaign.slug,day,kind,post if kind == "post" else story,idea,delivery_mode,idea_group)

    async def generate_now(self,slug,idea="",image=None,delivery_mode="finished"):
        c = self.campaigns[slug]
        if not c.enabled or self.store.paused(slug):
            await self.telegram.send(f"{c.name} is paused or disabled. Use /resume first.")
            return
        day = next_free_day(self.store,c,utcnow(),self.campaigns,self.settings["minimum_publish_gap_minutes"])
        await self.telegram.send(f"Preparing {c.name} for {day:%a %d %b %Y} ({c.timezone}).")
        for kind in ("post","story"):
            draft = self.reserve(c,day,kind,idea,delivery_mode)
            await self.prepare(draft,image=image)

    def history(self,draft):
        cutoff = (date.fromisoformat(draft["day"])-timedelta(days=30)).isoformat()
        return self.store.rows("SELECT h.* FROM history h JOIN drafts d ON d.id=h.draft_id WHERE h.campaign=? AND h.day>=? AND h.draft_id<>? AND d.kind=? ORDER BY h.day DESC,h.rowid DESC LIMIT 30",
                               (draft["campaign"],cutoff,draft["id"],draft["kind"]))

    def ensure_revisable(self,draft,clear_prepared=True):
        if draft["state"] in {"published","publishing","skipped"}:
            raise ValueError("This draft can no longer be revised")
        if self.store.one("SELECT idempotency_key FROM publications WHERE draft_id=? AND status IN ('sending','unknown','completed') AND idempotency_key LIKE 'live:%'",(draft["id"],)):
            raise ValueError("Publishing has already begun. Resolve/retry the existing publication first.")
        # Disposable uploads/containers from known rejected attempts refer to old
        # artwork. Clear them when revising, so a retry cannot publish stale media.
        if clear_prepared:
            self.store.execute("UPDATE publications SET status='reserved',stage=NULL,remote_id=NULL,container_id=NULL,upload_id=NULL,permalink=NULL,error=NULL WHERE draft_id=? AND status IN ('reserved','prepared','failed')",(draft["id"],))

    async def prepare(self,draft,revision=None,image=None):
        if draft.get("delivery_mode") == "finished":
            return await self.content.prepare(draft,revision)
        self.ensure_revisable(draft)
        c = self.campaigns[draft["campaign"]]
        history = self.history(draft)
        layouts = choose_layouts(c,history)
        previous = json.loads(draft["copy"]) if draft["copy"] else None
        if previous and revision:
            layouts = (draft["feed_layout"],draft["story_layout"],draft["feed_variant"],draft["story_variant"])
        if draft["state"] != "generating":
            self.store.transition(draft["id"],"generating",approved_version=None)
        correction = ""
        copy = None
        reasons = []
        for attempt in range(3):
            try:
                copy = await self.text.write(c,date.fromisoformat(draft["day"]),history,draft["idea"],revision,previous,correction)
                # Caption-only feedback must not silently alter supplied on-image wording.
                if previous and revision and revision.get("caption") and not revision.get("feedback"):
                    change_art = re.search(r"on[ -]?image|headline|artwork|supporting|graphic",revision["caption"],re.I)
                    if not change_art:
                        copy = Copy.model_validate({**previous,"caption":copy.caption,"hashtags":copy.hashtags})
                reasons = await check(c,copy,self.text)
                if not revision and any(h["headline"].casefold() == copy.headline.casefold() or h["topic"].casefold() == copy.topic.casefold() for h in history):
                    reasons.append("Headline or topic already used in the last 30 days")
                if not reasons:
                    break
                correction = "Correct these failures: " + "; ".join(reasons)
            except Exception:
                if attempt == 2:
                    if copy is None:
                        raise
                    reasons.append("Text regeneration failed; review/revise before approval")
        pending_images = self.store.rows("SELECT path FROM incoming_images WHERE campaign=? AND draft_id IS NULL ORDER BY created_at LIMIT 1",(c.slug,))
        incoming = image or (Path(pending_images[0]["path"]) if pending_images else None)
        if incoming:
            self.store.execute("UPDATE incoming_images SET draft_id=? WHERE path=?",(draft["id"],str(incoming)))
            copy = copy.model_copy(update={"visual_kind":"photo"})
        elif previous and revision and not revision.get("photo"):
            asset = self.store.one("SELECT path FROM assets WHERE draft_id=? AND kind LIKE 'original_%' ORDER BY id DESC LIMIT 1",(draft["id"],))
            incoming = Path(asset["path"]) if asset else None
        warnings = []
        draft = self.store.save_version(draft["id"],copy,None,None,layouts,reasons,warnings,awaiting_image=True)
        if incoming:
            self.store.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,datetime('now'))",
                               (draft["id"],draft["version"],"original_"+draft["kind"],str(incoming)))
            await self.compose(draft,incoming)
        elif previous and revision and not revision.get("photo") and previous["visual_kind"] in {"none","screenshot","premises"}:
            await self.compose(draft)
        else:
            await self.images.request(c,draft,(revision or {}).get("photo",""))
        return self.store.draft(draft["id"])

    async def new_image(self,draft,instructions=""):
        if draft.get("delivery_mode") == "finished":
            if instructions:
                return await self.content.prepare(draft,{"visual":instructions})
            return await self.content.request(draft)
        self.ensure_revisable(draft)
        if draft["state"] not in {"in_review","approved","awaiting_image","failed"}:
            raise ValueError("Only an unpublished draft can be revised")
        if self.store.one("SELECT idempotency_key FROM publications WHERE draft_id=? AND status IN ('sending','unknown','completed') AND idempotency_key LIKE 'live:%'",(draft["id"],)):
            raise ValueError("This draft has already begun publishing. Resolve or retry its existing publication first.")
        self.store.transition(draft["id"],"generating",approved_version=None)
        copy = Copy.model_validate_json(draft["copy"])
        layouts = (draft["feed_layout"],draft["story_layout"],draft["feed_variant"],draft["story_variant"])
        draft = self.store.save_version(draft["id"],copy,None,None,layouts,json.loads(draft["compliance"]),[],awaiting_image=True)
        await self.images.request(self.campaigns[draft["campaign"]],draft,instructions)

    async def compose(self,draft,image=None,skip_image=False):
        self.ensure_revisable(draft)
        c = self.campaigns[draft["campaign"]]
        if draft["state"] not in {"awaiting_image","in_review","approved","failed","generating"}:
            raise ValueError("This draft can no longer be changed")
        if draft["state"] != "generating":
            self.store.transition(draft["id"],"generating",approved_version=None)
        copy = Copy.model_validate_json(draft["copy"])
        warnings = json.loads(draft["warnings"])
        if image:
            copy = copy.model_copy(update={"visual_kind":"photo"})
        elif skip_image:
            screenshots = c.files(c.screenshots)
            copy = copy.model_copy(update={"visual_kind":"screenshot" if screenshots else "none"})
            warnings.append("Image skipped. Used a supplied screenshot or typography layout.")
        out_dir = ROOT/"out"/"drafts"/draft["id"]/f"v{draft['version']+1}"
        out_dir.mkdir(parents=True,exist_ok=True)
        visual = select_visual(c,copy,image,out_dir)
        kind = draft["kind"]
        layout = draft["feed_layout"] if kind == "post" else draft["story_layout"]
        variant = draft["feed_variant"] if kind == "post" else draft["story_variant"]
        if skip_image and copy.visual_kind == "screenshot" and kind == "post":
            if layout in {"type","blue"}:
                layout = "phone" if self.history(draft) and self.history(draft)[0]["feed_layout"] != "phone" else "split"
        if image and layout in {"type","blue","story_type"}:
            layout = "photo" if kind == "post" else "story_photo"
            hist = self.history(draft)
            if hist and hist[0]["feed_layout" if kind == "post" else "story_layout"] == layout:
                layout = "split" if kind == "post" else "story_panel"
        target = out_dir/f"{kind}.png"
        layouts = (layout if kind == "post" else draft["feed_layout"],layout if kind == "story" else draft["story_layout"],draft["feed_variant"],draft["story_variant"])
        try:
            async with rendering_browser() as browser:
                await render_one(browser,c,copy,layout,variant,visual,target,kind)
        except TextOverflow:
            self.store.transition(draft["id"],"failed",error="Text too long for a readable layout")
            await self.telegram.send(f"Draft {draft['id']}: text does not fit at a readable size. Reply Caption: shorten on-image text too, or use /generate again.")
            return
        reasons = list(dict.fromkeys(json.loads(draft["compliance"]) + await check(c,copy,self.text)))
        new_draft = self.store.save_version(draft["id"],copy,target if kind == "post" else None,target if kind == "story" else None,layouts,reasons,warnings)
        if draft.get("delivery_mode") == "finished":
            self.store.execute("UPDATE drafts SET content_approved_version=version WHERE id=?",(draft["id"],))
        self.store.execute("UPDATE image_requests SET status='superseded' WHERE draft_id=? AND status='pending'",(draft["id"],))
        await self.telegram.review(c,new_draft)
        self.store.set("review_sent:" + draft["id"],new_draft["version"])

    async def approve(self,draft,immediate=False):
        if draft["state"] not in {"in_review","failed"} or not draft["copy"]:
            raise ValueError("Compose and review this draft before approving")
        c = self.campaigns[draft["campaign"]]
        if local_day(c,utcnow()).isoformat() > draft["day"]:
            await self.skip(draft)
            raise ValueError("This draft expired at local midnight")
        path = draft["post_path"] if draft["kind"] == "post" else draft["story_path"]
        if not path or not Path(path).is_file():
            raise ValueError("This draft needs an image or Skip image before approval")
        if json.loads(draft["compliance"]):
            raise ValueError("Approval blocked. Revise the flagged copy first: " + "; ".join(json.loads(draft["compliance"])))
        reasons = await check(c,Copy.model_validate_json(draft["copy"]),self.text)
        if reasons:
            raise ValueError("Approval blocked: " + "; ".join(reasons))
        self.store.transition(draft["id"],"approved",approved_version=draft["version"],immediate=int(immediate))
        await self.telegram.send(f"Approved {c.name} {draft['kind']} ({draft['id']}). " + ("DRY_RUN: publishing will be simulated." if self.settings["dry_run"] else "It will publish at its scheduled time, or now if today's time has passed."))

    async def skip(self,draft):
        if draft["state"] in {"published","skipped"}:
            return
        self.store.transition(draft["id"],"skipped")
        self.store.execute("UPDATE schedules SET status='skipped' WHERE draft_id=? AND status='pending'",(draft["id"],))
        self.store.execute("UPDATE image_requests SET status='expired' WHERE draft_id=? AND status='pending'",(draft["id"],))

    async def publish(self,draft):
        c = self.campaigns[draft["campaign"]]
        if self.store.paused(c.slug) or not c.enabled:
            return
        if draft["approved_version"] != draft["version"]:
            raise ValueError("Current draft version is not approved")
        if draft["state"] not in {"approved","publishing","failed"}:
            return
        if not self.settings["dry_run"]:
            reasons = await check(c,Copy.model_validate_json(draft["copy"]),self.text)
            if reasons:
                if draft["state"] != "failed":
                    self.store.transition(draft["id"],"failed",error="Compliance failed before publishing: " + "; ".join(reasons))
                self.store.notify("publish_compliance:"+draft["id"],f"Draft {draft['id']} was not published. Current brand/compliance checks failed: "+"; ".join(reasons))
                return
        if draft["state"] != "publishing":
            self.store.transition(draft["id"],"publishing")
        copy = Copy.model_validate_json(draft["copy"])
        path = draft["post_path"] if draft["kind"] == "post" else draft["story_path"]
        platforms = ["facebook","instagram"]
        if draft["kind"] == "post" and self.settings["linkedin"]["enabled"] and c.linkedin_org_urn:
            platforms.append("linkedin")
        failures = []
        for platform in platforms:
            record = self.store.reserve_publication(draft["id"],platform,draft["kind"],self.settings["dry_run"])
            if record["status"] == "completed":
                continue
            try:
                if self.settings["dry_run"]:
                    self.store.publication_update(record["idempotency_key"],status="completed",permalink=None,remote_id="DRY_RUN")
                    confirmation = f"DRY_RUN: would publish {c.name} {draft['kind']} on {platform}. No publication API was called."
                else:
                    adapter = self.linkedin if platform == "linkedin" else self.meta
                    _,link = await adapter.publish(c,draft,record,path,copy.full_caption)
                    confirmation = f"Published {c.name} {draft['kind']} on {platform}. " + (link or "Story/link receipt saved; a public permalink is unavailable.")
                self.store.notify("publication:" + record["idempotency_key"],confirmation)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            self.store.transition(draft["id"],"failed",error="; ".join(failures))
            self.store.notify("publish_failure:"+draft["id"]+":"+str(draft["version"]),f"{c.name} {draft['kind']} ({draft['id']}): " + "; ".join(failures) + "\nUse /retry after fixing the issue. Successful platforms will not be posted twice.")
        else:
            self.store.execute("UPDATE schedules SET status='completed' WHERE draft_id=?",(draft["id"],))
            self.store.transition(draft["id"],"published")
