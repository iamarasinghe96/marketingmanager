from __future__ import annotations
import asyncio
import hashlib
import hmac
from datetime import datetime, timezone
from pathlib import Path

from bot.http import request, APIError
from bot.db import stamp
from bot.config import load_config


class UncertainPublication(APIError):
    def __init__(self):
        super().__init__("Publishing", "The server may have accepted this post, but its acknowledgement was lost. Automatic retry is blocked to prevent a duplicate. Check the Page/account, then use /resolve. See README.",ambiguous=True)


class MetaClient:
    def __init__(self,client,settings,secrets,store):
        self.client,self.settings,self.secrets,self.store = client,settings,secrets,store
        self.base = "https://graph.facebook.com/" + settings["meta"]["graph_version"]

    def page_token(self,campaign):
        row = self.store.token("meta_page",campaign.facebook_page_id)
        if not row or not row["valid"]:
            raise APIError("Meta", "Page token missing or invalid. Run the setup wizard with a fresh META_USER_TOKEN.")
        return row["value"]

    async def graph(self,method,path,token,**kwargs):
        params = kwargs.pop("params",{})
        if self.secrets.get("META_APP_SECRET"):
            params["appsecret_proof"] = hmac.new(self.secrets["META_APP_SECRET"].encode(),token.encode(),hashlib.sha256).hexdigest()
        return await request(self.client,"Meta",method,self.base + "/" + path,
                             safe_retry=method == "GET",headers={"Authorization":"Bearer " + token},params=params,**kwargs)

    async def exchange(self,campaigns):
        app_token = self.secrets["META_APP_ID"] + "|" + self.secrets["META_APP_SECRET"]
        short = self.secrets["META_USER_TOKEN"]
        info = await self.graph("GET","debug_token",app_token,params={"input_token":short})
        debug = info["data"]
        if not debug.get("is_valid") or str(debug.get("app_id")) != self.secrets["META_APP_ID"]:
            raise APIError("Meta", "User token is invalid or belongs to a different app. Select your Business app in Graph API Explorer.")
        long = await request(self.client,"Meta OAuth","GET",self.base + "/oauth/access_token",safe_retry=True,
                             params={"grant_type":"fb_exchange_token","client_id":self.secrets["META_APP_ID"],
                                     "client_secret":self.secrets["META_APP_SECRET"],"fb_exchange_token":short})
        user_token = long["access_token"]
        self.store.save_token("meta_user","owner",user_token)
        pages = {}
        after = None
        while True:
            params = {"fields":"id,name,access_token,tasks,instagram_business_account{id,username}","limit":100}
            if after:
                params["after"] = after
            result = await self.graph("GET","me/accounts",user_token,params=params)
            pages.update({p["id"]:p for p in result.get("data",[])})
            if not result.get("paging",{}).get("next"):
                break
            after = result["paging"]["cursors"]["after"]
        reports = []
        needed = {"pages_show_list","pages_read_engagement","pages_manage_posts","instagram_basic","instagram_content_publish"}
        missing = needed-set(debug.get("scopes",[]))
        if missing:
            raise APIError("Meta","User token needs permissions: " + ", ".join(sorted(missing)) + ". Regenerate it in Graph API Explorer.")
        for c in campaigns.values():
            if not c.enabled:
                continue
            page = pages.get(c.facebook_page_id)
            if not page or not page.get("access_token"):
                reports.append((c.slug,False,"Page not granted to this token. Grant the configured Page and ensure you are its admin."))
                continue
            token = page["access_token"]
            page_debug = (await self.graph("GET","debug_token",app_token,params={"input_token":token}))["data"]
            if not page_debug.get("is_valid"):
                reports.append((c.slug,False,"Page token did not validate."))
                continue
            ig = page.get("instagram_business_account") or {}
            if str(ig.get("id")) != c.instagram_account_id:
                reports.append((c.slug,False,"Linked Instagram ID does not match campaign.yaml. Connect an Instagram Business account to this Facebook Page."))
                continue
            expires = page_debug.get("expires_at",0)
            expiry = datetime.fromtimestamp(expires,timezone.utc).isoformat() if expires else None
            self.store.save_token("meta_page",c.facebook_page_id,token,expiry)
            label = "Page + Instagram connected. Ensure Instagram is Business for story publishing. " + ("Token has no scheduled expiry (revocable)." if not expires else "Token has an expiry; daily checks will alert you.")
            reports.append((c.slug,True,label))
        return reports

    async def health(self,campaigns):
        app_token = self.secrets.get("META_APP_ID","") + "|" + self.secrets.get("META_APP_SECRET","")
        results = []
        for c in campaigns.values():
            row = self.store.token("meta_page",c.facebook_page_id)
            if not c.enabled:
                continue
            if not row:
                results.append(f"{c.name}: no Page token. Run python -m bot.setup.")
                continue
            try:
                data = (await self.graph("GET","debug_token",app_token,params={"input_token":row["value"]}))["data"]
                valid = bool(data.get("is_valid")) and str(data.get("app_id")) == self.secrets.get("META_APP_ID")
                self.store.execute("UPDATE tokens SET valid=?,checked_at=? WHERE service='meta_page' AND account=?", (int(valid),stamp(),c.facebook_page_id))
                if not valid:
                    results.append(f"{c.name}: Page authorisation expired/revoked. Get a new META_USER_TOKEN in Graph API Explorer and rerun the setup wizard.")
            except APIError:
                results.append(f"{c.name}: unable to verify token. Check internet and Meta app settings, then rerun setup.")
        return results

    async def instagram_limit(self,campaign,token):
        data = await self.graph("GET",campaign.instagram_account_id + "/content_publishing_limit",token,
                                params={"fields":"quota_usage,config"})
        usage = data.get("data",[{}])[0].get("quota_usage",0)
        # Respect the brief's conservative 25-post ceiling even if Meta permits more.
        if usage >= self.settings["meta"]["instagram_daily_limit"]:
            raise APIError("Instagram","The 24-hour publishing limit has been reached. Wait for quota to reset.")
        cutoff = datetime.now(timezone.utc).timestamp()-86400
        account_campaigns = [slug for slug,c in load_config()[1].items() if c.instagram_account_id==campaign.instagram_account_id]
        placeholders = ','.join('?' for _ in account_campaigns)
        reservations = self.store.rows(f"SELECT p.updated_at FROM publications p JOIN drafts d ON d.id=p.draft_id WHERE p.platform='instagram' AND p.status IN ('sending','unknown','completed') AND p.idempotency_key LIKE 'live:%' AND d.campaign IN ({placeholders})",account_campaigns)
        recent = sum(datetime.fromisoformat(r["updated_at"]).timestamp() >= cutoff for r in reservations)
        if recent >= self.settings["meta"]["instagram_daily_limit"]:
            raise APIError("Instagram","Local 24-hour publishing budget reached. Wait for quota to reset.")

    async def mutate(self,publication,stage,path,token,**kwargs):
        key = publication["idempotency_key"]
        self.store.publication_update(key,status="sending",stage=stage,error=None)
        try:
            result = await self.graph("POST",path,token,**kwargs)
        except APIError as exc:
            self.store.publication_update(key,status="unknown" if exc.ambiguous else "failed",error=str(exc))
            raise
        return result

    async def upload(self,publication,campaign,path,token,published=False,caption=""):
        result = await self.mutate(publication,"facebook_feed" if published else "upload",
                                   campaign.facebook_page_id + "/photos",token,
                                   data={"published":"true" if published else "false","caption":caption},
                                   files={"source":(Path(path).name,Path(path).read_bytes(),"image/png")})
        photo_id = str(result["id"])
        self.store.publication_update(publication["idempotency_key"],status="prepared",upload_id=photo_id,
                                      remote_id=str(result.get("post_id",photo_id)) if published else None)
        return photo_id,result.get("post_id",photo_id)

    async def poll_container(self,container_id,token):
        for _ in range(24):
            result = await self.graph("GET",container_id,token,params={"fields":"status_code,status"})
            status = result.get("status_code")
            if status in {"FINISHED","PUBLISHED"}:
                return status
            if status in {"ERROR","EXPIRED"}:
                raise APIError("Instagram","Media container failed or expired. Use /retry to prepare a fresh container for the same approved artwork; check image format and account permissions.",code=status)
            await asyncio.sleep(5)
        raise APIError("Instagram","Image is still processing. Use /retry later; the existing container will be reused.",transient=True)

    async def publish(self,campaign,draft,publication,path,caption):
        key = publication["idempotency_key"]
        platform,kind = publication["platform"],publication["kind"]
        token = self.page_token(campaign)
        if publication["status"] == "completed":
            return publication["remote_id"],publication["permalink"]
        if publication["status"] in {"sending","unknown"}:
            # No provider idempotency support for Meta photo/stories; fail closed.
            if platform == "instagram" and publication["stage"] == "instagram_publish" and publication["container_id"]:
                status = await self.poll_container(publication["container_id"],token)
                if status == "PUBLISHED":
                    self.store.publication_update(key,status="completed",remote_id=publication["container_id"],
                                                  permalink=None,error="Published confirmed by container; permalink unavailable.")
                    return publication["container_id"],None
                # FINISHED after a timeout is still not proof that an in-flight call failed.
            raise UncertainPublication()
        if platform == "facebook" and kind == "post":
            # A successful feed upload is itself the final publish operation.
            if publication["remote_id"]:
                remote_id = publication["remote_id"]
            else:
                _,remote_id = await self.upload(publication,campaign,path,token,True,caption)
            permalink = "https://www.facebook.com/" + str(remote_id)
        else:
            photo_id = publication["upload_id"]
            if not photo_id:
                photo_id,_ = await self.upload(publication,campaign,path,token)
            if platform == "facebook":
                result = await self.mutate(publication,"facebook_story",campaign.facebook_page_id + "/photo_stories",token,data={"photo_id":photo_id})
                remote_id = result.get("post_id") or result.get("id")
                if not remote_id:
                    self.store.publication_update(key,status="unknown",error="Story acknowledgement did not contain a publication ID")
                    raise UncertainPublication()
                remote_id = str(remote_id)
                self.store.publication_update(key,status="completed",remote_id=remote_id)
                # A story post_id is not a public story URL. Read Meta's actual URL.
                permalink = None
                try:
                    stories = await self.graph("GET",campaign.facebook_page_id + "/stories",token,params={"fields":"post_id,url"})
                    permalink = next((s.get("url") for s in stories.get("data",[]) if str(s.get("post_id"))==remote_id),None)
                except APIError:
                    pass
            else:
                await self.instagram_limit(campaign,token)
                container = publication["container_id"]
                if not container:
                    images = await self.graph("GET",photo_id,token,params={"fields":"images"})
                    image_url = images["images"][0]["source"]
                    params = {"image_url":image_url}
                    if kind == "story":
                        params["media_type"] = "STORIES"
                    else:
                        params["caption"] = caption
                    result = await self.mutate(publication,"instagram_container",campaign.instagram_account_id + "/media",token,data=params)
                    container = str(result["id"])
                    self.store.publication_update(key,status="prepared",container_id=container)
                try:
                    status = await self.poll_container(container,token)
                except APIError as exc:
                    if exc.code in {"ERROR","EXPIRED"}:
                        self.store.publication_update(key,status="prepared",container_id=None,error=str(exc))
                    raise
                if status == "PUBLISHED":
                    raise UncertainPublication()
                result = await self.mutate(publication,"instagram_publish",campaign.instagram_account_id + "/media_publish",token,data={"creation_id":container})
                remote_id = str(result["id"])
                # Persist the receipt before fetching optional permalink metadata.
                self.store.publication_update(key,status="completed",remote_id=remote_id)
                try:
                    media = await self.graph("GET",remote_id,token,params={"fields":"permalink"})
                    permalink = media.get("permalink")
                except APIError:
                    permalink = None
        self.store.publication_update(key,status="completed",remote_id=str(remote_id),permalink=permalink,error=None)
        return str(remote_id),permalink
