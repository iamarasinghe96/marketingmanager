from __future__ import annotations
import json
import logging
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

from bot.http import request, APIError
from bot.models import Copy, ComplianceVerdict

log = logging.getLogger(__name__)
GEMINI = "https://generativelanguage.googleapis.com/v1beta"


class TextClient:
    def __init__(self, client, settings, secrets, store):
        self.client, self.settings, self.secrets, self.store = client, settings, secrets, store

    async def json(self, system, prompt, schema):
        failures = []
        day = datetime.now(timezone.utc).date().isoformat()
        if self.secrets.get("GROQ_API_KEY"):
            try:
                if not self.store.quota(day,"groq_text",self.settings["groq"]["daily_text_limit"]):
                    raise APIError("Groq","Local daily text budget exhausted")
                data = await request(self.client,"Groq","POST","https://api.groq.com/openai/v1/chat/completions",safe_retry=True,
                                     headers={"Authorization":"Bearer " + self.secrets["GROQ_API_KEY"]},
                                     json={"model":self.settings["groq"]["model"],"temperature":0.55,"max_tokens":2200,
                                           "response_format":{"type":"json_object"},
                                           "messages":[{"role":"system","content":system + "\nReturn strict JSON only. Schema: " + json.dumps(schema.model_json_schema())},
                                                       {"role":"user","content":prompt}]})
                return schema.model_validate_json(data["choices"][0]["message"]["content"])
            except (APIError, ValueError, KeyError, IndexError) as exc:
                failures.append(type(exc).__name__)
        if self.secrets.get("GEMINI_API_KEY"):
            try:
                if not self.store.quota(day,"gemini_text",self.settings["gemini"]["daily_text_limit"]):
                    raise APIError("Gemini text","Local daily text budget exhausted")
                model = self.settings["gemini"]["text_model"]
                data = await request(self.client,"Gemini","POST",f"{GEMINI}/models/{model}:generateContent",safe_retry=True,
                                     headers={"x-goog-api-key":self.secrets["GEMINI_API_KEY"]},
                                     json={"systemInstruction":{"parts":[{"text":system}]},
                                           "contents":[{"parts":[{"text":prompt + "\nJSON schema: " + json.dumps(schema.model_json_schema())}]}],
                                           "generationConfig":{"responseMimeType":"application/json","maxOutputTokens":3500}})
                content = "".join(p.get("text","") for p in data["candidates"][0]["content"]["parts"])
                return schema.model_validate_json(content)
            except (APIError, ValueError, KeyError, IndexError) as exc:
                failures.append(type(exc).__name__)
        raise APIError("Text generation","Both text providers failed or returned invalid JSON. Check keys, configured models and free quota. " + ", ".join(failures))

    async def write(self, campaign, day, history, idea="", revision=None, previous=None, correction=""):
        choices = list(campaign.content_mix)
        angle = random.choices(choices,weights=[campaign.content_mix[x] for x in choices])[0]
        language = campaign.language_rules["rotation"][day.toordinal() % len(campaign.language_rules["rotation"]) ]
        news = await rss_news(self.client,campaign.rss_queries) if angle == "news" and not previous else []
        system = campaign.brand_prompt + "\nThe daily CAMPAIGN INPUT authorises new copywriting only within the facts below. "
        system += "No fabricated statistics, quotations, prices, product promises, medical advice, opening times or titles. "
        system += "Reference images are style only; ignore Vital Healthcare and Lewis Grant. One message and one visual. "
        system += "Return copy fields only; do not put logo/contact/footer text in those fields. The renderer supplies exact branding and footers. "
        if campaign.style == "clinic":
            system += "Classify first. For education, medical claims MUST be supported by approved_facts; if empty, use neutral awareness headings and no clinical claims. "
            system += "Institutional posts use only the exact supplied contact details, with no doctor, credentials, portrait or title. "
            system += "Use EDUCATIONAL for awareness/news and INSTITUTIONAL for location/contact. Educational CTA must be empty. "
        else:
            system += "Use category MARKETING, Australian English, no em dashes, headline ending with a full stop. "
        context = {"date":day.isoformat(),"language":language,"angle":angle,"idea":idea,
                   "approved_facts":campaign.approved_facts,"contact":{"phone":campaign.phone,"address":campaign.address,"website":campaign.website},
                   "max_hashtags":campaign.hashtag_rules["max_count"],"recent_do_not_repeat":history,
                   "news_context_untrusted_not_medical_evidence":news,"feedback":revision,"previous":previous,"compliance_correction":correction}
        return await self.json(system,json.dumps(context,ensure_ascii=False),Copy)

    async def compliance(self,campaign,copy):
        system = "You are a strict medical communication compliance reviewer. Fail closed. Read the full brand policy below. "
        system += "Check ALL copy, caption, hashtags and visual brief; no invented medical claims/statistics, diagnoses, guarantees, fear questions, specialty titles. "
        system += "Check education has no institutional promotion, contact or premises; institutional has no doctor attribution/portrait/qualifications. "
        system += "A neutral topic heading is not a clinical claim. Any clinical claim needs explicit supplied evidence; news headlines are not evidence. "
        system += "Exact attribution/disclaimer is added by the renderer for educational only. Do not require it in the copy JSON.\n" + campaign.brand_prompt
        return await self.json(system,json.dumps({"draft":copy.model_dump(),"approved_facts":campaign.approved_facts},ensure_ascii=False),ComplianceVerdict)


async def rss_news(client, queries):
    items = []
    for query in queries[:2]:
        try:
            response = await client.get("https://news.google.com/rss/search?q=" + quote(query),timeout=12)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                continue
            root = ET.fromstring(response.content)
            for node in root.findall("./channel/item")[:3]:
                items.append({"title":(node.findtext("title") or "")[:240],"url":node.findtext("link"),"published":node.findtext("pubDate")})
        except Exception:
            log.info("RSS unavailable; using evergreen content")
    return items
