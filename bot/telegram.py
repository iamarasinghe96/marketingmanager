import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from bot.http import request

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

🖼 Two hours before each post/story, I send an image prompt. Copy the code block into the ChatGPT app yourself, then reply here with the result as a photo or file. Files preserve full original quality. I never use an image API or automate ChatGPT.

Skip image uses a supplied screenshot or typography layout. New prompt rewrites the image request. Thirty minutes before publishing I remind you once if the image is still missing.

After composition, use Approve / New image / New text / Skip. Reply approve, ok or 👍 to approve. Post and story have separate requests and approvals, so each can be reviewed before its own deadline.

Photo: your direction → a fresh manual image request.
Caption: shorter → revise caption. Say ‘on-image text too’ to change the artwork wording. You can send both instructions on separate lines.
Any image reply to a review card replaces that draft's visual.
An image without a reply goes to the oldest waiting image request; if several are waiting, choose with buttons.
Photo caption ‘Use this for LushNote’ reserves it for that campaign's next free draft.

Idea: your topic for LushNote → draft now; today's occupied slots move it to the next free day. Free text on a pending draft is revision feedback. Reply to a specific card when several drafts exist.

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

    async def poll(self,offset):
        return await self.call("getUpdates",safe_retry=True,json={"offset":offset,"timeout":25,"allowed_updates":["message","callback_query"]},timeout=35)

    async def answer(self,callback_id,text=""):
        return await self.call("answerCallbackQuery",safe_retry=True,json={"callback_query_id":callback_id,"text":text[:180]})

    def map_message(self,message_id,draft):
        self.store.execute("INSERT OR REPLACE INTO review_messages VALUES(?,?,?,?)",(self.owner,message_id,draft["id"],draft["version"]))

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
