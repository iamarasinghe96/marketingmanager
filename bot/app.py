from __future__ import annotations
import asyncio
import json
import logging
import signal
import uuid
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from bot.config import ROOT,load_config
from bot.db import Store,utcnow,stamp
from bot.generation import TextClient
from bot.images import image_provider
from bot.telegram import Telegram,HELP
from bot.meta import MetaClient
from bot.linkedin import LinkedInClient
from bot.pipeline import Pipeline
from bot.replies import parse_reply,detect_campaign,detect_formats
from bot.scheduling import local_day,slots_for
from bot.daily import DailyFlow,HELP as DAILY_HELP

log = logging.getLogger(__name__)


class App:
    def __init__(self,client,settings,campaigns,secrets,store):
        self.settings,self.campaigns,self.secrets,self.store = settings,campaigns,secrets,store
        self.telegram = Telegram(client,secrets,store)
        self.text = TextClient(client,settings,secrets,store)
        self.images = image_provider(settings,store,self.telegram,self.text)
        self.meta = MetaClient(client,settings,secrets,store)
        self.linkedin = LinkedInClient(client,settings,secrets,store)
        self.pipeline = Pipeline(settings,campaigns,store,self.text,self.images,self.telegram,self.meta,self.linkedin)
        self.stopping = False
        store.sync_campaigns(campaigns)
        self.daily = DailyFlow(self) if settings.get("workflow","daily") == "daily" else None
        store.set("max_upload_bytes",settings["images"]["maximum_upload_mb"]*1024*1024)

    async def choose(self,kind,payload,options,prompt):
        interaction = uuid.uuid4().hex[:12]
        self.store.execute("INSERT INTO interactions VALUES(?,?,?,?)",(interaction,kind,json.dumps(payload,ensure_ascii=False),stamp()))
        buttons = [[{"text":title[:60],"callback_data":f"x:{interaction}:{key}"}] for key,title in options]
        await self.telegram.send(prompt,buttons)

    def campaign_options(self):
        return [(c.slug,c.name) for c in self.campaigns.values() if c.enabled]

    async def idea(self,text,slug=None,image=None,references=None,kinds=None):
        references = list(references or ([] if image is None else [str(image)]))
        slug = slug or detect_campaign(text,self.campaigns)
        if not slug:
            await self.choose("idea_campaign",{"text":text,"references":references,"kinds":kinds},self.campaign_options(),"Which campaign is this idea for?")
            return
        if len(references) == 1 and text.casefold().startswith("use this"):
            image = Path(references[0])
            # Explicit source photos retain the original composition workflow.
            self.store.execute("INSERT INTO incoming_images VALUES(?,?,?,?,NULL,?)",
                               (uuid.uuid4().hex[:12],slug,str(image),text,stamp()))
            await self.pipeline.generate_now(slug,text,delivery_mode="visual")
            return
        kinds = kinds or detect_formats(text)
        if not kinds:
            await self.choose("idea_format",{"text":text,"slug":slug,"references":references},
                              [("post","Post"),("story","Story"),("both","Both")],"Make this idea a Post, Story or Both?")
            return
        return await self.pipeline.content.create(slug,text,kinds,references)

    async def draft_action(self,draft,reply):
        if draft.get("delivery_mode") == "finished":
            if reply.action == "approve" and draft["state"] == "awaiting_content":
                await self.pipeline.content.approve(draft)
            elif reply.action == "approve":
                await self.pipeline.approve(draft)
            elif reply.action == "skip":
                await self.pipeline.skip(draft)
            else:
                await self.pipeline.content.prepare(draft,{"headline":reply.headline,"caption":reply.caption,
                                                          "visual":reply.visual or reply.photo,"feedback":reply.feedback})
            return
        if reply.action == "approve":
            await self.pipeline.approve(draft)
        elif reply.action == "skip":
            await self.pipeline.skip(draft)
            await self.telegram.send("Skipped " + draft["id"])
        elif reply.photo and not reply.caption and not reply.feedback:
            await self.pipeline.new_image(draft,reply.photo)
        else:
            if self.store.one("SELECT idempotency_key FROM publications WHERE draft_id=? AND status IN ('sending','unknown','completed') AND idempotency_key LIKE 'live:%'",(draft["id"],)):
                raise ValueError("Publishing already began. Use /retry or /resolve before changing this draft.")
            await self.pipeline.prepare(draft,{"photo":reply.photo,"caption":reply.caption,"feedback":reply.feedback})

    async def text_message(self,message):
        text = message.get("text","").strip()
        if not text:
            return
        if text.startswith("/"):
            await self.command(text)
            return
        reply = parse_reply(text)
        reference = message.get("reply_to_message",{}).get("message_id")
        mapped = self.store.one("SELECT * FROM review_messages WHERE chat_id=? AND message_id=?",(self.telegram.owner,reference)) if reference else None
        if mapped:
            draft = self.store.draft(mapped["draft_id"])
            if draft["version"] != mapped["version"]:
                raise ValueError("This is an older card. Reply to the newest draft/request.")
            if self.store.get("new_idea:"+draft["id"]) == draft["version"]:
                self.store.set("new_idea:"+draft["id"],None)
                self.store.execute("UPDATE drafts SET idea=? WHERE id=?",(text,draft["id"]))
                self.store.execute("UPDATE ideas_queue SET text=? WHERE draft_id=?",(text,draft["id"]))
                await self.pipeline.content.prepare(self.store.draft(draft["id"]),{"feedback":"Replace the previous concept with the owner's new idea. Preserve exact supplied wording."})
            else:
                await self.draft_action(draft,reply)
        else:
            # Outside a draft reply, every ordinary message starts a new idea.
            await self.idea(text)

    async def attach_image(self,draft,path):
        if draft.get("delivery_mode") == "finished":
            return await self.pipeline.content.receive(draft,path)
        if draft["state"] in {"published","skipped","publishing"}:
            raise ValueError("This draft has already closed. Add ‘Use this for <campaign>’ to reserve a new draft.")
        if self.store.one("SELECT idempotency_key FROM publications WHERE draft_id=? AND status IN ('sending','unknown','completed') AND idempotency_key LIKE 'live:%'",(draft["id"],)):
            raise ValueError("This draft has already begun publishing; its visual cannot be changed")
        request_row = self.store.one("SELECT * FROM image_requests WHERE draft_id=? AND status='pending' ORDER BY created_at LIMIT 1",(draft["id"],))
        if request_row:
            draft = await self.images.submit(request_row["id"],path)
        else:
            self.store.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,?)",
                               (draft["id"],draft["version"],"original_"+draft["kind"],str(path),stamp()))
        await self.pipeline.compose(draft,path)

    async def photo_message(self,message,album=None):
        if album:
            messages = sorted(album,key=lambda item:item["message_id"])
            paths = []
            for item in messages:
                path = ROOT/"out"/"originals"/(str(item["message_id"])+"-"+uuid.uuid4().hex[:8]+".image")
                paths.append(str(await self.telegram.download_image(item,path)))
            captions = list(dict.fromkeys(item.get("caption","") for item in messages if item.get("caption")))
            caption = "\n".join(captions)
            if any(item.get("reply_to_message",{}).get("message_id") for item in messages):
                raise ValueError("For finished artwork, reply with one image per Post or Story. The album originals are saved; send an album outside a reply to use it as style references.")
            await self.idea(caption or "Make a campaign using these style references",references=paths)
            return
        path = ROOT/"out"/"originals"/(str(message["message_id"])+"-"+uuid.uuid4().hex[:8]+".image")
        path = await self.telegram.download_image(message,path)
        caption = message.get("caption","")
        reference = message.get("reply_to_message",{}).get("message_id")
        mapped = self.store.one("SELECT * FROM review_messages WHERE chat_id=? AND message_id=?",(self.telegram.owner,reference)) if reference else None
        if mapped:
            draft = self.store.draft(mapped["draft_id"])
            if draft["version"] != mapped["version"]:
                raise ValueError("This image replies to an old card. Send it again as a reply to the newest card.")
            await self.attach_image(draft,path)
            return
        if caption:
            await self.idea(caption,image=path)
            return
        requests = self.store.rows("SELECT i.* FROM image_requests i JOIN drafts d ON d.id=i.draft_id WHERE i.status='pending' AND d.state='awaiting_image' ORDER BY i.created_at")
        if len(requests) == 1:
            await self.attach_image(self.store.draft(requests[0]["draft_id"]),path)
        elif requests:
            options = []
            for request_row in requests:
                draft = self.store.draft(request_row["draft_id"])
                options.append((draft["id"],self.campaigns[draft["campaign"]].name+" · "+draft["kind"]+" · "+draft["day"]))
            await self.choose("image",{"path":str(path)},options,"Several image requests are waiting. Choose one (oldest is first).")
        else:
            await self.idea(caption or "Make a campaign using this style reference",references=[str(path)])

    async def callback(self,callback):
        parts = callback.get("data","").split(":")
        await self.telegram.answer(callback["id"])
        if parts[0] == "c" and len(parts) == 4:
            _,draft_id,version,action = parts
            draft = self.store.draft(draft_id)
            if not draft or draft["version"] != int(version):
                raise ValueError("This content card is outdated. Use the newest card.")
            if action == "approve":
                await self.pipeline.content.approve(draft)
            elif action == "rewrite":
                await self.pipeline.content.prepare(draft,{"feedback":"Rewrite this idea with fresh compliant copy, preserving the owner's exact supplied text and reference instructions."})
            elif action == "new":
                self.pipeline.ensure_revisable(draft)
                self.store.set("new_idea:"+draft["id"],draft["version"])
                card = await self.telegram.send("Reply to this message with your replacement idea. It will use this draft's reserved slot and style references.")
                self.telegram.map_message(card["message_id"],draft)
            elif action == "today":
                await self.pipeline.content.use_today(draft)
            else:
                raise ValueError("Unknown content action")
        elif parts[0] == "d" and len(parts) == 4:
            _,draft_id,version,action = parts
            draft = self.store.draft(draft_id)
            if not draft or draft["version"] != int(version):
                raise ValueError("This card is outdated. Use the newest review card.")
            if action == "image":
                await self.pipeline.new_image(draft,"Create a different visual idea for the same theme")
            elif action == "text":
                await self.pipeline.prepare(draft,{"feedback":"Write fresh copy for this topic"})
            elif action == "today":
                await self.pipeline.content.use_today(draft)
            else:
                await self.draft_action(draft,parse_reply(action))
        elif parts[0] == "i" and len(parts) == 3:
            row = self.store.one("SELECT * FROM image_requests WHERE id=? AND status='pending'",(parts[1],))
            if not row:
                raise ValueError("This image request is closed. Use the newest request.")
            draft = self.store.draft(row["draft_id"])
            if row["version"] != draft["version"]:
                raise ValueError("This image request is outdated")
            if parts[2] == "skip":
                draft = await self.images.skip(row["id"])
                await self.pipeline.compose(draft,skip_image=True)
            elif parts[2] == "today":
                await self.pipeline.content.use_today(draft)
            else:
                if row["mode"] == "finished":
                    await self.pipeline.content.prepare(draft,{"feedback":"Choose a fresh visual composition for the same idea. Keep the approved headline, supporting copy, CTA and caption, and preserve exact style-reference instructions."})
                else:
                    await self.images.request(self.campaigns[draft["campaign"]],draft,"Rewrite the prompt with a fresh composition")
        elif parts[0] == "x" and len(parts) == 3:
            item = self.store.one("SELECT * FROM interactions WHERE id=?",(parts[1],))
            if not item:
                raise ValueError("This choice has already been handled")
            payload = json.loads(item["payload"])
            key = parts[2]
            if item["kind"] == "idea_campaign" and key in self.campaigns:
                await self.idea(payload["text"],key,references=payload.get("references"),kinds=payload.get("kinds"))
            elif item["kind"] == "idea_format" and key in {"post","story","both"}:
                await self.idea(payload["text"],payload["slug"],references=payload.get("references"),kinds=["post","story"] if key == "both" else [key])
            elif item["kind"] in {"image","revision"}:
                draft = self.store.draft(key)
                if not draft:
                    raise ValueError("Draft no longer exists")
                if item["kind"] == "image":
                    await self.attach_image(draft,Path(payload["path"]))
                else:
                    await self.draft_action(draft,parse_reply(payload["text"]))
            elif item["kind"] == "command":
                await self.command(payload["command"]+" "+key)
            else:
                raise ValueError("Invalid choice")
            self.store.execute("DELETE FROM interactions WHERE id=?",(parts[1],))

    def status(self):
        lines = ["Marketing Manager is running",f"DRY_RUN={'true' if self.settings['dry_run'] else 'false'}","Images: manual Telegram handoff (no image API)"]
        for c in self.campaigns.values():
            token = self.store.token("meta_page",c.facebook_page_id)
            status = "paused" if self.store.paused(c.slug) else "enabled" if c.enabled else "disabled"
            lines.append(f"{c.name}: {status}; Facebook {c.facebook_page_id}; Instagram {c.instagram_account_id}; token {'valid' if token and token['valid'] else 'missing/invalid'}")
        if self.settings["linkedin"]["enabled"]:
            token = self.store.token("linkedin","access")
            lines.append("LinkedIn: enabled; " + ("token expires " + str(token["expires_at"]) if token else "token missing"))
        else:
            lines.append("LinkedIn: disabled")
        if self.daily:
            lines.extend(self.daily.status_lines())
            return "\n".join(lines)
        lines.append("Next jobs:")
        for row in self.store.rows("SELECT d.id,d.campaign,d.kind,d.state,s.due_at FROM drafts d JOIN schedules s ON s.draft_id=d.id WHERE s.status='pending' ORDER BY s.due_at LIMIT 8"):
            lines.append(f"{row['id']} · {row['campaign']} {row['kind']} · {row['state']} · {row['due_at']}")
        for c in self.campaigns.values():
            if c.enabled:
                day = local_day(c,utcnow())
                post,story = slots_for(self.campaigns,c,day,self.settings["minimum_publish_gap_minutes"])
                for kind,due in (("post",post),("story",story)):
                    if due <= utcnow():
                        post_next,story_next = slots_for(self.campaigns,c,day+timedelta(days=1),self.settings["minimum_publish_gap_minutes"])
                        due = post_next if kind == "post" else story_next
                    request_at = (due-timedelta(minutes=self.settings["images"]["request_lead_minutes"])).astimezone(ZoneInfo(c.timezone))
                    lines.append(f"{c.name} {kind} image request: {request_at:%a %d %b %H:%M %Z}")
        day = utcnow().date().isoformat()
        quotas = self.store.rows("SELECT service,count FROM quota_usage WHERE day=?",(day,))
        lines.append("API text usage today: " + (", ".join(f"{q['service']} {q['count']}" for q in quotas) or "0") + ". Provider free quota is checked by each call.")
        usage = {q["service"]:q["count"] for q in quotas}
        lines.append("Local daily text budget left: Groq " + str(max(0,self.settings["groq"]["daily_text_limit"]-usage.get("groq_text",0))) +
                     "; Gemini " + str(max(0,self.settings["gemini"]["daily_text_limit"]-usage.get("gemini_text",0))) + ". Actual provider limits may be lower.")
        return "\n".join(lines)

    async def command(self,text):
        args = text.split()
        name = args[0].split("@")[0].casefold()
        target = args[1] if len(args)>1 else None
        if self.daily and name in {"/generate","/queue","/publish_now","/retry"}:
            if name == "/generate":
                await self.daily.start(self.daily.today())
            else:
                await self.telegram.send("\n".join(self.daily.status_lines()) + '\n\nTo post: send the image, then reply "approve". /help explains the daily flow.')
            return
        if name in {"/start","/help"}:
            await self.telegram.send(DAILY_HELP if self.daily else HELP)
        elif name == "/reset" and self.daily:
            if not self.settings["dry_run"]:
                await self.telegram.send("/reset only works in dry run (DRY_RUN=true), so nothing live is ever posted twice.")
                return
            day = self.daily.today().isoformat()
            cleared = self.store.execute("DELETE FROM sessions WHERE day=?",(day,)).rowcount
            await self.telegram.send(f"Cleared today's session ({cleared}). Send \"hi\" (or \"hi sinhala\" / \"hi tamil\") to start again.")
        elif name == "/status":
            await self.telegram.send(self.status()[:4000])
        elif name == "/campaigns":
            await self.telegram.send("\n".join(f"{c.slug}: {c.name}" for c in self.campaigns.values()))
        elif name == "/queue":
            drafts = self.store.rows("SELECT id,campaign,day,kind,state FROM drafts WHERE state NOT IN ('published','skipped') ORDER BY day,created_at LIMIT 30")
            await self.telegram.send("\n".join(f"{d['id']} · {d['campaign']} · {d['kind']} · {d['day']} · {d['state']}" for d in drafts) or "Queue empty")
        elif name in {"/pause","/resume"}:
            slug = target if target in self.campaigns else detect_campaign(target or "",self.campaigns)
            if target and not slug:
                raise ValueError("Unknown campaign. Use /campaigns.")
            slugs = [slug] if slug else list(self.campaigns)
            for slug in slugs:
                self.store.execute("UPDATE campaigns SET paused=? WHERE slug=?",(int(name=="/pause"),slug))
            await self.telegram.send(("Paused: " if name=="/pause" else "Resumed: ")+", ".join(slugs))
        elif name == "/generate":
            slug = target if target in self.campaigns else detect_campaign(target or "",self.campaigns)
            if slug:
                await self.pipeline.generate_now(slug)
            else:
                await self.choose("command",{"command":"/generate"},self.campaign_options(),"Generate for which campaign?")
        elif name in {"/publish_now","/retry"}:
            draft = self.store.draft(target) if target else None
            if not draft:
                matches = [d for d in self.store.pending() if not target or d["campaign"]==target]
                await self.choose("command",{"command":name},[(d["id"],d["id"]+" · "+d["campaign"]+" · "+d["kind"]) for d in matches],"Choose an approved/failed draft.")
                return
            if draft["approved_version"] != draft["version"] or draft["state"] not in {"approved","failed","publishing"}:
                raise ValueError("Approve the current review card first")
            c = self.campaigns[draft["campaign"]]
            if local_day(c,utcnow()).isoformat() > draft["day"]:
                raise ValueError("Draft expired at local midnight")
            await self.pipeline.publish(draft)
        elif name == "/resolve":
            if len(args) != 5:
                raise ValueError("Use /resolve <draft ID> <platform> <post|story> <remote ID|not-posted>. Confirm the account manually first.")
            _,draft_id,platform,kind,receipt = args
            key = f"live:{draft_id}:{platform}:{kind}"
            row = self.store.one("SELECT * FROM publications WHERE idempotency_key=?",(key,))
            if not row or row["status"] not in {"sending","unknown"}:
                raise ValueError("No uncertain publication to resolve")
            if receipt == "not-posted":
                self.store.publication_update(key,status="prepared" if row["upload_id"] else "reserved",error=None)
                await self.telegram.send("Recorded your confirmation that it was not posted. Use /retry " + draft_id)
            else:
                self.store.publication_update(key,status="completed",remote_id=receipt,error=None)
                await self.telegram.send("Saved the existing publication receipt. Use /retry to finish remaining platforms.")
        else:
            await self.telegram.send("Unknown command. Use /help.")

    async def handle(self,update):
        if not self.telegram.authorised(update):
            return
        try:
            callback = update.get("callback_query")
            message = update.get("message",{})
            if self.daily:
                await self.handle_daily(callback,message)
            elif callback:
                await self.callback(callback)
            elif message.get("photo") or message.get("document"):
                if message.get("media_group_id"):
                    self.buffer_album(message)
                else:
                    await self.photo_message(message)
            else:
                await self.text_message(message)
        except Exception as exc:
            log.exception("Owner action failed")
            await self.telegram.send(str(exc)[:1800] + "\nUse /help for supported actions.")

    async def handle_daily(self,callback,message):
        if callback:
            await self.telegram.answer(callback["id"])
            if callback.get("data","").startswith(("s:","o:")):
                await self.daily.callback(callback)
            else:
                await self.callback(callback)
        elif message.get("photo") or message.get("document"):
            path = ROOT/"out"/"originals"/(str(message["message_id"])+"-"+uuid.uuid4().hex[:8]+".image")
            await self.daily.on_photo(message,await self.telegram.download_image(message,path))
        elif message.get("text","").strip().startswith("/"):
            await self.command(message["text"].strip())
        elif message.get("text","").strip():
            await self.daily.on_text(message)

    def buffer_album(self,message):
        album_id = str(message["media_group_id"])
        row = self.store.one("SELECT * FROM media_albums WHERE id=?",(album_id,))
        if row and row["status"] != "pending":
            raise ValueError("This album was already processed. Send a new album to add more references.")
        messages = json.loads(row["messages"]) if row else []
        if not any(item["message_id"] == message["message_id"] for item in messages):
            messages.append(message)
        ready_at = (utcnow()+timedelta(seconds=2)).isoformat()
        self.store.execute("INSERT INTO media_albums(id,messages,ready_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET messages=excluded.messages,ready_at=excluded.ready_at",
                           (album_id,json.dumps(messages,ensure_ascii=False),ready_at))

    async def flush_albums(self):
        for row in self.store.rows("SELECT * FROM media_albums WHERE status='pending' AND ready_at<=? ORDER BY ready_at",(utcnow().isoformat(),)):
            # Claim before sending, so reboot/retry cannot reserve the idea twice.
            self.store.execute("UPDATE media_albums SET status='processing' WHERE id=?",(row["id"],))
            try:
                await self.photo_message({},album=json.loads(row["messages"]))
                self.store.execute("UPDATE media_albums SET status='completed' WHERE id=?",(row["id"],))
            except Exception as exc:
                self.store.execute("UPDATE media_albums SET status='failed' WHERE id=?",(row["id"],))
                await self.telegram.send("Could not finish the album: "+str(exc)+". Send it again as a new album; check /queue for any reserved drafts first.")

    async def tick(self):
        if self.daily:
            await self.daily.tick()
            await self.housekeeping()
            return
        await self.flush_albums()
        now = utcnow()
        for c in self.campaigns.values():
            if not c.enabled or self.store.paused(c.slug):
                continue
            day = local_day(c,now)
            post,story = slots_for(self.campaigns,c,day,self.settings["minimum_publish_gap_minutes"])
            for kind,due in (("post",post),("story",story)):
                request_at = due-timedelta(minutes=self.settings["images"]["request_lead_minutes"])
                existing = self.store.one("SELECT * FROM drafts WHERE campaign=? AND day=? AND kind=?",(c.slug,day.isoformat(),kind))
                if not existing and now>=request_at:
                    draft = self.pipeline.reserve(c,day,kind,delivery_mode="finished")
                    if now>=due:
                        await self.pipeline.skip(draft)
                        self.store.notify("missed:"+draft["id"],f"Skipped missed {c.name} {kind}. Bot came online after its publish time.")
                    else:
                        try:
                            await self.pipeline.prepare(draft)
                        except Exception as exc:
                            current = self.store.draft(draft["id"])
                            if current["state"] not in {"failed","skipped"}:
                                self.store.transition(draft["id"],"failed",error=str(exc))
                            self.store.notify("generation_failed:"+draft["id"],f"Could not prepare {c.name} {kind} ({draft['id']}): {exc}. Check text API keys/quota, then send revision feedback to regenerate.")
                            if draft["delivery_mode"] == "finished":
                                await self.pipeline.content.failed(self.store.draft(draft["id"]),exc)
        for draft in self.store.rows("SELECT * FROM drafts WHERE state NOT IN ('published','skipped') ORDER BY day,created_at"):
            c = self.campaigns[draft["campaign"]]
            today = local_day(c,utcnow()).isoformat()
            if draft["day"] < today:
                await self.pipeline.skip(draft)
                self.store.notify("expired:"+draft["id"],f"Expired {c.name} {draft['kind']} ({draft['id']}) at local midnight.")
                continue
            if self.store.paused(c.slug) or not c.enabled:
                continue
            due = datetime.fromisoformat(self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"])
            row = self.store.one("SELECT * FROM image_requests WHERE draft_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",(draft["id"],))
            if ((row and not row["reminded"]) or draft["state"] == "awaiting_content") and utcnow()>=due-timedelta(minutes=self.settings["images"]["reminder_lead_minutes"]):
                self.store.notify("image_reminder:"+draft["id"],f"🖼 Still waiting for {c.name} {draft['kind']} image ({draft['id']}). Reply to the request with your image, or choose Skip image. Nothing will publish without approval.")
                if row:
                    self.store.execute("UPDATE image_requests SET reminded=1 WHERE id=?",(row["id"],))
            if draft["state"] == "approved" and (draft["immediate"] or due<=utcnow() and draft["day"] == today):
                await self.pipeline.publish(draft)
            elif due<=utcnow() and draft["state"] in {"awaiting_content","in_review","awaiting_image"} and not draft["reminded"]:
                self.store.notify("approval_reminder:"+draft["id"],f"{c.name} {draft['kind']} ({draft['id']}) is due. It has not been approved, so it was not published. A later approval will publish today; it expires at local midnight.")
                self.store.execute("UPDATE drafts SET reminded=1 WHERE id=?",(draft["id"],))
        await self.housekeeping()

    async def housekeeping(self):
        today = utcnow().date().isoformat()
        if self.store.get("health_day") != today:
            for issue in await self.meta.health(self.campaigns):
                self.store.notify("health:"+today+":"+issue.split(":")[0],issue)
            if self.settings["linkedin"]["enabled"]:
                try:
                    await self.linkedin.access_token()
                except Exception as exc:
                    self.store.notify("linkedin_health:"+today,str(exc))
            self.store.set("health_day",today)
        local = utcnow().astimezone(ZoneInfo(self.settings["summary_timezone"]))
        if not self.daily and local.strftime("%H:%M") >= self.settings["summary_time"] and self.store.get("summary_day") != local.date().isoformat():
            cutoff = local.replace(hour=0,minute=0,second=0,microsecond=0).astimezone(ZoneInfo("UTC")).isoformat()
            pubs = self.store.rows("SELECT p.platform,p.kind,p.idempotency_key,p.permalink,d.campaign FROM publications p JOIN drafts d ON d.id=p.draft_id WHERE p.status='completed' AND p.updated_at>=? ORDER BY p.updated_at",(cutoff,))
            published = "\n".join(f"{'DRY_RUN ' if p['idempotency_key'].startswith('dry:') else ''}{p['campaign']} {p['kind']} · {p['platform']}" for p in pubs[:20]) or "None"
            pending = self.store.pending()
            waiting = "\n".join(f"{d['campaign']} {d['kind']} · {d['day']} · {d['state']}" for d in pending[:10]) or "None"
            self.store.notify("summary:"+local.date().isoformat(),"Daily summary\nPublished/simulated today:\n"+published+f"\n\nPending drafts ({len(pending)}):\n"+waiting+"\n\nImage API usage: zero. Images come from you.\nText usage:"+self.status().split("API text usage today:")[-1])
            self.store.set("summary_day",local.date().isoformat())
        for row in self.store.rows("SELECT * FROM notifications WHERE sent=0 ORDER BY created_at LIMIT 10"):
            await self.telegram.send(row["text"][:4000])
            self.store.execute("UPDATE notifications SET sent=1 WHERE key=?",(row["key"],))
        self.store.set("heartbeat",stamp())
        self.store.set("status_text",self.status())

    async def recover(self):
        self.store.execute("UPDATE publications SET status='unknown' WHERE status='sending'")
        if self.daily:
            # The old scheduled drafts are retired; the daily flow never carries work over.
            self.store.execute("UPDATE drafts SET state='skipped',updated_at=? WHERE state NOT IN ('published','skipped')",(stamp(),))
            self.store.execute("UPDATE schedules SET status='skipped' WHERE status='pending'")
            self.store.execute("UPDATE image_requests SET status='expired' WHERE status='pending'")
            self.store.execute("UPDATE sessions SET state='failed',error='Interrupted while working' WHERE state IN ('generating','publishing')")
            return
        for draft in self.store.rows("SELECT * FROM drafts WHERE state='publishing'"):
            self.store.transition(draft["id"],"failed",error="Publishing interrupted. /retry reconciles known receipts; uncertain writes need /resolve.")
        for draft in self.store.rows("SELECT * FROM drafts WHERE state IN ('generating','planned')"):
            due = datetime.fromisoformat(self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"])
            if utcnow()<due:
                await self.pipeline.prepare(draft)
            else:
                await self.pipeline.skip(draft)
                self.store.notify("recovery_skip:"+draft["id"],"Skipped an interrupted draft after its publish time: "+draft["id"])
        for draft in self.store.rows("SELECT * FROM drafts WHERE state='in_review'"):
            if self.store.get("review_sent:"+draft["id"]) != draft["version"]:
                await self.telegram.review(self.campaigns[draft["campaign"]],draft)
                self.store.set("review_sent:"+draft["id"],draft["version"])
        for draft in self.store.rows("SELECT * FROM drafts WHERE state='awaiting_content'"):
            if self.store.get("content_sent:"+draft["id"]) != draft["version"]:
                await self.telegram.content_review(self.campaigns[draft["campaign"]],draft)
                self.store.set("content_sent:"+draft["id"],draft["version"])
        for draft in self.store.rows("SELECT * FROM drafts WHERE state='awaiting_image' AND delivery_mode='finished'"):
            if draft["content_approved_version"] == draft["version"] and not self.store.one("SELECT id FROM image_requests WHERE draft_id=? AND status='pending'",(draft["id"],)):
                await self.pipeline.content.request(draft)
        for row in self.store.rows("SELECT * FROM image_requests WHERE status='pending' AND message_id IS NULL"):
            draft = self.store.draft(row["draft_id"])
            if row["mode"] == "finished":
                await self.pipeline.content.request(draft)
            else:
                await self.telegram.image_request(self.campaigns[draft["campaign"]],draft,row)
        # Resume a handoff interrupted midway through uploading its files.
        for row in self.store.rows("SELECT * FROM image_requests WHERE status='pending' AND mode='finished' AND message_id IS NOT NULL"):
            draft = self.store.draft(row["draft_id"])
            directory = ROOT/"out"/"handoffs"/draft["id"]/f"v{row['version']}"
            archive = directory/f"{draft['campaign']}-{draft['kind']}-v{row['version']}.zip"
            files = sorted(path for path in directory.glob('*') if path.suffix != '.zip')
            sent = self.store.get("handoff_files:"+row["id"],[])
            if archive.is_file() and any(str(path) not in sent for path in [archive,*files]):
                await self.telegram.handoff(self.campaigns[draft["campaign"]],draft,row,archive,files)
        self.store.execute("UPDATE media_albums SET status='failed' WHERE status='processing'")

    async def run(self):
        if not self.telegram.token or self.telegram.owner<=0:
            raise ValueError("Run the setup wizard and fill secrets.txt first")
        for sig in (signal.SIGINT,signal.SIGTERM):
            signal.signal(sig,lambda *_: setattr(self,"stopping",True))
        await self.recover()
        if not self.daily:
            await self.telegram.send("Marketing Manager is online\n" + ("DRY_RUN=true. Nothing will be published." if self.settings["dry_run"] else "Live publishing enabled; owner approval required.") + "\nImages: manual ChatGPT handoff through Telegram. /help explains it.")
        while not self.stopping and not (ROOT/"data"/"stop.request").exists():
            try:
                await self.tick()
                timeout = 2 if self.store.one("SELECT id FROM media_albums WHERE status='pending'") else 25
                updates = await self.telegram.poll(self.store.get("telegram_offset",0),timeout)
                for update in updates:
                    if self.stopping or (ROOT/"data"/"stop.request").exists():
                        break
                    # Checkpoint BEFORE the action: replaying an approval/photo write is unsafe.
                    self.store.set("telegram_offset",update["update_id"]+1)
                    await self.handle(update)
            except Exception:
                log.exception("Background cycle failed; will retry")
                await asyncio.sleep(5)
        try:
            if not self.daily:
                await self.telegram.send("Marketing Manager stopped. Drafts and publication receipts are saved.")
        except Exception:
            log.info("Stopped cleanly; Telegram stop notification unavailable")
