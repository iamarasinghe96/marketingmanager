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
    assert "No attachments needed" in caption  # educational: no clinic logo
    prompt = app.telegram.document.await_args_list[0].args[0].read_text(encoding="utf-8")
    assert "CAMPAIGN TYPE:\nEDUCATIONAL" in prompt and "ABSOLUTE MEDICAL COMMUNICATION RULES" in prompt
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
    assert session["caption"] == "Short and clear.\n\n#Asthma"
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


def test_clean_caption_removes_attribution_and_duplicate_disclaimer():
    campaign = load_config()[1]["allergy-asthma-centre"]
    disclaimer = campaign.required_footer_rules["disclaimer"]
    text = "Topic text.\n\n"+ATTRIBUTION+"\n\n"+disclaimer+"\n\n"+disclaimer
    cleaned = clean_caption(campaign,text)
    assert "Explained by" not in cleaned and "MBBS" not in cleaned
    assert cleaned.count(disclaimer) == 1 and cleaned.startswith("Topic text.")


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
