from contextlib import asynccontextmanager
from datetime import date,datetime,timezone,timedelta
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import httpx
from PIL import Image

from bot.app import App
from bot.config import load_config
from bot.db import Store,stamp
from bot.images import ImagePrompt
from bot.models import Copy
from bot.selftest import sample
from bot.scheduling import slots_for
from bot.telegram import Telegram


@pytest.fixture
def app(tmp_path,monkeypatch):
    settings,campaigns,_=load_config()
    campaigns={"lushnote":campaigns["lushnote"]}
    db=Store(tmp_path/"flow.sqlite3")
    settings["workflow"]="scheduled"
    app=App(None,settings,campaigns,{"TELEGRAM_OWNER_CHAT_ID":"123"},db)
    app.telegram.send=AsyncMock(return_value={"message_id":1})
    app.telegram.review=AsyncMock()
    app.telegram.image_request=AsyncMock()
    app.telegram.content_review=AsyncMock()
    app.telegram.handoff=AsyncMock()
    app.text.write=AsyncMock(return_value=sample(campaigns["lushnote"]))
    app.text.json=AsyncMock(return_value=ImagePrompt(prompt="A quiet clinical desk photographed in ordinary natural light."))
    app.meta.health=AsyncMock(return_value=[])
    app.meta.publish=AsyncMock()
    app.pipeline.telegram=app.telegram
    @asynccontextmanager
    async def fake_browser():
        yield object()
    async def fake_render(browser,campaign,copy,layout,variant,visual,target,kind):
        target.write_bytes(b"rendered PNG fixture")
    monkeypatch.setattr("bot.pipeline.rendering_browser",fake_browser)
    monkeypatch.setattr("bot.pipeline.render_one",fake_render)
    monkeypatch.setattr("bot.pipeline.select_visual",lambda campaign,copy,provided,out_dir:provided)
    monkeypatch.setattr("bot.pipeline.ROOT",tmp_path)
    monkeypatch.setattr("bot.content.ROOT",tmp_path)
    monkeypatch.setattr("bot.images.ROOT",tmp_path)
    yield app
    db.close()


def clock(monkeypatch,now):
    monkeypatch.setattr("bot.app.utcnow",lambda:now)
    monkeypatch.setattr("bot.pipeline.utcnow",lambda:now)


async def test_request_two_hours_before_and_remind_once(app,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=2,minutes=1))
    await app.tick()
    assert not app.store.rows("SELECT * FROM drafts")
    clock(monkeypatch,due-timedelta(hours=2))
    await app.tick()
    draft=app.store.one("SELECT * FROM drafts WHERE kind='post'")
    assert draft["state"]=="awaiting_content"
    app.telegram.content_review.assert_awaited_once()
    await app.pipeline.content.approve(draft)
    app.telegram.handoff.assert_awaited_once()
    clock(monkeypatch,due-timedelta(minutes=30))
    await app.tick();await app.tick()
    assert len(app.store.rows("SELECT * FROM notifications WHERE key LIKE 'image_reminder:%'"))==1
    clock(monkeypatch,due)
    await app.tick()
    app.meta.publish.assert_not_awaited()
    assert app.store.draft(draft["id"])["state"]=="awaiting_image"
    assert len(app.store.rows("SELECT * FROM notifications WHERE key LIKE 'approval_reminder:%'"))==1


async def test_skip_image_caption_revision_approval_dry_run(app,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    draft=app.pipeline.reserve(app.campaigns["lushnote"],date(2026,10,3),"post")
    await app.pipeline.prepare(draft)
    req=app.store.one("SELECT * FROM image_requests WHERE status='pending'")
    draft=await app.images.skip(req["id"])
    await app.pipeline.compose(draft,skip_image=True)
    draft=app.store.draft(draft["id"])
    assert draft["state"]=="in_review"
    original=Copy.model_validate_json(draft["copy"])
    app.text.write.return_value=original.model_copy(update={"headline":"Unexpected changed headline.","caption":"Short caption."})
    await app.pipeline.prepare(draft,{"caption":"shorter","photo":"","feedback":""})
    revised=app.store.draft(draft["id"])
    assert Copy.model_validate_json(revised["copy"]).headline==original.headline
    assert Copy.model_validate_json(revised["copy"]).caption=="Short caption."
    await app.pipeline.approve(revised)
    clock(monkeypatch,due+timedelta(minutes=10))
    await app.tick()
    assert app.store.draft(draft["id"])["state"]=="published"
    app.meta.publish.assert_not_awaited()


async def test_replacement_invalidates_approval_and_preserves_original(app,tmp_path,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    draft=app.pipeline.reserve(app.campaigns["lushnote"],date(2026,10,3),"post")
    await app.pipeline.prepare(draft)
    original=tmp_path/"image.png";original.write_bytes(b"unchanged original")
    await app.attach_image(app.store.draft(draft["id"]),original)
    draft=app.store.draft(draft["id"])
    await app.pipeline.approve(draft)
    newer=tmp_path/"new.png";newer.write_bytes(b"new original")
    await app.attach_image(app.store.draft(draft["id"]),newer)
    draft=app.store.draft(draft["id"])
    assert draft["state"]=="in_review" and draft["approved_version"] is None
    assert original.read_bytes()==b"unchanged original"
    assert newer.read_bytes()==b"new original"


async def test_stale_card_cannot_approve(app,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    draft=app.pipeline.reserve(app.campaigns["lushnote"],date(2026,10,3),"post")
    await app.pipeline.prepare(draft)
    draft=app.store.draft(draft["id"])
    await app.pipeline.compose(draft,skip_image=True)
    current=app.store.draft(draft["id"])
    app.telegram.answer=AsyncMock()
    with pytest.raises(ValueError,match="outdated"):
        await app.callback({"id":"cb","data":f"d:{current['id']}:{current['version']-1}:approve"})
    assert app.store.draft(current["id"])["approved_version"] is None


async def test_unapproved_expiry_at_local_midnight(app,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    draft=app.pipeline.reserve(app.campaigns["lushnote"],date(2026,10,3),"post")
    await app.pipeline.prepare(draft)
    clock(monkeypatch,datetime(2026,10,3,14,1,tzinfo=timezone.utc))
    await app.tick()
    assert app.store.draft(draft["id"])["state"]=="skipped"
    assert app.store.one("SELECT status FROM image_requests WHERE draft_id=?",(draft["id"],))["status"]=="expired"


async def test_campaign_image_next_free_slot(app,tmp_path,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    # Own incoming image is saved before copy generation and consumed once.
    image=tmp_path/"real.png";image.write_bytes(b"real photo original")
    await app.idea("Use this for LushNote",image=image)
    incoming=app.store.one("SELECT * FROM incoming_images")
    assert incoming["campaign"]=="lushnote" and incoming["draft_id"]
    assert app.store.draft(incoming["draft_id"])["state"]=="in_review"
    assert image.read_bytes()==b"real photo original"
    assert len(app.store.rows("SELECT * FROM drafts"))==2


async def test_restart_does_not_replay_uncertain_write(app,monkeypatch):
    due,_=slots_for(app.campaigns,app.campaigns["lushnote"],date(2026,10,3))
    clock(monkeypatch,due-timedelta(hours=1))
    draft=app.pipeline.reserve(app.campaigns["lushnote"],date(2026,10,3),"post")
    await app.pipeline.prepare(draft)
    await app.pipeline.compose(app.store.draft(draft["id"]),skip_image=True)
    draft=app.store.draft(draft["id"])
    app.store.transition(draft["id"],"approved",approved_version=draft["version"])
    app.store.transition(draft["id"],"publishing")
    pub=app.store.reserve_publication(draft["id"],"facebook","post")
    app.store.publication_update(pub["idempotency_key"],status="sending",stage="facebook_feed")
    await app.recover()
    assert app.store.draft(draft["id"])["state"]=="failed"
    assert app.store.reserve_publication(draft["id"],"facebook","post")["status"]=="unknown"
    app.meta.publish.assert_not_awaited()


@pytest.mark.parametrize("as_document",[False,True])
async def test_download_original_bytes_and_largest_photo(tmp_path,as_document):
    raw=io.BytesIO();Image.new("RGB",(320,480),"#0F568C").save(raw,format="PNG")
    original=raw.getvalue()
    def respond(req):
        if req.url.path.endswith("getFile"):
            return httpx.Response(200,json={"ok":True,"result":{"file_path":"photos/source.png"}})
        return httpx.Response(200,content=original)
    db=Store(tmp_path/"upload.sqlite3")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            telegram=Telegram(client,{"TELEGRAM_BOT_TOKEN":"fake","TELEGRAM_OWNER_CHAT_ID":"123"},db)
            message={"document":{"file_id":"original","file_size":len(original)}} if as_document else {"photo":[{"file_id":"small","width":40,"height":60},{"file_id":"large","width":320,"height":480}]}
            path=await telegram.download_image(message,tmp_path/"upload.image")
            assert path.suffix==".png" and path.read_bytes()==original
    finally:
        db.close()
