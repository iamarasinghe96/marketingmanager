from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from bot.config import load_config
from bot.db import Store
from bot.http import APIError
from bot.meta import MetaClient
from bot.linkedin import LinkedInClient
from bot.scheduling import slots_for


@pytest.fixture
def environment(tmp_path):
    settings,campaigns,_=load_config()
    store=Store(tmp_path/"contract.sqlite3")
    campaign=campaigns["lushnote"]
    store.save_token("meta_page",campaign.facebook_page_id,"page_token")
    store.sync_campaigns(campaigns)
    due,_=slots_for(campaigns,campaign,date(2026,10,3))
    draft=store.reserve_draft(campaign.slug,date(2026,10,3),"post",due)
    image=tmp_path/"rendered.png";image.write_bytes(b"fixture png")
    yield settings,campaign,store,draft,image
    store.close()


async def test_facebook_feed_write_receipt_is_durable(environment):
    settings,campaign,store,draft,image=environment
    meta=MetaClient(None,settings,{},store)
    pub=store.reserve_publication(draft["id"],"facebook","post")
    async def graph(method,path,token,**kwargs):
        row=store.reserve_publication(draft["id"],"facebook","post")
        assert row["status"]=="sending" and row["stage"]=="facebook_feed"
        assert kwargs["data"]["published"]=="true"
        assert kwargs["data"]["caption"]=="Exact caption"
        assert kwargs["files"]["source"][1]==b"fixture png"
        return {"id":"photo123","post_id":"page_post123"}
    meta.graph=AsyncMock(side_effect=graph)
    receipt=await meta.publish(campaign,draft,pub,image,"Exact caption")
    pub=store.reserve_publication(draft["id"],"facebook","post")
    assert pub["status"]=="completed" and pub["remote_id"]=="page_post123"
    assert await meta.publish(campaign,draft,pub,image,"Exact caption")==receipt
    meta.graph.assert_awaited_once()


async def test_facebook_story_uses_returned_permalink(environment):
    settings,campaign,store,draft,image=environment
    meta=MetaClient(None,settings,{},store)
    pub=store.reserve_publication(draft["id"],"facebook","story")
    async def graph(method,path,token,**kwargs):
        if path.endswith("/photos"):
            assert kwargs["data"]["published"]=="false"
            return {"id":"photo456"}
        if path.endswith("/photo_stories"):
            assert kwargs["data"]=={"photo_id":"photo456"}
            return {"success":True,"post_id":"story789"}
        assert path.endswith("/stories")
        assert store.reserve_publication(draft["id"],"facebook","story")["status"]=="completed"
        return {"data":[{"post_id":"story789","url":"https://www.facebook.com/stories/page/actual-story"}]}
    meta.graph=AsyncMock(side_effect=graph)
    _,link=await meta.publish(campaign,draft,pub,image,"caption")
    assert link=="https://www.facebook.com/stories/page/actual-story"


async def test_instagram_container_publish_and_metadata_failure(environment):
    settings,campaign,store,draft,image=environment
    meta=MetaClient(None,settings,{},store)
    pub=store.reserve_publication(draft["id"],"instagram","post")
    async def graph(method,path,token,**kwargs):
        if path.endswith("/photos"):
            return {"id":"unpublished1"}
        if path.endswith("/content_publishing_limit"):
            return {"data":[{"quota_usage":0}]}
        if path=="unpublished1":
            return {"images":[{"source":"https://cdn.example.com/photo.jpg"}]}
        if path.endswith("/media"):
            assert kwargs["data"]["image_url"]=="https://cdn.example.com/photo.jpg"
            assert kwargs["data"]["caption"]=="Approved caption"
            return {"id":"container2"}
        if path=="container2":
            return {"status_code":"FINISHED"}
        if path.endswith("/media_publish"):
            row=store.reserve_publication(draft["id"],"instagram","post")
            assert row["container_id"]=="container2" and row["status"]=="sending"
            assert kwargs["data"]=={"creation_id":"container2"}
            return {"id":"media3"}
        assert path=="media3"
        assert store.reserve_publication(draft["id"],"instagram","post")["remote_id"]=="media3"
        raise APIError("Meta","Permalink metadata unavailable")
    meta.graph=AsyncMock(side_effect=graph)
    receipt=await meta.publish(campaign,draft,pub,image,"Approved caption")
    assert receipt==("media3",None)
    pub=store.reserve_publication(draft["id"],"instagram","post")
    assert pub["status"]=="completed"
    calls=meta.graph.await_count
    assert await meta.publish(campaign,draft,pub,image,"Approved caption")==receipt
    assert meta.graph.await_count==calls


async def test_expired_container_can_retry_without_reposting_facebook(environment):
    settings,campaign,store,draft,image=environment
    meta=MetaClient(None,settings,{},store)
    pub=store.reserve_publication(draft["id"],"instagram","post")
    store.publication_update(pub["idempotency_key"],status="prepared",upload_id="u1",container_id="expired")
    pub=store.reserve_publication(draft["id"],"instagram","post")
    async def graph(method,path,token,**kwargs):
        if path.endswith("content_publishing_limit"):
            return {"data":[{"quota_usage":0}]}
        assert path=="expired"
        return {"status_code":"EXPIRED"}
    meta.graph=AsyncMock(side_effect=graph)
    with pytest.raises(APIError,match="fresh container"):
        await meta.publish(campaign,draft,pub,image,"caption")
    assert store.reserve_publication(draft["id"],"instagram","post")["container_id"] is None


async def test_meta_setup_supported_fields_and_page_token(environment):
    settings,campaign,store,_,_=environment
    secrets={"META_APP_ID":"app123","META_APP_SECRET":"appsecret","META_USER_TOKEN":"short"}
    meta=MetaClient(None,settings,secrets,store)
    scopes=["pages_show_list","pages_read_engagement","pages_manage_posts","instagram_basic","instagram_content_publish"]
    async def response(method,path,token,**kwargs):
        if path=="debug_token":
            return {"data":{"is_valid":True,"app_id":"app123","expires_at":0,"scopes":scopes}}
        assert path=="me/accounts"
        assert "account_type" not in kwargs["params"]["fields"]
        return {"data":[{"id":campaign.facebook_page_id,"access_token":"permanent","instagram_business_account":{"id":campaign.instagram_account_id,"username":"lushnote"}}]}
    meta.graph=AsyncMock(side_effect=response)
    class Client:
        async def request(self,*args,**kwargs):
            import httpx
            return httpx.Response(200,json={"access_token":"long"})
    meta.client=Client()
    reports=await meta.exchange({campaign.slug:campaign})
    assert reports[0][1] is True
    assert store.token("meta_page",campaign.facebook_page_id)["value"]=="permanent"


async def test_linkedin_introspection_checks_scopes(environment):
    settings,_,store,_,_=environment
    secrets={"LINKEDIN_CLIENT_ID":"app","LINKEDIN_CLIENT_SECRET":"secret"}
    import httpx
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"active":True,"scope":"w_organization_social,r_organization_social"}))) as client:
        assert (await LinkedInClient(client,settings,secrets,store).introspect("token"))["active"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,json={"active":False}))) as client:
        with pytest.raises(APIError,match="inactive"):
            await LinkedInClient(client,settings,secrets,store).introspect("token")
