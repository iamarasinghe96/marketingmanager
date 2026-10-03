"""The daily "hi" session: one greeting, one prompt per campaign, one image, one caption, approve.

Nothing is planned ahead and nothing carries over. If the owner does not reply "hi",
nothing happens that day. Unfinished sessions are discarded at local midnight.
"""
from __future__ import annotations
import json
import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bot.bundles import filled_prompt
from bot.compliance import check, banned_matches, UNAVAILABLE
from bot.config import ROOT
from bot.db import stamp, utcnow
from bot.models import Copy
from bot.replies import detect_campaign

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY, campaign TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
 copy TEXT, caption TEXT, image_path TEXT, story_path TEXT, error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(campaign, day));
CREATE TABLE IF NOT EXISTS session_messages(message_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL);
"""

OPEN = ("awaiting_image", "awaiting_approval", "failed")
GREETINGS = {"hi", "hii", "hiii", "hello", "hey", "start", "good morning", "morning"}
ATTRIBUTION_MARKERS = ("explained by", "විස්තර කරන්නේ", "mbbs", "mrcpch", "mrcp (uk)")
LINE = 'Reply "approve" to post, or tell me what to change.'


class CaptionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str = Field(min_length=2, max_length=1700)
    hashtags: list[str] = Field(default_factory=list, max_length=12)


def is_greeting(text):
    return text.casefold().strip(" .!👋") in GREETINGS


def is_approval(text):
    return text.casefold().strip(" .!") in {"approve", "approved", "ok", "okay", "👍", "yes", "post it", "post"}


def clean_caption(campaign, caption):
    """Captions never carry the doctor attribution, and carry the disclaimer at most once."""
    lines = [line for line in caption.splitlines()
             if not any(marker in line.casefold() for marker in ATTRIBUTION_MARKERS)]
    text = "\n".join(lines)
    disclaimer = campaign.required_footer_rules.get("disclaimer", "")
    if disclaimer and text.count(disclaimer) > 1:
        first = text.index(disclaimer) + len(disclaimer)
        text = text[:first] + text[first:].replace(disclaimer, "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fit(source, size, background):
    """Scale the whole image into size and pad the rest; never crop the owner's artwork."""
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        scale = min(size[0] / image.width, size[1] / image.height)
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, background)
        canvas.paste(image, ((size[0] - image.width) // 2, (size[1] - image.height) // 2))
        return canvas


def waiting_for(session):
    if session["state"] == "awaiting_image":
        return "image"
    if session["state"] == "awaiting_approval":
        return "approval"
    if not session["copy"]:
        return 'prompt failed, reply "retry"'
    return 'publishing failed, reply "approve" to retry'


class DailyFlow:
    def __init__(self, app):
        self.app = app
        self.store, self.telegram, self.text = app.store, app.telegram, app.text
        self.settings, self.campaigns = app.settings, app.campaigns
        self.config = {"timezone": "Australia/Sydney", "greeting_time": "08:00", "reminder_after_hours": 3,
                       **(self.settings.get("daily") or {})}
        self.store.conn.executescript(SCHEMA)

    # ---- time -------------------------------------------------------------------------
    def zone(self):
        return ZoneInfo(self.config["timezone"])

    def today(self, now=None):
        return (now or utcnow()).astimezone(self.zone()).date()

    def active(self, day):
        return [c for c in self.campaigns.values()
                if c.enabled and not self.store.paused(c.slug) and (not c.active_from or day >= c.active_from)]

    # ---- storage ----------------------------------------------------------------------
    def session(self, session_id):
        return self.store.one("SELECT * FROM sessions WHERE id=?", (session_id,))

    def sessions(self, day, states=None):
        rows = self.store.rows("SELECT * FROM sessions WHERE day=? ORDER BY created_at", (day.isoformat(),))
        return [r for r in rows if states is None or r["state"] in states]

    def update(self, session_id, **fields):
        allowed = {"state", "copy", "caption", "image_path", "story_path", "error"}
        if not set(fields) <= allowed:
            raise ValueError("Unknown session field")
        fields["updated_at"] = stamp()
        sql = ",".join(f"{key}=?" for key in fields)
        self.store.execute(f"UPDATE sessions SET {sql} WHERE id=?", (*fields.values(), session_id))
        return self.session(session_id)

    def remember(self, message, session):
        if message and message.get("message_id"):
            self.store.execute("INSERT OR REPLACE INTO session_messages VALUES(?,?)", (message["message_id"], session["id"]))

    def replied_session(self, message, day):
        reference = message.get("reply_to_message", {}).get("message_id")
        row = self.store.one("SELECT session_id FROM session_messages WHERE message_id=?", (reference,)) if reference else None
        session = self.session(row["session_id"]) if row else None
        return session if session and session["day"] == day.isoformat() else None

    def history(self, campaign, day):
        cutoff = (day - timedelta(days=30)).isoformat()
        rows = self.store.rows("SELECT topic,headline FROM history WHERE campaign=? AND day>=? ORDER BY day DESC LIMIT 30", (campaign.slug, cutoff))
        return [dict(row) for row in rows]

    # ---- background -------------------------------------------------------------------
    async def tick(self):
        now = utcnow()
        day = self.today(now)
        # Nothing carries over: anything unfinished from an earlier day is discarded quietly.
        self.store.execute(f"UPDATE sessions SET state='discarded',updated_at=? WHERE day<? AND state IN ({','.join('?' * len(OPEN))})",
                           (stamp(), day.isoformat(), *OPEN))
        greeting_at = datetime.combine(day, time.fromisoformat(self.config["greeting_time"]), self.zone())
        if now >= greeting_at and self.store.get("greeted_day") != day.isoformat():
            self.store.set("greeted_day", day.isoformat())
            if self.active(day) and not self.sessions(day):
                await self.telegram.send('Good morning 👋 Reply "hi" to start today\'s posts.')
        await self.remind(now, day)

    async def remind(self, now, day):
        if self.store.get("reminded_day") == day.isoformat():
            return
        unfinished = self.sessions(day, ("awaiting_image", "awaiting_approval"))
        last = self.store.get("last_owner_message")
        if not unfinished or not last:
            return
        if now - datetime.fromisoformat(last) < timedelta(hours=float(self.config["reminder_after_hours"])):
            return
        self.store.set("reminded_day", day.isoformat())
        lines = [f"{self.campaigns[s['campaign']].name} post is still waiting for "
                 + ("the image." if s["state"] == "awaiting_image" else "your approval.") for s in unfinished]
        await self.telegram.send("\n".join(lines))

    # ---- owner messages ---------------------------------------------------------------
    async def on_text(self, message):
        text = message.get("text", "").strip()
        day = self.today()
        self.store.set("last_owner_message", utcnow().isoformat())
        if is_greeting(text):
            return await self.start(day)
        lowered = text.casefold()
        if lowered.startswith(("hi ", "hi,", "hello ", "hello,")):
            return await self.start(day, idea=re.sub(r"^(hi|hello)[\s,!.]*", "", text, flags=re.I))
        session = self.replied_session(message, day) or await self.pick(day, ("awaiting_approval", "awaiting_image", "failed"), {"text": text})
        if session is False:
            return
        if not session:
            if not self.sessions(day):
                await self.telegram.send('Reply "hi" to start today\'s posts.')
            else:
                await self.telegram.send("Today's posts are finished. See you tomorrow 👋")
            return
        await self.act(session, text)

    async def act(self, session, text):
        if is_approval(text):
            if session["state"] == "awaiting_image":
                await self.telegram.send(f"{self.campaigns[session['campaign']].name}: send me the finished image first.")
                return
            return await self.publish(session)
        await self.edit(session, text)

    async def on_photo(self, message, path):
        day = self.today()
        self.store.set("last_owner_message", utcnow().isoformat())
        session = self.replied_session(message, day)
        if not session:
            caption = message.get("caption", "")
            slug = detect_campaign(caption, self.campaigns) if caption else None
            candidates = [s for s in self.sessions(day, ("awaiting_image", "awaiting_approval", "failed"))
                          if not slug or s["campaign"] == slug]
            waiting = [s for s in candidates if s["state"] == "awaiting_image"]
            pool = waiting or candidates
            if len(pool) == 1:
                session = pool[0]
            elif pool:
                await self.choose(pool, {"path": str(path)}, "Which post is this image for?")
                return
            else:
                await self.telegram.send('Reply "hi" first to start today\'s posts.' if not self.sessions(day)
                                         else "There's no post waiting for an image today.")
                return
        await self.attach(session, path)

    async def choose(self, sessions, payload, prompt):
        interaction = uuid.uuid4().hex[:12]
        self.store.execute("INSERT INTO interactions VALUES(?,?,?,?)",
                           (interaction, "daily", json.dumps(payload, ensure_ascii=False), stamp()))
        buttons = [[{"text": self.campaigns[s["campaign"]].name[:60], "callback_data": f"s:{interaction}:{s['id']}"}] for s in sessions]
        await self.telegram.send(prompt, buttons)

    async def pick(self, day, states, payload):
        """Return the only open session, None if there is none, or False after asking with buttons."""
        open_sessions = self.sessions(day, states)
        if len(open_sessions) <= 1:
            return open_sessions[0] if open_sessions else None
        await self.choose(open_sessions, payload, "Which post is this for?")
        return False

    async def callback(self, callback):
        parts = callback.get("data", "").split(":")
        if len(parts) != 3:
            raise ValueError("This button is no longer valid.")
        item = self.store.one("SELECT * FROM interactions WHERE id=?", (parts[1],))
        session = self.session(parts[2])
        if not item or not session or session["day"] != self.today().isoformat():
            raise ValueError("This choice has expired.")
        self.store.execute("DELETE FROM interactions WHERE id=?", (parts[1],))
        payload = json.loads(item["payload"])
        if payload.get("path"):
            await self.attach(session, Path(payload["path"]))
        else:
            await self.act(session, payload.get("text", ""))

    # ---- steps ------------------------------------------------------------------------
    async def start(self, day, idea=""):
        existing = self.sessions(day)
        if existing:
            unfinished = [s for s in existing if s["state"] in OPEN]
            if not unfinished:
                await self.telegram.send("Today's posts are already done ✅")
            else:
                await self.telegram.send("Already started today. Waiting for: " + ", ".join(
                    f"{self.campaigns[s['campaign']].name} ({waiting_for(s)})" for s in unfinished))
            return
        campaigns = self.active(day)
        if not campaigns:
            await self.telegram.send("No campaigns are active today.")
            return
        target = detect_campaign(idea, self.campaigns) if idea else None
        for campaign in campaigns:
            session_id = uuid.uuid4().hex[:12]
            try:
                self.store.execute("INSERT INTO sessions(id,campaign,day,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                                   (session_id, campaign.slug, day.isoformat(), "generating", stamp(), stamp()))
            except Exception:
                continue  # Another "hi" already created it.
            try:
                own_idea = idea if idea and (not target or target == campaign.slug) else ""
                copy = await self.write(campaign, day, own_idea)
                caption = self.caption(campaign, copy)
                session = self.update(session_id, state="awaiting_image", copy=copy.model_dump_json(), caption=caption)
                self.store.execute("INSERT OR REPLACE INTO history(draft_id,campaign,day,topic,headline) VALUES(?,?,?,?,?)",
                                   (session_id, campaign.slug, day.isoformat(), copy.topic, copy.headline))
                await self.send_prompt(campaign, session, copy)
            except Exception as exc:
                log.exception("Could not prepare %s", campaign.slug)
                self.update(session_id, state="failed", error=str(exc))
                await self.telegram.send(f"{campaign.name}: couldn't write today's prompt ({str(exc)[:300]}). Reply \"retry\" to try again.")

    async def write(self, campaign, day, idea=""):
        self.unchecked = False
        correction, copy, reasons = "", None, []
        history = self.history(campaign, day)
        context = {"format": "post", "caption_rules": "No doctor name, credentials or attribution in the caption."}
        for _ in range(3):
            copy = await self.text.write(campaign, day, history, idea, context, None, correction)
            if campaign.style == "clinic" and copy.url:
                copy = copy.model_copy(update={"url": ""})
            reasons = await check(campaign, copy, self.text)
            if not reasons:
                return copy
            if reasons == [UNAVAILABLE]:
                # The AI check could not run (provider error), but the fixed banned-phrase and
                # category rules passed. Continue and ask the owner to review carefully.
                self.unchecked = True
                return copy
            correction = "Correct these failures: " + "; ".join(reasons)
        raise ValueError("The text could not pass the brand/medical checks: " + "; ".join(reasons))

    def caption(self, campaign, copy):
        text = clean_caption(campaign, copy.caption)
        tags = " ".join(copy.hashtags[:campaign.hashtag_rules["max_count"]])
        return text + ("\n\n" + tags if tags else "")

    def assets(self, campaign, copy):
        files = []
        if campaign.style != "clinic" or copy.category == "INSTITUTIONAL":
            files.append((campaign.asset(campaign.logo_path), "logo" + Path(campaign.logo_path).suffix))
        if copy.visual_kind == "screenshot":
            shots = campaign.files(campaign.screenshots)
            if shots:
                chosen = shots[min(copy.screenshot_index, len(shots) - 1)]
                files.append((chosen, "screenshot" + chosen.suffix))
        if campaign.style == "clinic" and copy.category == "INSTITUTIONAL" and copy.visual_kind == "premises" and campaign.premises_path:
            files.append((campaign.asset(campaign.premises_path), "premises.jpg"))
        return files

    async def send_prompt(self, campaign, session, copy):
        folder = ROOT / "out" / "daily" / session["day"] / campaign.slug
        folder.mkdir(parents=True, exist_ok=True)
        prompt = folder / "prompt.txt"
        prompt.write_text(filled_prompt(campaign, {"copy": copy.model_dump_json(), "kind": "post", "idea": "",
                                                   "reference_instructions": ""}, []), encoding="utf-8", newline="\n")
        assets = self.assets(campaign, copy)
        attach = ("Attach: " + ", ".join(name for _, name in assets) + ".") if assets else "No attachments needed."
        label = {"MARKETING": "", "EDUCATIONAL": " (educational)", "INSTITUTIONAL": " (institutional)"}[copy.category]
        note = " ⚠ The AI medical check couldn't run today, so please review the wording carefully." if getattr(self, "unchecked", False) else ""
        message = await self.telegram.document(prompt, caption=f"{campaign.name}{label} – today's prompt. {attach} Send me the finished image when ready.{note}")
        self.remember(message, session)
        for source, name in assets:
            target = folder / name
            if name == "premises.jpg":
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image)
                    box = tuple(round(v * image.size[i % 2]) for i, v in enumerate(campaign.premises_crop))
                    image.crop(box).convert("RGB").save(target, quality=95)
            else:
                target.write_bytes(Path(source).read_bytes())
            self.remember(await self.telegram.document(target), session)

    async def attach(self, session, path):
        if session["state"] in {"published", "publishing", "discarded"}:
            await self.telegram.send("That post is already finished.")
            return
        if not session["copy"]:
            await self.telegram.send('This prompt failed to generate. Reply "retry" first.')
            return
        campaign = self.campaigns[session["campaign"]]
        folder = ROOT / "out" / "daily" / session["day"] / campaign.slug
        folder.mkdir(parents=True, exist_ok=True)
        background = campaign.palette.get("background", "#FFFFFF")
        post, story = folder / "post.png", folder / "story.png"
        fit(path, tuple(campaign.post_size), background).save(post)
        fit(path, tuple(campaign.story_size), background).save(story)
        session = self.update(session["id"], state="awaiting_approval", image_path=str(post), story_path=str(story), error=None)
        await self.show(session)

    async def show(self, session):
        text = session["caption"] + "\n\n" + LINE
        if len(text) <= 1000:
            self.remember(await self.telegram.photo(session["image_path"], text), session)
        else:
            self.remember(await self.telegram.photo(session["image_path"]), session)
            self.remember(await self.telegram.send(text[:4000]), session)

    async def edit(self, session, instruction):
        if session["state"] == "failed" and not session["copy"]:
            if instruction.casefold().strip(" .!") in {"retry", "try again"}:
                self.store.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
                return await self.start_one(session)
            await self.telegram.send('Reply "retry" to write this prompt again.')
            return
        campaign = self.campaigns[session["campaign"]]
        copy = Copy.model_validate_json(session["copy"])
        system = (campaign.brand_prompt + "\n\nYou are editing ONE social media caption. Apply the owner's instruction exactly and change "
                  "nothing else. Keep the brand and medical rules above. Never include a doctor's name, credentials or "
                  "'Explained by' attribution in the caption. Include the medical disclaimer at most once. Put hashtags only in "
                  f"the hashtags list (max {campaign.hashtag_rules['max_count']}), never inside the caption text. Keep the caption's language "
                  "unless asked to change it.")
        prompt = json.dumps({"current_caption": copy.caption, "current_hashtags": copy.hashtags, "post_headline": copy.headline,
                             "owner_instruction": instruction}, ensure_ascii=False)
        reasons = []
        for _ in range(2):
            try:
                result = await self.text.json(system, prompt, CaptionEdit)
                updated = copy.model_copy(update={"caption": clean_caption(campaign, result.caption), "hashtags": result.hashtags})
                Copy.model_validate(updated.model_dump())
            except (ValidationError, ValueError) as exc:
                reasons = [str(exc)[:200]]
                continue
            reasons = await check(campaign, updated, self.text)
            if not reasons:
                break
            prompt = json.dumps({"current_caption": copy.caption, "current_hashtags": copy.hashtags, "owner_instruction": instruction,
                                 "previous_attempt_failed_because": reasons}, ensure_ascii=False)
        if reasons:
            await self.telegram.send("I couldn't make that change without breaking the brand/medical rules: "
                                     + "; ".join(reasons)[:600] + "\nThe caption is unchanged.")
            return
        session = self.update(session["id"], copy=updated.model_dump_json(), caption=self.caption(campaign, updated))
        tail = LINE if session["state"] == "awaiting_approval" else "Send me the finished image when ready."
        self.remember(await self.telegram.send((session["caption"] + "\n\n" + tail)[:4000]), session)

    async def start_one(self, session):
        campaign = self.campaigns[session["campaign"]]
        day = date.fromisoformat(session["day"])
        session_id = uuid.uuid4().hex[:12]
        self.store.execute("INSERT INTO sessions(id,campaign,day,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (session_id, campaign.slug, day.isoformat(), "generating", stamp(), stamp()))
        try:
            copy = await self.write(campaign, day)
            new = self.update(session_id, state="awaiting_image", copy=copy.model_dump_json(), caption=self.caption(campaign, copy))
            await self.send_prompt(campaign, new, copy)
        except Exception as exc:
            self.update(session_id, state="failed", error=str(exc))
            await self.telegram.send(f"{campaign.name}: still couldn't write the prompt ({str(exc)[:300]}).")

    async def publish(self, session):
        if session["state"] not in {"awaiting_approval", "failed"} or not session["image_path"]:
            await self.telegram.send("Send me the finished image first.")
            return
        campaign = self.campaigns[session["campaign"]]
        copy = Copy.model_validate_json(session["copy"])
        hits = banned_matches(session["caption"], campaign.banned_phrases)
        if hits:
            await self.telegram.send("Not posted: the caption contains banned wording (" + ", ".join(hits) + "). Tell me what to change.")
            return
        self.update(session["id"], state="publishing")
        dry = self.settings["dry_run"]
        links, failures = [], []
        for platform in ("facebook", "instagram"):
            for kind, path in (("post", session["image_path"]), ("story", session["story_path"])):
                record = self.store.reserve_publication(session["id"], platform, kind, dry)
                if record["status"] == "completed":
                    continue
                try:
                    if dry:
                        self.store.publication_update(record["idempotency_key"], status="completed", remote_id="DRY_RUN")
                        continue
                    _, link = await self.app.meta.publish(campaign, {"id": session["id"], "kind": kind}, record, path,
                                                          session["caption"] if kind == "post" else "")
                    if link and kind == "post":
                        links.append(f"{platform.title()}: {link}")
                except Exception as exc:
                    log.exception("Publishing %s %s failed", platform, kind)
                    failures.append(f"{platform} {kind}: {str(exc)[:200]}")
        if failures:
            self.update(session["id"], state="failed", error="; ".join(failures))
            await self.telegram.send(f"⚠ {campaign.name}: some posts failed.\n" + "\n".join(failures)
                                     + '\nReply "approve" to retry. Anything already posted will not be posted twice.')
            return
        self.update(session["id"], state="published")
        if dry:
            await self.telegram.send(f"DRY RUN ✅ {campaign.name}: would have posted the post and story to Facebook and Instagram. Nothing was published.")
        else:
            await self.telegram.send(f"✅ Posted {campaign.name} (post + story) to Facebook and Instagram." + ("\n" + "\n".join(links) if links else ""))

    def status_lines(self):
        day = self.today()
        greeting = datetime.combine(day, time.fromisoformat(self.config["greeting_time"]), self.zone())
        lines = [f"Daily greeting: {greeting:%H:%M} {self.config['timezone']}",
                 "Active today: " + (", ".join(c.name for c in self.active(day)) or "none")]
        for c in self.campaigns.values():
            if c.active_from and day < c.active_from:
                lines.append(f"{c.name}: paused until {c.active_from:%d %b %Y}")
        for s in self.sessions(day):
            lines.append(f"Today · {self.campaigns[s['campaign']].name}: {s['state'].replace('_', ' ')}")
        return lines


HELP = """Marketing Manager – daily flow

1. Every morning I say good morning. Reply "hi" to start. No reply = nothing happens that day.
2. I send one prompt.txt per active campaign (plus the logo/screenshot if that post needs it).
3. Make the image in ChatGPT with that prompt, then send me the finished image.
4. I reply with the image and its caption.
5. To change the caption, just write what you want, e.g. "make it shorter" or "add a line about dust mites".
6. Reply "approve" and I post it straight away to Facebook and Instagram, as a post and a story.

If something is unfinished I remind you once. Anything unfinished is dropped at midnight; nothing carries over.
Tip: "hi, idea: <your idea>" uses your idea for today's prompt.

/status · accounts and today's progress
/pause [campaign] · /resume [campaign]
/campaigns · campaign names
/help · this guide"""
