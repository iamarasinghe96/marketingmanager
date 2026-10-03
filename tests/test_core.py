from datetime import date,datetime,timezone,timedelta
from pathlib import Path
import pytest
from bot.config import load_config
from bot.db import Store
from bot.scheduling import slots_for,next_free_day,choose_layouts,local_slot
from bot.replies import parse_reply,detect_campaign
from bot.compliance import banned_matches,deterministic_check
from bot.render import largest_fitting_size,TextOverflow,compose_html,select_visual
from bot.selftest import sample


@pytest.fixture
def campaigns():
    return load_config()[1]


@pytest.fixture
def store(tmp_path):
    db=Store(tmp_path/"test.sqlite3")
    yield db
    db.close()


@pytest.mark.parametrize("text",["APPROVE","ok","Okay!","👍","approved"])
def test_approval(text):
    assert parse_reply(text).action=="approve"


def test_revision_parser():
    result=parse_reply("pHoTo - a quiet desk\ncaption- shorter please")
    assert result.photo=="a quiet desk"
    assert result.caption=="shorter please"
    assert parse_reply("Idea: after-hours notes").action=="idea"
    assert parse_reply("make the headline smaller").feedback=="make the headline smaller"


def test_campaign_detection(campaigns):
    assert detect_campaign("Use this for LushNote",campaigns)=="lushnote"
    assert detect_campaign("an idea for AAC",campaigns)=="allergy-asthma-centre"
    assert detect_campaign("lushnote and clinic",campaigns) is None
    assert detect_campaign("inline notes",campaigns) is None


def test_sydney_dst_and_colombo(campaigns):
    c=campaigns["lushnote"]
    assert local_slot(c,date(2026,10,3),"12:30").hour==2
    assert local_slot(c,date(2026,10,4),"12:30").hour==1
    a=campaigns["allergy-asthma-centre"]
    assert local_slot(a,date(2026,10,4),"10:00").strftime("%H:%M")=="04:30"


def test_stagger_collisions(campaigns):
    campaigns["allergy-asthma-centre"].post_time="07:10"
    day=date(2026,10,4)
    first,_=slots_for(campaigns,campaigns["lushnote"],day)
    second,_=slots_for(campaigns,campaigns["allergy-asthma-centre"],day)
    assert second-first>=timedelta(minutes=30)


def test_next_free_day(store,campaigns):
    c=campaigns["lushnote"]
    now=datetime(2026,10,3,0,0,tzinfo=timezone.utc)
    day=next_free_day(store,c,now,campaigns)
    assert day==date(2026,10,3)
    post,_=slots_for(campaigns,c,day)
    store.reserve_draft(c.slug,day,"post",post)
    assert next_free_day(store,c,now,campaigns)==date(2026,10,4)
    assert next_free_day(store,c,datetime(2026,10,5,5,tzinfo=timezone.utc),campaigns)==date(2026,10,6)


def test_banned_multilingual(campaigns):
    c=campaigns["allergy-asthma-centre"]
    for text in ("Do YOU have asthma?","#PermanentCure","ඔබට ඇදුමද?","உங்களுக்கு ஆஸ்துமா உள்ளதா?","100% effective"):
        assert banned_matches(text,c.banned_phrases)
    assert not banned_matches("Understanding asthma",c.banned_phrases)


def test_category_separation(campaigns):
    c=campaigns["allergy-asthma-centre"]
    copy=sample(c)
    assert not deterministic_check(c,copy)
    assert deterministic_check(c,copy.model_copy(update={"cta":"Book now"}))
    institutional=sample(c,category="INSTITUTIONAL")
    assert not deterministic_check(c,institutional)
    assert deterministic_check(c,institutional.model_copy(update={"supporting":"Dr. Ajith Amarasinghe MBBS"}))
    markup=compose_html(c,copy,"type",0,None,"post")
    assert c.logo_path.replace("/","%2F") not in markup
    assert 'class="logo"' not in markup
    assert c.phone not in markup
    assert c.required_footer_rules["attribution"] in markup
    assert c.required_footer_rules["disclaimer"] in markup
    markup=compose_html(c,institutional,"contact",0,None,"post")
    assert 'class="logo"' in markup and c.phone in markup
    assert "Dr. Ajith" not in markup


def test_autofit_bounds():
    assert largest_fitting_size(20,100,lambda size:size<=63)==63
    with pytest.raises(TextOverflow):
        largest_fitting_size(20,100,lambda size:False)


def test_layout_rotation(campaigns):
    c=campaigns["lushnote"]
    history=[]
    seen=set()
    for _ in range(31):
        f,s,fv,sv=choose_layouts(c,history[:30])
        if history:
            assert f!=history[0]["feed_layout"] and s!=history[0]["story_layout"]
        assert (f,fv) not in seen
        seen.add((f,fv))
        history.insert(0,{"feed_layout":f,"story_layout":s,"feed_variant":fv,"story_variant":sv})


def test_versions_and_state(store,campaigns):
    c=campaigns["lushnote"]
    day=date(2026,10,3);post,_=slots_for(campaigns,c,day)
    draft=store.reserve_draft(c.slug,day,"post",post)
    store.transition(draft["id"],"generating")
    draft=store.save_version(draft["id"],sample(c),None,None,("type","story_type",0,0),[],[],True)
    assert draft["state"]=="awaiting_image" and draft["version"]==1
    with pytest.raises(ValueError):
        store.transition(draft["id"],"approved")
    store.transition(draft["id"],"generating")
    draft=store.save_version(draft["id"],sample(c),"test.png",None,("type","story_type",0,0),[],[])
    assert draft["version"]==2 and draft["state"]=="in_review"
    assert len(store.rows("SELECT * FROM draft_versions"))==2


def test_idempotency_is_per_slot_platform_and_mode(store):
    first=store.reserve_publication("draft1","facebook","post")
    store.publication_update(first["idempotency_key"],status="completed",remote_id="receipt")
    second=store.reserve_publication("draft1","facebook","post")
    assert second["remote_id"]=="receipt" and second["status"]=="completed"
    store.reserve_publication("draft1","instagram","post")
    store.reserve_publication("draft1","facebook","story")
    store.reserve_publication("draft1","facebook","post",True)
    assert len(store.rows("SELECT * FROM publications"))==4


def test_quota_atomic_limit(store):
    assert store.quota("2026-10-03","groq",1)
    assert not store.quota("2026-10-03","groq",1)


def test_large_visual_uses_bounded_copy_preserving_original(tmp_path,campaigns):
    from PIL import Image
    import hashlib
    path=tmp_path/"original.png"
    Image.new("RGB",(3200,4800),"#0F568C").save(path)
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    copy=sample(campaigns["lushnote"]).model_copy(update={"visual_kind":"photo"})
    rendered=select_visual(campaigns["lushnote"],copy,path,tmp_path)
    assert rendered!=path
    with Image.open(rendered) as img:
        assert img.width<=1600 and img.height<=2400
    assert hashlib.sha256(path.read_bytes()).hexdigest()==digest


def test_third_campaign_wordmark_is_configured(campaigns):
    c=campaigns["lushnote"].model_copy(update={"slug":"another-brand","name":"Another Brand","wordmark":"ANOTHER BRAND"})
    markup=compose_html(c,sample(c),"type",0,None,"post")
    assert '<span class="wordmark">ANOTHER BRAND</span>' in markup
    assert '>LUSHNOTE</span>' not in markup
