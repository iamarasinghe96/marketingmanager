import asyncio
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import quote
from bot.http import request, APIError
from bot.meta import UncertainPublication


class LinkedInClient:
    def __init__(self,client,settings,secrets,store):
        self.client,self.settings,self.secrets,self.store = client,settings,secrets,store

    async def introspect(self,token):
        info = await request(self.client,"LinkedIn token","POST","https://www.linkedin.com/oauth/v2/introspectToken",safe_retry=True,
                             data={"client_id":self.secrets["LINKEDIN_CLIENT_ID"],"client_secret":self.secrets["LINKEDIN_CLIENT_SECRET"],"token":token})
        scopes = set(info.get("scope","").replace(","," ").split())
        if not info.get("active") or not {"w_organization_social","r_organization_social"} <= scopes:
            raise APIError("LinkedIn","Token is inactive or lacks the approved organisation posting scopes. Re-authorise with python -m bot.linkedin_auth.")
        return info

    async def access_token(self):
        row = self.store.token("linkedin","access")
        if not row:
            raise APIError("LinkedIn","Run python -m bot.linkedin_auth after Community Management access is approved.")
        expires = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
        refresh = self.store.token("linkedin","refresh")
        if expires and expires < datetime.now(timezone.utc)+timedelta(days=7):
            self.store.notify("linkedin_expiry:" + expires.date().isoformat(),"LinkedIn token expires within 7 days. Run python -m bot.linkedin_auth if automatic refresh is unavailable.")
            if refresh and (not refresh["expires_at"] or datetime.fromisoformat(refresh["expires_at"]) > datetime.now(timezone.utc)):
                data = await request(self.client,"LinkedIn OAuth","POST","https://www.linkedin.com/oauth/v2/accessToken",safe_retry=True,
                                     data={"grant_type":"refresh_token","refresh_token":refresh["value"],
                                           "client_id":self.secrets["LINKEDIN_CLIENT_ID"],"client_secret":self.secrets["LINKEDIN_CLIENT_SECRET"]})
                self.store.save_token("linkedin","access",data["access_token"],(datetime.now(timezone.utc)+timedelta(seconds=data["expires_in"])).isoformat())
                if data.get("refresh_token"):
                    self.store.save_token("linkedin","refresh",data["refresh_token"],(datetime.now(timezone.utc)+timedelta(seconds=data.get("refresh_token_expires_in",31536000))).isoformat())
                row = self.store.token("linkedin","access")
            elif expires <= datetime.now(timezone.utc):
                raise APIError("LinkedIn","Token expired. Run python -m bot.linkedin_auth.")
        return row["value"]

    async def headers(self):
        return {"Authorization":"Bearer " + await self.access_token(),"LinkedIn-Version":str(self.settings["linkedin"]["api_version"]),
                "X-Restli-Protocol-Version":"2.0.0","Content-Type":"application/json"}

    async def publish(self,campaign,draft,publication,path,caption):
        if not self.settings["linkedin"]["enabled"] or not campaign.linkedin_org_urn:
            raise APIError("LinkedIn","LinkedIn is disabled or organisation URN is missing")
        if publication["status"] == "completed":
            return publication["remote_id"],publication["permalink"]
        if publication["status"] in {"sending","unknown"}:
            raise UncertainPublication()
        key = publication["idempotency_key"]
        headers = await self.headers()
        image = publication["upload_id"]
        if not image:
            data = await request(self.client,"LinkedIn image","POST","https://api.linkedin.com/rest/images?action=initializeUpload",safe_retry=True,
                                 headers=headers,json={"initializeUploadRequest":{"owner":campaign.linkedin_org_urn}})
            value = data["value"]
            image = value["image"]
            # Images API upload host is provided by LinkedIn, not arbitrary user input.
            await request(self.client,"LinkedIn upload","PUT",value["uploadUrl"],safe_retry=True,
                          headers={"Authorization":headers["Authorization"],"Content-Type":"image/png"},content=Path(path).read_bytes())
            self.store.publication_update(key,status="prepared",upload_id=image)
        for _ in range(24):
            data = await request(self.client,"LinkedIn image","GET","https://api.linkedin.com/rest/images/" + quote(image,safe=""),safe_retry=True,headers=headers)
            if data.get("status") == "AVAILABLE":
                break
            if data.get("status") in {"PROCESSING_FAILED","WAITING_UPLOAD"}:
                raise APIError("LinkedIn","Image upload failed. Check organisation permissions.")
            await asyncio.sleep(5)
        else:
            raise APIError("LinkedIn","Image still processing. Use /retry later.")
        self.store.publication_update(key,status="sending",stage="linkedin_post")
        try:
            # LinkedIn returns the ID in a response header, not JSON.
            for attempt in range(3):
                response = await self.client.post("https://api.linkedin.com/rest/posts",headers=headers,
                                               json={"author":campaign.linkedin_org_urn,"commentary":caption,"visibility":"PUBLIC",
                                                     "distribution":{"feedDistribution":"MAIN_FEED","targetEntities":[],"thirdPartyDistributionChannels":[]},
                                                     "content":{"media":{"id":image,"title":""}},"lifecycleState":"PUBLISHED","isReshareDisabledByAuthor":False})
                if response.status_code != 429 or attempt == 2:
                    break
                await asyncio.sleep(min(30,max(2**attempt,float(response.headers.get("Retry-After","0") or 0))))
        except Exception:
            self.store.publication_update(key,status="unknown")
            raise UncertainPublication() from None
        if response.status_code != 201:
            uncertain = response.status_code >= 500
            self.store.publication_update(key,status="unknown" if uncertain else "failed")
            if uncertain:
                raise UncertainPublication()
            raise APIError("LinkedIn",f"Posting refused (HTTP {response.status_code}). Check approved API access and company Page role.")
        remote_id = response.headers.get("x-restli-id")
        if not remote_id:
            self.store.publication_update(key,status="unknown")
            raise UncertainPublication()
        permalink = "https://www.linkedin.com/feed/update/" + remote_id
        self.store.publication_update(key,status="completed",remote_id=remote_id,permalink=permalink,error=None)
        return remote_id,permalink
