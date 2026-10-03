"""One-time interactive loopback OAuth. No browser automation."""
import asyncio
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,HTTPServer
import secrets as random_secrets
import time
from urllib.parse import urlencode,urlparse,parse_qs
import webbrowser
import httpx
from bot.config import ROOT,load_config
from bot.db import Store
from bot.http import request
from bot.runtime import is_running,redact


def main():
    if is_running():
        raise ValueError("Stop Marketing Manager before authorising LinkedIn")
    settings,campaigns,keys = load_config()
    if not keys.get("LINKEDIN_CLIENT_ID") or not keys.get("LINKEDIN_CLIENT_SECRET"):
        raise ValueError("Fill LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET in secrets.txt first")
    redirect = settings["linkedin"]["redirect_uri"]
    if redirect != "http://localhost:8765/callback":
        raise ValueError("Register and use http://localhost:8765/callback exactly")
    state = random_secrets.token_urlsafe(32)
    result = {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            url = urlparse(self.path)
            params = parse_qs(url.query)
            if url.path != "/callback" or params.get("state",[""])[0] != state:
                self.send_error(400,"Invalid OAuth callback")
                return
            result["code"] = params.get("code",[""])[0]
            result["error"] = params.get("error",[""])[0]
            self.send_response(200);self.send_header("Content-Type","text/plain; charset=utf-8");self.end_headers()
            self.wfile.write(b"Authorisation received. Return to the setup window; you can close this tab.")
    url = "https://www.linkedin.com/oauth/v2/authorization?"+urlencode({"response_type":"code","client_id":keys["LINKEDIN_CLIENT_ID"],
          "redirect_uri":redirect,"state":state,"scope":"w_organization_social r_organization_social"})
    print("Open this URL in the VPS browser over RDP:\n"+url)
    with HTTPServer(("127.0.0.1",8765),Handler) as server:
        server.timeout = 1
        webbrowser.open(url)
        deadline = time.monotonic()+600
        while not result and time.monotonic()<deadline:
            server.handle_request()
    if not result.get("code"):
        raise ValueError("LinkedIn authorisation cancelled or timed out. Check approved access/scopes and retry.")
    async def exchange():
        async with httpx.AsyncClient(timeout=30) as client:
            data = await request(client,"LinkedIn OAuth","POST","https://www.linkedin.com/oauth/v2/accessToken",
                                 data={"grant_type":"authorization_code","code":result["code"],"redirect_uri":redirect,
                                       "client_id":keys["LINKEDIN_CLIENT_ID"],"client_secret":keys["LINKEDIN_CLIENT_SECRET"]})
            db = Store(ROOT/settings["database"])
            try:
                from bot.linkedin import LinkedInClient
                await LinkedInClient(client,settings,keys,db).introspect(data["access_token"])
                now = datetime.now(timezone.utc)
                db.save_token("linkedin","access",data["access_token"],(now+timedelta(seconds=data["expires_in"])).isoformat())
                if data.get("refresh_token"):
                    db.save_token("linkedin","refresh",data["refresh_token"],(now+timedelta(seconds=data.get("refresh_token_expires_in",31536000))).isoformat())
                print("✓ LinkedIn credentials exchanged and tokens saved. Refresh is automatic when the app receives a refresh token.")
            finally:
                db.close()
    asyncio.run(exchange())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(redact(str(exc)))
        raise SystemExit(1)
