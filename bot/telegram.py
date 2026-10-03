import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from bot.http import request
from bot.db import utcnow

HELP = """Marketing Manager
/status · accounts, tokens and next jobs
/queue · drafts and image requests
/campaigns · campaign names
/pause [campaign] · suspend publishing and generation
/resume [campaign] · continue
/generate <campaign> · request a post and story now
/publish_now <draft ID> · publish an approved draft now (simulated in DRY_RUN)
/retry <draft ID> · resume a failed publication safely
/resolve <draft ID> <facebook|instagram|linkedin> <post|story> <remote ID|not-posted>
/help · this guide

🖼 Two hours before each post/story, review copy with Approve content / Rewrite / New idea. Reply Headline:, Caption: or Visual: to revise. Photo: also updates visual directions. Caption: shorter changes caption only unless you say ‘on-image text too’.

Approve content to receive a ZIP plus individual prompt.txt, permitted logo/screenshots and style references. The prompt includes the full brand rules with CAMPAIGN INPUT filled in. Paste it into ChatGPT yourself and attach the individual files. Return the FINISHED graphic as a reply to the handoff, ZIP or any file. Send as a file for full resolution. Posts: LushNote square, AAC 4:5; stories: 9:16.

Check every word, logo and AAC footer in the final image, then use Approve / New image / New text / Skip, or reply approve / ok / 👍. Final approval is separate. Image replies to current final cards replace artwork and clear approval. Skip image uses HTML typography/screenshots. New prompt returns to content review. No image API or website automation is used.

Any ordinary message outside a draft reply starts a new idea. Send text, a screenshot/photo with an idea caption, or an album with one caption. I detect the campaign or ask LushNote / Allergy & Asthma Centre, then ask Post / Story / Both if missing.
Example, with a screenshot/album: ‘Idea for LushNote: doctors finishing notes before leaving clinic. Make it look like this. Same layout but blue background. Both’.

Groq fills the campaign input and preserves exact supplied text. Prohibited wording, such as an AAC cure claim, is rewritten with changes/reasons shown before approval. References become style_reference_1.jpg, _2.jpg, etc. Instructions follow their layout, composition and mood while keeping brand rules and never copying reference text, logos or names. Gemini may describe style as text; if it fails, the image is still attached.

Ideas use today if free, otherwise the next free day, with the date shown. Use today instead moves the idea to today and today's drafts to the next free day. Publishing/published drafts cannot move. New idea replaces the concept in the reserved slot when you reply to its prompt.

Uncaptioned images without a reply go to the sole oldest waiting request; choose with buttons if several wait. Caption ‘Use this for LushNote’ explicitly uses a source photo in the next draft's HTML layout. Older drafts retain visual-only prompts.

Thirty minutes before publishing I remind you once if the image is missing, including pending content approval.

Only approved, compliant content is published. Late approvals publish immediately on the same local day. Unapproved drafts expire at local midnight. Check DRY_RUN in /status before going live."""


def label(campaign,draft,due_at):
    local = datetime.fromisoformat(due_at).astimezone(ZoneInfo(campaign.timezone))
    return f"{campaign.name} · {draft['kind'].title()} · {local:%a} {local.day} {local:%b %H:%M %Z}"


class Telegram:
    def __init__(self,client,secrets,store):
        self.client,self.store = client,store
        self.token = secrets.get("TELEGRAM_BOT_TOKEN","")
        self.owner = int(secrets.get("TELEGRAM_OWNER_CHAT_ID") or "0")
        self.base = "https://api.telegram.org/bot" + self.token + "/"

    def authorised(self,update):
        message = update.get("message")
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message",{})
            sender = callback.get("from",{})
        else:
            sender = (message or {}).get("from",{})
        return bool(self.owner > 0 and message and message.get("chat",{}).get("id") == self.owner and sender.get("id") == self.owner)

    async def call(self,method,*,safe_retry=False,**kwargs):
        data = await request(self.client,"Telegram","POST",self.base+method,safe_retry=safe_retry,**kwargs)
        return data["result"]

    async def send(self,text,buttons=None,html_mode=False):
        from bot.runtime import redact
        text = redact(text,self.store)
        payload = {"chat_id":self.owner,"text":text,"link_preview_options":{"is_disabled":True}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard":buttons}
        if html_mode:
            payload["parse_mode"] = "HTML"
        # Telegram cannot deduplicate sendMessage after a lost acknowledgement.
        return await self.call("sendMessage",json=payload)

    async def poll(self,offset,timeout=25):
        return await self.call("getUpdates",safe_retry=True,json={"offset":offset,"timeout":timeout,"allowed_updates":["message","callback_query"]},timeout=timeout+10)

    async def answer(self,callback_id,text=""):
        return await self.call("answerCallbackQuery",safe_retry=True,json={"callback_query_id":callback_id,"text":text[:180]})

    def map_message(self,message_id,draft):
        self.store.execute("INSERT OR REPLACE INTO review_messages VALUES(?,?,?,?)",(self.owner,message_id,draft["id"],draft["version"]))

    async def content_review(self,campaign,draft):
        from bot.models import Copy
        from bot.bundles import reference_requirements
        copy = Copy.model_validate_json(draft["copy"])
        due = self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"]
        text = f"Content review: {label(campaign,draft,due)}\nDraft {draft['id']} · v{draft['version']}\n"
        width,height = campaign.story_size if draft["kind"] == "story" else campaign.post_size
        text += f"\nType: {copy.category} · Language: {copy.language}\nFormat: {width} × {height} {draft['kind']}\nPurpose: {copy.purpose or copy.topic}\nHeadline: {copy.headline}\nSupporting copy: {copy.supporting or 'NONE'}"
        if copy.body:
            text += "\nBody: " + copy.body
        if copy.items:
            text += "\nList: " + " / ".join(copy.items)
        url = "NONE" if copy.category == "EDUCATIONAL" else copy.url or campaign.website or "NONE"
        references = [{"path":row["path"],"description":self.store.get("reference_description:"+row["path"],"")}
                      for row in self.store.rows("SELECT path FROM assets WHERE draft_id=? AND kind LIKE 'style_reference_%' ORDER BY id",(draft["id"],))]
        reference_text = reference_requirements(campaign,references,self.store.get("reference_instructions:"+draft["id"],""))
        special = "\n\n".join(part for part in (copy.special_requirements,reference_text) if part) or "NONE"
        text += f"\nCTA: {copy.cta or 'NONE'}\nURL: {url}\nVisual: {copy.visual_brief}\nSpecial requirements: {special}\n\nCaption:\n{copy.full_caption}"
        if copy.category == "EDUCATIONAL":
            text += "\n\nEducational footer:\n"+campaign.required_footer_rules.get("attribution","")+"\n"+campaign.required_footer_rules.get("disclaimer","")
        elif copy.category == "INSTITUTIONAL":
            text += f"\n\nInstitutional contact: {campaign.phone}\n{campaign.address}"
        if references:
            text += f"\n\nStyle references: {len(references)} attached after content approval. Your exact reference instructions will be included."
        reasons = json.loads(draft["compliance"])
        if reasons:
            text += "\n\n⚠ Content approval blocked:\n" + "\n".join(reasons)
        warnings = json.loads(draft["warnings"])
        if warnings:
            text += "\n\nChanges and reasons:\n" + "\n".join(warnings)
        text += "\n\nReply Headline:, Caption: or Visual: to revise. Content approval prepares files; approve the finished image separately before publishing."
        prefix = f"c:{draft['id']}:{draft['version']}:"
        buttons = [[{"text":"Approve content","callback_data":prefix+"approve"},{"text":"Rewrite","callback_data":prefix+"rewrite"}],
                   [{"text":"New idea","callback_data":prefix+"new"}]]
        today = utcnow().astimezone(ZoneInfo(campaign.timezone)).date().isoformat()
        if draft["idea_group"] and draft["day"] != today:
            buttons.append([{"text":"Use today instead","callback_data":prefix+"today"}])
        for offset in range(0,len(text),3500):
            last = offset + 3500 >= len(text)
            card = await self.send(text[offset:offset+3500],buttons if last else None)
            self.map_message(card["message_id"],draft)
        return card

    async def document(self,path,draft=None,caption=None):
        path = Path(path)
        if path.stat().st_size > 49*1024*1024:
            raise ValueError(f"{path.name} exceeds Telegram's document limit. Use fewer/smaller references.")
        data = {"chat_id":str(self.owner)}
        if caption:
            data["caption"] = caption[:1024]
        message = await self.call("sendDocument",data=data,files={"document":(path.name,path.read_bytes(),"application/octet-stream")})
        if draft:
            self.map_message(message["message_id"],draft)
        return message

    async def photo(self,path,caption=None):
        path = Path(path)
        data = {"chat_id":str(self.owner)}
        if caption:
            data["caption"] = caption[:1024]
        return await self.call("sendPhoto",data=data,files={"photo":(path.name,path.read_bytes(),"image/png")})

    async def handoff(self,campaign,draft,row,archive,files):
        due = self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"]
        text = "🖼 Image needed: " + label(campaign,draft,due)
        text += "\nContent approved. Open prompt.txt and attach the individual permitted assets in the ChatGPT app. The ZIP contains the same files. Send the FINISHED artwork as a reply to this message or any attached file. Send as a file for full quality. Check every word, logo and medical footer before final approval."
        buttons = [[{"text":"Skip image","callback_data":f"i:{row['id']}:skip"},{"text":"New prompt","callback_data":f"i:{row['id']}:new"}]]
        if draft["idea_group"] and draft["day"] != utcnow().astimezone(ZoneInfo(campaign.timezone)).date().isoformat():
            buttons.append([{"text":"Use today instead","callback_data":f"i:{row['id']}:today"}])
        message = await self.send(text,buttons)
        self.store.execute("UPDATE image_requests SET message_id=? WHERE id=?",(message["message_id"],row["id"]))
        self.map_message(message["message_id"],draft)
        checkpoint = "handoff_files:"+row["id"]
        sent = self.store.get(checkpoint,[])
        for path in [archive,*files]:
            if str(path) not in sent:
                await self.document(path,draft)
                sent.append(str(path))
                self.store.set(checkpoint,sent)
        return message

    async def review(self,campaign,draft):
        from bot.models import Copy
        copy = Copy.model_validate_json(draft["copy"])
        path = Path(draft["post_path"] if draft["kind"] == "post" else draft["story_path"])
        due = self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"]
        title = label(campaign,draft,due)
        photo = await self.call("sendPhoto",data={"chat_id":str(self.owner),"caption":title},files={"photo":(path.name,path.read_bytes(),"image/png")})
        self.map_message(photo["message_id"],draft)
        reasons = json.loads(draft["compliance"])
        warnings = json.loads(draft["warnings"])
        text = f"{title}\nDraft {draft['id']} · v{draft['version']}\n\n{copy.full_caption}"
        if reasons:
            text += "\n\n⚠ Compliance blocked. Revise before approval:\n" + "\n".join(reasons)
        if warnings:
            text += "\n\n" + "\n".join(warnings)
        prefix = f"d:{draft['id']}:{draft['version']}:"
        buttons = [[{"text":"Approve","callback_data":prefix+"approve"},{"text":"New image","callback_data":prefix+"image"}],
                   [{"text":"New text","callback_data":prefix+"text"},{"text":"Skip","callback_data":prefix+"skip"}]]
        if draft["idea_group"] and draft["day"] != utcnow().astimezone(ZoneInfo(campaign.timezone)).date().isoformat():
            buttons.append([{"text":"Use today instead","callback_data":prefix+"today"}])
        card = await self.send(text[:4000],buttons)
        self.map_message(card["message_id"],draft)
        return card

    async def image_request(self,campaign,draft,request_row):
        from bot.models import Copy
        copy = Copy.model_validate_json(draft["copy"])
        due = self.store.one("SELECT due_at FROM schedules WHERE draft_id=?",(draft["id"],))["due_at"]
        text = "🖼 Image needed: " + label(campaign,draft,due)
        text += "\nHeadline: " + copy.headline + "\nReply with a photo or image file. Sending as a file preserves the full original resolution.\n\n"
        text = html.escape(text) + "<pre><code>" + html.escape(request_row["prompt"]) + "</code></pre>"
        req = request_row["id"]
        buttons = [[{"text":"Skip image","callback_data":f"i:{req}:skip"},{"text":"New prompt","callback_data":f"i:{req}:new"}]]
        message = await self.send(text,buttons,html_mode=True)
        self.store.execute("UPDATE image_requests SET message_id=? WHERE id=?",(message["message_id"],req))
        self.map_message(message["message_id"],draft)
        reasons = json.loads(draft["compliance"])
        caption = "Draft caption:\n"+copy.full_caption
        if reasons:
            caption += "\n\n⚠ Compliance blocked. Revise before approval:\n"+"\n".join(reasons)
        caption_message = await self.send(caption[:3900])
        self.map_message(caption_message["message_id"],draft)
        return message

    async def download_image(self,message,target):
        from PIL import Image
        photos = message.get("photo")
        document = message.get("document")
        if photos:
            source = max(photos,key=lambda x:x.get("width",0)*x.get("height",0))
        elif document:
            source = document
        else:
            raise ValueError("Send a photo or a PNG/JPEG/WebP image document")
        max_bytes = self.store.get("max_upload_bytes",20*1024*1024)
        if source.get("file_size",0) > max_bytes:
            raise ValueError("Image is too large for Telegram bot download. Send a file under 20 MB.")
        info = await self.call("getFile",safe_retry=True,json={"file_id":source["file_id"]})
        target.parent.mkdir(parents=True,exist_ok=True)
        total = 0
        try:
            async with self.client.stream("GET","https://api.telegram.org/file/bot" + self.token + "/" + info["file_path"],timeout=60) as response:
                response.raise_for_status()
                with target.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError("Image exceeds the download limit")
                        file.write(chunk)
            # Keep downloaded bytes untouched; verify without downscaling or recompressing.
            with Image.open(target) as img:
                if img.format not in {"PNG","JPEG","WEBP"} or img.width*img.height > 40_000_000:
                    raise ValueError("Use a PNG, JPEG or WebP under 40 megapixels")
                extension = {"PNG":".png","JPEG":".jpg","WEBP":".webp"}[img.format]
                img.verify()
            final = target.with_suffix(extension)
            target.replace(final)
            target = final
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
