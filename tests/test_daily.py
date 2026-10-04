from datetime import datetime,timezone,timedelta
from unittest.mock import AsyncMock
import pytest
from PIL import Image

from bot.app import App
from bot.config import load_config
from bot.daily import clean_caption,fit,CaptionEdit,is_greeting
from bot.db import Store
from bot.selftest import sample

ATTRIBUTION = "විස්තර කරන්නේ / Explained by: Dr. Ajith Amarasinghe — MBBS, DCH, MD (Paediatrics), MRCP (UK), MRCPCH (UK), MBA (Health Care)"


@pytest.fixture
def app(tmp_path,monkeypatch):
    settings,campaigns,_ = load_config()
    settings["workflow"] = "daily"
    settings["dry_run"] = True
    store = Store(tmp_path/"daily.sqlite3")
    assert settings["daily"]["timezone"] == "Australia/Sydney"
    # The scenarios below are written in Colombo time; the flow logic is timezone-agnostic.
    settings["daily"]["timezone"] = "Asia/Colombo"
    app = App(None,settings,campaigns,{"TELEGRAM_OWNER_CHAT_ID":"123"},store)
    app.telegram.send = AsyncMock(return_value={"message_id":10})
    app.telegram.document = AsyncMock(return_value={"message_id":11})
    app.telegram.photo = AsyncMock(return_value={"message_id":12})
    aac = campaigns["allergy-asthma-centre"]
    copy = sample(aac,"en").model_copy(update={"caption":"Understanding asthma triggers.\n\n"+ATTRIBUTION,"hashtags":["#AsthmaAwareness"]})
    app.text.write = AsyncMock(return_value=copy)
    monkeypatch.setattr("bot.daily.check",AsyncMock(return_value=[]))
    monkeypatch.setattr("bot.daily.ROOT",tmp_path)
    yield app
    store.close()


def at(monkeypatch,text):
    now = datetime.fromisoformat(text)
    monkeypatch.setattr("bot.daily.utcnow",lambda:now)
    return now


def image(tmp_path,size=(1000,1250)):
    path = tmp_path/"finished.png"
    Image.new("RGB",size,"#12CFD0").save(path)
    return path


async def test_greeting_once_and_nothing_without_hi(app,monkeypatch):
    at(monkeypatch,"2026-10-05T02:00:00+00:00")  # 07:30 Colombo
    await app.daily.tick()
    app.telegram.send.assert_not_called()
    at(monkeypatch,"2026-10-05T02:31:00+00:00")  # 08:01 Colombo
    await app.daily.tick()
    await app.daily.tick()
    assert app.telegram.send.await_count == 1
    assert "hi" in app.telegram.send.await_args.args[0]
    at(monkeypatch,"2026-10-05T12:00:00+00:00")
    await app.daily.tick()
    assert app.telegram.send.await_count == 1  # no reminder without "hi"
    app.text.write.assert_not_called()


async def test_hi_sends_only_active_campaign_prompt(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"Hi"})
    # LushNote starts on 1 November; only the clinic prompt is sent.
    assert app.text.write.await_count == 1
    caption = app.telegram.document.await_args_list[0].kwargs["caption"]
    assert caption.startswith("Allergy & Asthma Centre")
    assert "Attach: logo.png" in caption  # the clinic logo goes on every post
    assert app.telegram.document.await_args_list[1].args[0].name == "logo.png"
    prompt = app.telegram.document.await_args_list[0].args[0].read_text(encoding="utf-8")
    assert "CAMPAIGN TYPE:\nEDUCATIONAL" in prompt and "ABSOLUTE MEDICAL COMMUNICATION RULES" in prompt
    assert "DOCTOR ATTRIBUTION:\nNONE" in prompt and "Ajith" not in prompt
    assert "EDUCATIONAL DISCLAIMER:\nNONE" in prompt and "ලියාපදිංචි" not in prompt
    assert "logo exactly as supplied on EVERY post" in prompt
    session = app.daily.sessions(app.daily.today())[0]
    assert session["state"] == "awaiting_image"
    assert "Explained by" not in session["caption"] and "#AsthmaAwareness" in session["caption"]
    await app.daily.on_text({"message_id":2,"text":"hi"})
    assert app.text.write.await_count == 1  # second "hi" does not start again


async def test_lushnote_joins_from_november(app,monkeypatch):
    at(monkeypatch,"2026-11-01T03:00:00+00:00")
    assert [c.slug for c in app.daily.active(app.daily.today())] == ["allergy-asthma-centre","lushnote"]


async def test_image_edit_approve_publishes_post_and_story(app,monkeypatch,tmp_path):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    await app.daily.on_photo({"message_id":2},image(tmp_path))
    session = app.daily.sessions(app.daily.today())[0]
    assert session["state"] == "awaiting_approval"
    assert Image.open(session["image_path"]).size == (1080,1350)
    assert Image.open(session["story_path"]).size == (1080,1920)
    shown = app.telegram.photo.await_args.args[1]
    assert 'Reply "approve"' in shown
    app.text.json = AsyncMock(return_value=CaptionEdit(caption="Short and clear.\n"+ATTRIBUTION,hashtags=["#Asthma"]))
    await app.daily.on_text({"message_id":3,"text":"make it shorter"})
    session = app.daily.session(session["id"])
    english = app.campaigns["allergy-asthma-centre"].required_footer_rules["disclaimers"]["en"]
    footer = app.campaigns["allergy-asthma-centre"].caption_footer
    assert session["caption"] == "Short and clear.\n\n" + english + "\n\n" + footer + "\n\n#Asthma"
    await app.daily.on_text({"message_id":4,"text":"approve"})
    session = app.daily.session(session["id"])
    assert session["state"] == "published"
    keys = {row["idempotency_key"] for row in app.store.rows("SELECT idempotency_key FROM publications WHERE status='completed'")}
    assert keys == {f"dry:{session['id']}:{p}:{k}" for p in ("facebook","instagram") for k in ("post","story")}
    assert "DRY RUN" in app.telegram.send.await_args.args[0]


async def test_live_publish_uses_meta_for_post_and_story(app,monkeypatch,tmp_path):
    app.settings["dry_run"] = False
    app.meta.publish = AsyncMock(return_value=("1","https://example.test/p"))
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    await app.daily.on_photo({"message_id":2},image(tmp_path))
    await app.daily.on_text({"message_id":3,"text":"ok"})
    calls = [(c.args[1]["kind"],c.args[2]["platform"],c.args[4]) for c in app.meta.publish.await_args_list]
    assert [(k,p) for k,p,_ in calls] == [("post","facebook"),("story","facebook"),("post","instagram"),("story","instagram")]
    assert all(caption == "" for kind,_,caption in calls if kind == "story")
    assert "Posted" in app.telegram.send.await_args.args[0]


async def test_approve_before_image_and_text_without_hi(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"approve"})
    assert "hi" in app.telegram.send.await_args.args[0]
    await app.daily.on_text({"message_id":2,"text":"hi"})
    await app.daily.on_text({"message_id":3,"text":"approve"})
    assert "image first" in app.telegram.send.await_args.args[0]


async def test_one_reminder_then_midnight_discard(app,monkeypatch,tmp_path):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    sends = app.telegram.send.await_count
    at(monkeypatch,"2026-10-05T05:00:00+00:00")
    await app.daily.tick()
    assert app.telegram.send.await_count == sends  # already started: no greeting, reminder not due yet
    at(monkeypatch,"2026-10-05T06:01:00+00:00")
    await app.daily.tick()
    await app.daily.tick()
    assert app.telegram.send.await_count == sends + 1
    assert "still waiting for the image" in app.telegram.send.await_args.args[0]
    at(monkeypatch,"2026-10-05T12:00:00+00:00")
    await app.daily.tick()
    assert app.telegram.send.await_count == sends + 1  # only once
    at(monkeypatch,"2026-10-05T19:00:00+00:00")  # 00:30 next day in Colombo
    await app.daily.tick()
    assert app.store.one("SELECT state FROM sessions")["state"] == "discarded"


def test_clean_caption_removes_attribution_and_puts_one_disclaimer_in_post_language():
    campaign = load_config()[1]["allergy-asthma-centre"]
    si = campaign.required_footer_rules["disclaimers"]["si"]
    en = campaign.required_footer_rules["disclaimers"]["en"]
    text = "Topic text.\n\n"+ATTRIBUTION+"\n\n"+si+"\n\n"+si
    cleaned = clean_caption(campaign,text,"en",educational=True)
    assert "Explained by" not in cleaned and "MBBS" not in cleaned and si not in cleaned
    assert cleaned.count(en) == 1 and cleaned.startswith("Topic text.")
    assert si not in clean_caption(campaign,text)  # institutional/no flag: no disclaimer


def test_caption_language_check():
    from bot.daily import caption_language_ok
    assert caption_language_ok("Understanding asthma triggers.","en")
    assert not caption_language_ok("ඇදුම රෝගය පිළිබඳ දැනුවත් වීම.","en")
    assert caption_language_ok("ඇදුම රෝගය පිළිබඳ දැනුවත් වීම. #Asthma","si")
    assert not caption_language_ok("Understanding asthma triggers.","si")
    assert caption_language_ok("ஆஸ்துமா பற்றிய விழிப்புணர்வு.","ta")


async def test_wrong_language_caption_is_rewritten(app,monkeypatch):
    aac = app.campaigns["allergy-asthma-centre"]
    good = sample(aac,"si").model_copy(update={"caption":"ඇදුම රෝගය පිළිබඳ දැනුවත් වීම."})
    bad = good.model_copy(update={"caption":"Understanding asthma."})
    app.text.write = AsyncMock(side_effect=[bad,good])
    at(monkeypatch,"2026-10-06T03:00:00+00:00")  # a Sinhala day
    await app.daily.on_text({"message_id":1,"text":"hi"})
    assert app.text.write.await_count == 2
    assert "Sinhala" in app.text.write.await_args_list[1].args[6]
    session = app.daily.sessions(app.daily.today())[0]
    assert session["caption"].endswith(aac.required_footer_rules["disclaimers"]["si"] + "\n\n" + aac.caption_footer)


def test_fit_pads_without_cropping(tmp_path):
    source = tmp_path/"wide.png"
    Image.new("RGB",(2000,1000),"#FF0000").save(source)
    result = fit(source,(1080,1920),"#F7F7F2")
    assert result.size == (1080,1920)
    assert result.getpixel((540,960)) == (255,0,0) and result.getpixel((540,10)) == (247,247,242)


@pytest.mark.parametrize("text,expected",[("hi",True),("Hi!",True),("hello",True),("hi there friend",False),("approve",False)])
def test_greeting(text,expected):
    assert is_greeting(text) == expected


async def test_default_greeting_is_canberra_time(tmp_path,monkeypatch):
    settings,campaigns,_ = load_config()
    store = Store(tmp_path/"act.sqlite3")
    app = App(None,settings,campaigns,{"TELEGRAM_OWNER_CHAT_ID":"123"},store)
    app.telegram.send = AsyncMock(return_value={"message_id":1})
    at(monkeypatch,"2026-10-04T20:59:00+00:00")  # 07:59 AEDT, Monday 5 Oct
    await app.daily.tick()
    app.telegram.send.assert_not_called()
    at(monkeypatch,"2026-10-04T21:00:00+00:00")  # 08:00 AEDT
    await app.daily.tick()
    assert app.telegram.send.await_count == 1
    store.close()


def test_parse_json_accepts_fences_and_extra_keys():
    from bot.generation import parse_json
    from bot.models import ComplianceVerdict
    verdict = parse_json('```json\n{"passed": true, "reasons": [], "notes": "extra"}\n```',ComplianceVerdict)
    assert verdict.passed is True


async def test_prompt_still_sent_when_ai_check_is_down(app,monkeypatch):
    from bot.compliance import UNAVAILABLE
    monkeypatch.setattr("bot.daily.check",AsyncMock(return_value=[UNAVAILABLE]))
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    assert app.daily.sessions(app.daily.today())[0]["state"] == "awaiting_image"
    assert "review the wording carefully" in app.telegram.document.await_args_list[0].kwargs["caption"]


async def test_failed_prompt_label_and_retry(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    app.text.write = AsyncMock(side_effect=ValueError("provider down"))
    await app.daily.on_text({"message_id":1,"text":"hi"})
    await app.daily.on_text({"message_id":2,"text":"hi"})
    assert 'prompt failed, reply "retry"' in app.telegram.send.await_args.args[0]
    app.text.write = AsyncMock(return_value=sample(app.campaigns["allergy-asthma-centre"],"en"))
    await app.daily.on_text({"message_id":3,"text":"retry"})
    assert app.daily.sessions(app.daily.today())[0]["state"] == "awaiting_image"


def test_parse_json_unwraps_nested_and_trims():
    from bot.generation import parse_json
    from bot.models import Copy
    raw = '{"Copy": {"category":"EDUCATIONAL","language":"en","topic":"Asthma triggers","headline":"' + "x "*100 + '","caption":"Caption.","visual_brief":"Flat illustration","visual_kind":"illustration","extra":1}}'
    copy = parse_json(raw,Copy)
    assert copy.category == "EDUCATIONAL" and len(copy.headline) <= 160


async def test_json_retries_with_validation_error(tmp_path):
    from bot.generation import TextClient
    from bot.models import ComplianceVerdict
    settings,_,_ = load_config()
    store = Store(tmp_path/"q.sqlite3")
    client = TextClient(None,settings,{"GROQ_API_KEY":"k"},store)
    client.ask_groq = AsyncMock(side_effect=['{"verdict": "ok"}','{"passed": true, "reasons": []}'])
    assert (await client.json("sys","prompt",ComplianceVerdict)).passed is True  # wrong keys -> retried
    client.ask_groq = AsyncMock(side_effect=['{"passed": "maybe"}','{"passed": true}'])
    assert (await client.json("sys","prompt",ComplianceVerdict)).passed is True
    assert "rejected" in client.ask_groq.await_args_list[1].args[1]
    store.close()


def test_hashtags_are_repaired_not_rejected():
    from bot.models import Copy
    copy = sample(load_config()[1]["allergy-asthma-centre"],"en").model_copy()
    fixed = Copy.model_validate({**copy.model_dump(),"hashtags":["AsthmaAwareness","#Allergy Care","Sri-Lanka","#AsthmaAwareness","ඇදුම‍රෝගය",""]})
    assert fixed.hashtags == ["#AsthmaAwareness","#AllergyCare","#SriLanka"]


async def test_premises_only_when_visual_is_the_building(app,monkeypatch):
    aac = app.campaigns["allergy-asthma-centre"]
    base = sample(aac,"en",category="INSTITUTIONAL")
    names = lambda copy: [name for _,name in app.daily.assets(aac,copy)]
    assert names(base.model_copy(update={"visual_kind":"premises","visual_brief":"Woman using an inhaler outdoors"})) == ["logo.png"]
    assert names(base.model_copy(update={"visual_kind":"premises","visual_brief":"The clinic building photographed from the gate"})) == ["logo.png","premises.jpg"]
    assert names(sample(aac,"en")) == ["logo.png"]


async def test_clinic_prompt_forbids_disclaimer_in_artwork(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    prompt = app.telegram.document.await_args_list[0].args[0].read_text(encoding="utf-8")
    assert prompt.index("Do NOT add any disclaimer") < prompt.index("\nCAMPAIGN INPUT\n")


async def test_gemini_first_groq_fallback(tmp_path):
    from bot.generation import TextClient
    from bot.models import ComplianceVerdict
    settings,_,_ = load_config()
    assert settings["text_routing"] == {"writing":["gemini","groq"],"checking":["groq","gemini"]}
    store = Store(tmp_path/"order.sqlite3")
    client = TextClient(None,settings,{"GROQ_API_KEY":"g","GEMINI_API_KEY":"m"},store)
    client.ask_gemini = AsyncMock(return_value='{"passed": true}')
    client.ask_groq = AsyncMock(return_value='{"passed": false}')
    assert (await client.json("s","p",ComplianceVerdict,task="writing")).passed is True
    client.ask_groq.assert_not_called()
    assert (await client.json("s","p",ComplianceVerdict)).passed is False  # checking: Groq first
    from bot.http import APIError
    client.ask_gemini = AsyncMock(side_effect=APIError("Gemini","quota"))
    assert (await client.json("s","p",ComplianceVerdict,task="writing")).passed is False  # Groq as fallback
    store.close()


def test_reset_today_keeps_published_in_live_mode(tmp_path,monkeypatch,capsys):
    import sqlite3
    from bot import reset_today
    settings,campaigns,_ = load_config()
    store = Store(tmp_path/"r.sqlite3")
    App(None,{**settings,"workflow":"daily"},campaigns,{"TELEGRAM_OWNER_CHAT_ID":"1"},store)
    day = datetime.now(timezone.utc).astimezone(__import__("zoneinfo").ZoneInfo("Australia/Sydney")).date().isoformat()
    for i,state in enumerate(["published","awaiting_image"]):
        store.execute("INSERT INTO sessions(id,campaign,day,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",(str(i),f"c{i}",day,state,"x","x"))
    store.close()
    monkeypatch.setattr(reset_today,"ROOT",tmp_path)
    monkeypatch.setattr(reset_today,"load_config",lambda:({**settings,"database":"r.sqlite3","dry_run":False},{},{}))
    reset_today.main()
    assert [r[0] for r in sqlite3.connect(tmp_path/"r.sqlite3").execute("SELECT state FROM sessions")] == ["published"]
    monkeypatch.setattr(reset_today,"load_config",lambda:({**settings,"database":"r.sqlite3","dry_run":True},{},{}))
    reset_today.main()
    assert sqlite3.connect(tmp_path/"r.sqlite3").execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


async def test_retry_while_waiting_for_image_gives_a_new_prompt(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    app.text.json = AsyncMock()
    await app.daily.on_text({"message_id":2,"text":"Retry"})
    app.text.json.assert_not_called()  # not treated as a caption edit
    assert app.text.write.await_count == 2 and app.telegram.document.await_count >= 3
    assert len(app.daily.sessions(app.daily.today())) == 1


async def test_clinic_prompt_has_contact_band_and_restraint(app,monkeypatch):
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    prompt = app.telegram.document.await_args_list[0].args[0].read_text(encoding="utf-8")
    assert "PHONE:\n077 371 0528" in prompt and "ADDRESS:\n107 Vijaya Kumarathunga Mawatha, Colombo 5" in prompt
    assert "NO PEOPLE" in prompt and prompt.index("DESIGN RESTRAINT") < prompt.index("\nCAMPAIGN INPUT\n")
    assert "style_reference" not in app.telegram.document.await_args_list[0].kwargs["caption"] and "VARIETY" in prompt


def test_hashtags_in_caption_are_moved_and_contact_allowed():
    from bot.compliance import deterministic_check
    from bot.models import Copy
    aac = load_config()[1]["allergy-asthma-centre"]
    copy = Copy.model_validate({**sample(aac,"en").model_dump(),
        "caption":"Know your triggers.\nAllergy & Asthma Centre – Colombo · 077 371 0528 · 107 Vijaya Kumarathunga Mawatha, Colombo 5\n#Asthma #AllergyCare",
        "hashtags":[]})
    assert "#" not in copy.caption and copy.hashtags == ["#Asthma","#AllergyCare"]
    assert deterministic_check(aac,copy) == []
    pitch = copy.model_copy(update={"caption":"Book an appointment today."})
    assert "Educational copy contains booking promotion" in deterministic_check(aac,pitch)


def test_sinhala_clinic_prompt_leaves_an_empty_title_area():
    from bot.bundles import filled_prompt
    aac = load_config()[1]["allergy-asthma-centre"]
    si = sample(aac,"si")
    prompt = filled_prompt(aac,{"copy":si.model_dump_json(),"kind":"post","idea":"","reference_instructions":""},[])
    assert "TITLE AREA (critical)" in prompt and "from 6% to 64% of the width and from 22% to 52%" in prompt
    assert si.headline not in prompt and "SINHALA TEXT ACCURACY" not in prompt
    en = filled_prompt(aac,{"copy":sample(aac,"en").model_dump_json(),"kind":"post","idea":"","reference_instructions":""},[])
    assert "TITLE AREA" not in en and "HEADLINE:\nUnderstanding" in en
    ta = filled_prompt(aac,{"copy":sample(aac,"ta").model_dump_json(),"kind":"post","idea":"","reference_instructions":""},[])
    assert "TITLE AREA" in ta and "Tamil" in ta.split("TITLE AREA")[1][:120]


async def test_sinhala_day_types_the_title_onto_the_image(app,monkeypatch,tmp_path):
    aac = app.campaigns["allergy-asthma-centre"]
    si = sample(aac,"si").model_copy(update={"caption":"ඇදුම රෝගය පිළිබඳ දැනුවත් වීම."})
    app.text.write = AsyncMock(return_value=si)
    calls = []
    async def fake_add_title(picture,text,language,color,box, **style):
        calls.append((text,language,color,box))
        return picture
    monkeypatch.setattr("bot.title.add_title",fake_add_title)
    at(monkeypatch,"2026-10-06T03:00:00+00:00")  # a Sinhala day
    await app.daily.on_text({"message_id":1,"text":"hi"})
    session = app.daily.sessions(app.daily.today())[0]
    copy = __import__("bot.models",fromlist=["Copy"]).Copy.model_validate_json(session["copy"])
    assert copy.supporting == "" and copy.items == []  # title only on the image
    assert "type the title in" in app.telegram.document.await_args_list[0].kwargs["caption"]
    await app.daily.on_photo({"message_id":2},image(tmp_path))
    assert calls == [(si.headline,"si",aac.palette["primary"],aac.title_box)]


async def test_english_headline_on_a_sinhala_day_is_rewritten(app,monkeypatch):
    aac = app.campaigns["allergy-asthma-centre"]
    good = sample(aac,"si").model_copy(update={"caption":"ඇදුම රෝගය පිළිබඳ දැනුවත් වීම."})
    mixed = good.model_copy(update={"headline":"Asthma Triggers","supporting":"Identifying triggers matters."})
    app.text.write = AsyncMock(side_effect=[mixed,good])
    at(monkeypatch,"2026-10-06T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    assert app.text.write.await_count == 2
    assert "on-image text must be written in Sinhala" in app.text.write.await_args_list[1].args[6]
    caption = app.daily.sessions(app.daily.today())[0]["caption"]
    assert caption.index("📞 +94 77 371 0528") < caption.index("#") if "#" in caption else "📞 +94 77 371 0528" in caption


def own_reader(app, language="en"):
    from bot.daily import OwnPost
    app.text.vision_json = AsyncMock(return_value=OwnPost(category="EDUCATIONAL",language=language,topic="Dust mites",
                                                          headline="Dust mites at home",caption="How to reduce dust mites at home.",
                                                          hashtags=["#Asthma"]))


async def test_own_post_is_captioned_and_posted_now(app,monkeypatch,tmp_path):
    own_reader(app)
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_photo({"message_id":5,"caption":"for the clinic"},image(tmp_path))
    session = app.store.one("SELECT * FROM sessions WHERE kind='own'")
    assert session["campaign"] == "allergy-asthma-centre" and session["state"] == "awaiting_approval"
    assert "How to reduce dust mites" in session["caption"] and "📞 +94 77 371 0528" in session["caption"]
    assert 'Reply "approve"' in app.telegram.photo.await_args.args[1]
    await app.daily.on_text({"message_id":6,"text":"approve"})
    assert app.daily.session(session["id"])["state"] == "published"


async def test_second_post_same_day_is_booked_for_tomorrow(app,monkeypatch,tmp_path):
    own_reader(app)
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_photo({"message_id":5,"caption":"clinic"},image(tmp_path))
    await app.daily.on_text({"message_id":6,"text":"approve"})
    await app.daily.on_photo({"message_id":7,"caption":"another clinic post"},image(tmp_path))
    await app.daily.on_text({"message_id":8,"text":"approve"})
    second = app.store.rows("SELECT * FROM sessions WHERE kind='own' ORDER BY created_at")[1]
    assert second["state"] == "scheduled" and second["publish_on"] == "2026-10-06"
    assert "booked for Tue 06 Oct at 09:00" in app.telegram.send.await_args.args[0]
    at(monkeypatch,"2026-10-06T03:00:00+00:00")  # 08:30 Colombo, before 09:00
    await app.daily.tick()
    assert app.daily.session(second["id"])["state"] == "scheduled"
    at(monkeypatch,"2026-10-06T03:31:00+00:00")  # 09:01 Colombo
    await app.daily.tick()
    assert app.daily.session(second["id"])["state"] == "published"


async def test_photo_goes_to_waiting_daily_prompt_unless_marked_own(app,monkeypatch,tmp_path):
    own_reader(app)
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    await app.daily.on_text({"message_id":1,"text":"hi"})
    await app.daily.on_photo({"message_id":2},image(tmp_path))
    assert app.daily.sessions(app.daily.today(),kind="daily")[0]["state"] == "awaiting_approval"
    app.text.vision_json.assert_not_called()
    await app.daily.on_photo({"message_id":3,"caption":"own post for the clinic"},image(tmp_path))
    assert app.store.one("SELECT COUNT(*) n FROM sessions WHERE kind='own'")["n"] == 1


async def test_unknown_campaign_asks_with_buttons(app,monkeypatch,tmp_path):
    from bot.daily import WhichCampaign
    at(monkeypatch,"2026-10-05T03:00:00+00:00")
    app.text.vision_json = AsyncMock(return_value=WhichCampaign(campaign="unknown"))
    await app.daily.on_photo({"message_id":2},image(tmp_path))
    buttons = app.telegram.send.await_args.args[1]
    assert {b[0]["callback_data"].split(":")[2] for b in buttons} == {"allergy-asthma-centre","lushnote"}
    own_reader(app)
    await app.daily.callback({"data":buttons[0][0]["callback_data"]})
    assert app.store.one("SELECT state FROM sessions WHERE kind='own'")["state"] == "awaiting_approval"


def test_old_sessions_table_is_migrated(tmp_path):
    import sqlite3
    from bot.daily import migrate
    conn = sqlite3.connect(tmp_path/"old.sqlite3")
    conn.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY, campaign TEXT NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL,
        copy TEXT, caption TEXT, image_path TEXT, story_path TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(campaign, day));
        INSERT INTO sessions(id,campaign,day,state,created_at,updated_at) VALUES('a','c','2026-10-04','published','x','x');""")
    migrate(conn)
    assert conn.execute("SELECT kind FROM sessions WHERE id='a'").fetchone()[0] == "daily"
    conn.execute("INSERT INTO sessions(id,campaign,day,state,created_at,updated_at,kind) VALUES('b','c','2026-10-04','x','x','x','own')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sessions(id,campaign,day,state,created_at,updated_at) VALUES('d','c','2026-10-04','x','x','x')")


def test_partial_contact_lines_are_replaced_by_the_full_block():
    aac = load_config()[1]["allergy-asthma-centre"]
    text = "Dust mites matter.\n\n🏥 Allergy & Asthma Centre – Colombo\n📍 107 Vijaya Kumarathunga Mawatha, Colombo 00500\n📞 +94 77 371 0528"
    cleaned = clean_caption(aac,text,"en",educational=True)
    assert cleaned.count("📞") == 1 and cleaned.count("📍") == 1 and "Colombo 00500" not in cleaned
    for line in aac.caption_footer.splitlines():
        assert line in cleaned
    assert cleaned.startswith("Dust mites matter.")


async def test_hi_tamil_picks_the_language_and_reset_clears(app,monkeypatch):
    aac = app.campaigns["allergy-asthma-centre"]
    ta = sample(aac,"ta").model_copy(update={"caption":"ஆஸ்துமா பற்றிய விழிப்புணர்வு."})
    app.text.write = AsyncMock(return_value=ta)
    at(monkeypatch,"2026-10-05T03:00:00+00:00")  # normally an English day
    await app.daily.on_text({"message_id":1,"text":"hi tamil"})
    assert app.text.write.await_args.kwargs["language"] == "ta"
    assert app.daily.sessions(app.daily.today())[0]["state"] == "awaiting_image"
    await app.command("/reset")
    assert app.daily.sessions(app.daily.today()) == []
    app.settings["dry_run"] = False
    await app.daily.on_text({"message_id":2,"text":"hi"})
    await app.command("/reset")
    assert "only works in dry run" in app.telegram.send.await_args.args[0] and app.daily.sessions(app.daily.today())


def test_sinhala_hashtags_are_moved_whole_without_leaving_fragments():
    from bot.models import Copy
    aac = load_config()[1]["allergy-asthma-centre"]
    caption = "ඇදුම පිළිබඳ දැනුවත් වන්න. 077 371 0528. #ආසාත්මිකතා #සෞඛ්‍යය #ඇදුම #Asthma"
    copy = Copy.model_validate({**sample(aac,"si").model_dump(),"caption":caption,"hashtags":[]})
    assert copy.caption == "ඇදුම පිළිබඳ දැනුවත් වන්න. 077 371 0528."
    assert copy.hashtags == ["#ආසාත්මිකතා","#ඇදුම","#Asthma"]  # the joiner tag (සෞඛ්‍යය) is dropped
