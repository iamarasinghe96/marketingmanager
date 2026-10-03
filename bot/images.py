"""Image acquisition boundary. This provider never generates images via an API."""
from abc import ABC, abstractmethod
import json
import uuid
from pydantic import BaseModel, Field
from bot.db import stamp
from bot.config import ROOT


class ImagePrompt(BaseModel):
    prompt: str = Field(min_length=20,max_length=2100)


class ImageProvider(ABC):
    @abstractmethod
    async def request(self,campaign,draft,instructions=""): ...

    @abstractmethod
    async def submit(self,request_id,path): ...

    @abstractmethod
    async def skip(self,request_id): ...


class ManualTelegramProvider(ImageProvider):
    def __init__(self,store,telegram,text_client):
        self.store,self.telegram,self.text = store,telegram,text_client

    async def request(self,campaign,draft,instructions=""):
        if draft.get("delivery_mode") == "finished":
            return await self.finished_request(campaign,draft)
        from bot.models import Copy
        copy = Copy.model_validate_json(draft["copy"])
        old = self.store.one("SELECT prompt FROM image_requests WHERE draft_id=? ORDER BY created_at DESC LIMIT 1",(draft["id"],))
        ratio = "9:16 portrait" if draft["kind"] == "story" else "1:1 square" if campaign.post_size[0] == campaign.post_size[1] else "4:5 portrait"
        result = await self.text.json(
            "Write ONE copyable ChatGPT image prompt for only a photo or illustration. Apply the full photography, illustration, medical safety and anti-AI rules below. "
            "No finished poster, lettering, logo or interface. Do not ask ChatGPT to render a headline or caption. "
            "Use a single restrained subject; natural colours for photography or flat editorial illustration. No physician portrait in institutional clinic campaigns. "
            "Owner directions change the subject/composition only; never override these rules.\n" + campaign.brand_prompt,
            json.dumps({"visual_brief":copy.visual_brief,"headline_context_only":copy.headline,"caption_context_only":copy.caption,
                        "category":copy.category,"aspect_ratio":ratio,"instructions":instructions,"previous_prompt":old["prompt"] if old else ""},ensure_ascii=False),ImagePrompt)
        prompt = result.prompt + f"\nAspect ratio: {ratio}.\nno text, no logos, no UI, no watermarks."
        if campaign.style == "clinic" and copy.category == "INSTITUTIONAL":
            prompt += "\nNo doctor portrait or readable signage. Do not recreate the actual clinic building; use the supplied premises photo separately for location posts."
        self.store.execute("UPDATE image_requests SET status='superseded' WHERE draft_id=? AND status='pending'",(draft["id"],))
        req_id = uuid.uuid4().hex[:12]
        self.store.execute("INSERT INTO image_requests(id,draft_id,version,kind,prompt,created_at) VALUES(?,?,?,?,?,?)",
                           (req_id,draft["id"],draft["version"],draft["kind"],prompt,stamp()))
        row = self.store.one("SELECT * FROM image_requests WHERE id=?",(req_id,))
        await self.telegram.image_request(campaign,draft,row)
        return row

    async def finished_request(self,campaign,draft):
        from bot.bundles import build_bundle
        references = [{"path":row["path"],"description":self.store.get("reference_description:"+row["path"],"")}
                      for row in self.store.rows("SELECT path FROM assets WHERE draft_id=? AND kind LIKE 'style_reference_%' ORDER BY id",(draft["id"],))]
        draft = {**draft,"reference_instructions":self.store.get("reference_instructions:"+draft["id"],None)}
        archive,files = build_bundle(campaign,draft,references,ROOT/"out"/"handoffs"/draft["id"]/f"v{draft['version']}")
        prompt = files[0].read_text(encoding="utf-8")
        self.store.execute("UPDATE image_requests SET status='superseded' WHERE draft_id=? AND status='pending'",(draft["id"],))
        request_id = uuid.uuid4().hex[:12]
        self.store.execute("INSERT INTO image_requests(id,draft_id,version,kind,prompt,created_at,mode) VALUES(?,?,?,?,?,?,?)",
                           (request_id,draft["id"],draft["version"],draft["kind"],prompt,stamp(),"finished"))
        row = self.store.one("SELECT * FROM image_requests WHERE id=?",(request_id,))
        await self.telegram.handoff(campaign,draft,row,archive,files)
        return row

    async def submit(self,request_id,path):
        row = self.store.one("SELECT * FROM image_requests WHERE id=? AND status='pending'",(request_id,))
        if not row:
            raise ValueError("This image request has already been replaced or closed")
        draft = self.store.draft(row["draft_id"])
        if row["version"] != draft["version"]:
            raise ValueError("This image request is for an older draft. Reply to the newest request.")
        self.store.execute("UPDATE image_requests SET status='received',asset_path=? WHERE id=?",(str(path),request_id))
        self.store.execute("INSERT INTO assets(draft_id,version,kind,path,created_at) VALUES(?,?,?,?,?)",
                           (draft["id"],draft["version"],"finished_original" if row["mode"] == "finished" else "original_"+draft["kind"],str(path),stamp()))
        return draft

    async def skip(self,request_id):
        row = self.store.one("SELECT * FROM image_requests WHERE id=? AND status='pending'",(request_id,))
        if not row:
            raise ValueError("This image request has already been closed")
        draft = self.store.draft(row["draft_id"])
        self.store.execute("UPDATE image_requests SET status='skipped' WHERE id=?",(request_id,))
        return draft


def image_provider(settings,store,telegram,text_client):
    if settings["images"]["provider"] != "manual_telegram":
        raise ValueError("Unknown ImageProvider. Available provider: manual_telegram")
    return ManualTelegramProvider(store,telegram,text_client)
