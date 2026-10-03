"""Live checks, plain-English repair instructions and token exchange."""
import asyncio
import shutil
import sys
import httpx
from bot.config import ROOT,load_config
from bot.db import Store
from bot.http import request
from bot.meta import MetaClient
from bot.runtime import is_running,redact,below_normal_priority


async def wizard():
    below_normal_priority()
    if is_running():
        print("Stop Marketing Manager first, then run setup again. Other processes are untouched.")
        return False
    if not (ROOT/"secrets.txt").exists():
        shutil.copyfile(ROOT/"secrets.example.txt",ROOT/"secrets.txt")
        print("Created secrets.txt. Fill it using the instructions in README.md, then run setup again.")
        return False
    settings,campaigns,secrets = load_config()
    store = Store(ROOT/settings["database"])
    store.sync_campaigns(campaigns)
    passed = True
    def report(name,ok,message):
        nonlocal passed
        print(f"{'✓' if ok else '✗'} {name}: {redact(message,store)}")
        passed &= ok
    async with httpx.AsyncClient(timeout=30) as client:
        bot_token = secrets.get("TELEGRAM_BOT_TOKEN")
        telegram_ok = False
        if not bot_token:
            report("TELEGRAM_BOT_TOKEN",False,"Create a bot with @BotFather /newbot and paste its token into secrets.txt.")
        else:
            try:
                result = await request(client,"Telegram","GET",f"https://api.telegram.org/bot{bot_token}/getMe",safe_retry=True)
                report("TELEGRAM_BOT_TOKEN",True,"Connected to @" + result["result"]["username"])
                webhook = await request(client,"Telegram","GET",f"https://api.telegram.org/bot{bot_token}/getWebhookInfo",safe_retry=True)
                if webhook["result"].get("url"):
                    await request(client,"Telegram","POST",f"https://api.telegram.org/bot{bot_token}/deleteWebhook",json={"drop_pending_updates":False})
                    print("Removed this bot's webhook so it can use private long polling.")
                telegram_ok = True
            except Exception:
                report("TELEGRAM_BOT_TOKEN",False,"Telegram refused the token or is offline. Copy the complete token from @BotFather; check internet access.")
        try:
            owner = int(secrets.get("TELEGRAM_OWNER_CHAT_ID",0))
            if owner<=0:
                raise ValueError()
            if not telegram_ok:
                raise ValueError()
            await request(client,"Telegram","POST",f"https://api.telegram.org/bot{bot_token}/sendMessage",
                          json={"chat_id":owner,"text":"Marketing Manager setup: Telegram connection verified. Only you can control this bot."})
            report("TELEGRAM_OWNER_CHAT_ID",True,"Private owner chat verified")
        except Exception:
            report("TELEGRAM_OWNER_CHAT_ID",False,"Send /start to your bot. Enter your positive personal chat ID; groups are not accepted.")
            if telegram_ok:
                try:
                    updates = await request(client,"Telegram","GET",f"https://api.telegram.org/bot{bot_token}/getUpdates",safe_retry=True,params={"timeout":0})
                    ids = {u["message"]["chat"]["id"] for u in updates["result"] if u.get("message",{}).get("chat",{}).get("type")=="private"}
                    for identifier in ids:
                        print(f"Private chat found: {identifier}. Use it only if this is YOUR Telegram account.")
                except Exception:
                    pass
        if secrets.get("GROQ_API_KEY"):
            try:
                await request(client,"Groq","POST","https://api.groq.com/openai/v1/chat/completions",safe_retry=True,
                              headers={"Authorization":"Bearer "+secrets["GROQ_API_KEY"]},
                              json={"model":settings["groq"]["model"],"messages":[{"role":"user","content":"Reply OK"}],"max_tokens":4})
                report("GROQ_API_KEY",True,"Live text generation passed")
            except Exception:
                report("GROQ_API_KEY",False,"Create a free key at console.groq.com/keys; check model name in config.yaml and free quota.")
        else:
            report("GROQ_API_KEY",False,"Create a free key at console.groq.com/keys.")
        if secrets.get("GEMINI_API_KEY"):
            try:
                model = settings["gemini"]["text_model"]
                await request(client,"Gemini text","POST",f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",safe_retry=True,
                              headers={"x-goog-api-key":secrets["GEMINI_API_KEY"]},json={"contents":[{"parts":[{"text":"Reply OK"}]}]})
                report("GEMINI_API_KEY",True,"Text fallback passed. This key is NEVER used for image generation.")
            except Exception:
                report("GEMINI_API_KEY",False,"Create an unbilled free-project key at aistudio.google.com/apikey. Check text model and free quota.")
        else:
            report("GEMINI_API_KEY",False,"Create a free Gemini key for text fallback. Images come only from Telegram uploads.")
        missing = False
        for key in ("META_APP_ID","META_APP_SECRET","META_USER_TOKEN"):
            if not secrets.get(key):
                missing = True
                report(key,False,"Fill this value from your Business app and Graph API Explorer. See README Meta steps.")
        if not missing:
            try:
                results = await MetaClient(client,settings,secrets,store).exchange(campaigns)
                report("META_APP_ID + META_APP_SECRET + META_USER_TOKEN",True,"App identity checked; user token upgraded")
                for slug,ok,message in results:
                    report(campaigns[slug].name + " accounts",ok,message)
            except Exception as exc:
                report("Meta authorisation",False,str(exc)+" Recreate the user token with the required permissions and selected Pages, then rerun setup.")
        if settings["linkedin"]["enabled"]:
            for key in ("LINKEDIN_CLIENT_ID","LINKEDIN_CLIENT_SECRET"):
                report(key,bool(secrets.get(key)),"Set from your approved LinkedIn app; live validation occurs during python -m bot.linkedin_auth.")
            try:
                from bot.linkedin import LinkedInClient
                row = store.token("linkedin","access")
                if not row:
                    raise ValueError("No LinkedIn token saved")
                await LinkedInClient(client,settings,secrets,store).introspect(row["value"])
                report("LinkedIn OAuth + app keys",True,"Live token introspection passed with both organisation scopes")
            except Exception:
                report("LinkedIn OAuth + app keys",False,"Run python -m bot.linkedin_auth with approved access and correct client ID/secret.")
        else:
            print("✓ LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET: optional; LinkedIn disabled.")
        try:
            from bot.render import rendering_browser
            async with rendering_browser():
                pass
            report("Fonts + Chromium",True,"Private renderer starts and closes successfully")
        except Exception:
            report("Fonts + Chromium",False,"Run install.bat again to download the private renderer and fonts.")
    print("\nSetup passed. Use the Marketing Manager desktop icon." if passed else "\nFix the ✗ items in secrets.txt, then rerun setup.bat. Nothing has been published.")
    store.close()
    return passed


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8",errors="replace")
    try:
        sys.exit(0 if asyncio.run(wizard()) else 1)
    except Exception as exc:
        print("Setup could not finish: " + redact(str(exc)))
        sys.exit(1)
