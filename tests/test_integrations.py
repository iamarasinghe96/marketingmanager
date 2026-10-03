from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import httpx
from bot.config import load_config
from bot.db import Store
from bot.http import request,APIError
from bot.meta import MetaClient,UncertainPublication
from bot.telegram import Telegram
from bot.images import ManualTelegramProvider,ImagePrompt
from bot.scheduling import slots_for
from bot.selftest import sample
from bot.app import App


@pytest.fixture
def fixture(tmp_path):
    settings,campaigns,_=load_config()
    store=Store(tmp_path/"state.sqlite3");store.sync_campaigns(campaigns)
    c=campaigns["lushnote"]
    due,_=slots_for(campaigns,c,date(2026,10,3))
    draft=store.reserve_draft(c.slug,date(2026,10,3),"post",due)
    store.transition(draft["id"],"generating")
    draft=store.save_version(draft["id"],sample(c),None,None,("photo","story_photo",0,0),[],[],True)
    yield settings,campaigns,store,draft
    store.close()


def test_owner_only(fixture):
    _,_,store,_=fixture
    telegram=Telegram(None,{"TELEGRAM_OWNER_CHAT_ID":"123","TELEGRAM_BOT_TOKEN":"dummy"},store)
    assert telegram.authorised({"message":{"chat":{"id":123},"from":{"id":123}}})
    assert not telegram.authorised({"message":{"chat":{"id":123},"from":{"id":999}}})
    assert not telegram.authorised({"message":{"chat":{"id":999},"from":{"id":123}}})
    assert not telegram.authorised({"callback_query":{"from":{"id":999},"message":{"chat":{"id":123}}}})


async def test_manual_prompt_contract_and_supersession(fixture,tmp_path):
    settings,campaigns,store,draft=fixture
    telegram=SimpleNamespace(image_request=AsyncMock())
    text=SimpleNamespace(json=AsyncMock(return_value=ImagePrompt(prompt="A quiet clinical desk in natural available light.")))
    provider=ManualTelegramProvider(store,telegram,text)
    row=await provider.request(campaigns["lushnote"],draft)
    assert "1:1 square" in row["prompt"]
    assert "no text, no logos, no UI, no watermarks" in row["prompt"]
    newer=await provider.request(campaigns["lushnote"],draft,"a different desk")
    assert store.one("SELECT status FROM image_requests WHERE id=?",(row["id"],))["status"]=="superseded"
    with pytest.raises(ValueError):
        await provider.submit(row["id"],tmp_path/"photo.png")
    result=await provider.skip(newer["id"])
    assert result["id"]==draft["id"]


async def test_unknown_publication_never_replays(fixture):
    settings,campaigns,store,draft=fixture
    store.save_token("meta_page",campaigns["lushnote"].facebook_page_id,"token")
    pub=store.reserve_publication(draft["id"],"facebook","post")
    store.publication_update(pub["idempotency_key"],status="unknown",stage="facebook_feed")
    pub=store.reserve_publication(draft["id"],"facebook","post")
    client=SimpleNamespace(request=AsyncMock())
    meta=MetaClient(client,settings,{},store)
    with pytest.raises(UncertainPublication):
        await meta.publish(campaigns["lushnote"],draft,pub,"irrelevant.png","caption")
    client.request.assert_not_awaited()


async def test_completed_publication_reuses_receipt(fixture):
    settings,campaigns,store,draft=fixture
    store.save_token("meta_page",campaigns["lushnote"].facebook_page_id,"token")
    pub=store.reserve_publication(draft["id"],"facebook","post")
    store.publication_update(pub["idempotency_key"],status="completed",remote_id="42",permalink="https://www.facebook.com/42")
    pub=store.reserve_publication(draft["id"],"facebook","post")
    client=SimpleNamespace(request=AsyncMock())
    result=await MetaClient(client,settings,{},store).publish(campaigns["lushnote"],draft,pub,"none.png","caption")
    assert result[0]=="42"
    client.request.assert_not_awaited()


async def test_write_timeout_is_ambiguous():
    count=0
    def respond(req):
        nonlocal count
        count+=1
        raise httpx.ReadTimeout("lost acknowledgement",request=req)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(APIError) as info:
            await request(client,"Meta","POST","https://example.com/photos",json={})
        assert info.value.ambiguous
    assert count==1


async def test_dry_run_never_calls_publish_adapter(fixture):
    settings,campaigns,store,draft=fixture
    app=App(None,settings,campaigns,{"TELEGRAM_OWNER_CHAT_ID":"123"},store)
    app.meta.publish=AsyncMock()
    store.transition(draft["id"],"generating")
    draft=store.save_version(draft["id"],sample(campaigns["lushnote"]),"dummy.png",None,("photo","story_photo",0,0),[],[])
    store.transition(draft["id"],"approved",approved_version=draft["version"])
    await app.pipeline.publish(store.draft(draft["id"]))
    app.meta.publish.assert_not_awaited()
    assert store.draft(draft["id"])["state"]=="published"
    assert len(store.rows("SELECT * FROM publications WHERE remote_id='DRY_RUN'"))==2
