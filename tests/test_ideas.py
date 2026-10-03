from datetime import date,datetime,timedelta,timezone
import hashlib
import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
from PIL import Image
import pytest

from bot.app import App
from bot.config import load_config
from bot.db import Store
from bot.generation import TextClient
from bot.models import Copy,ComplianceVerdict,ReferenceGuidance
from bot.replies import detect_formats,parse_reply
from bot.selftest import sample
from bot.scheduling import slots_for


@pytest.fixture
def app(tmp_path,monkeypatch):
    settings,campaigns,_=load_config()
    store=Store(tmp_path/'ideas.sqlite3')
    app=App(None,settings,campaigns,{'TELEGRAM_OWNER_CHAT_ID':'123'},store)
    app.telegram.send=AsyncMock(return_value={'message_id':80})
    app.telegram.answer=AsyncMock()
    app.telegram.content_review=AsyncMock()
    async def handoff(campaign,draft,row,archive,files):
        store.execute('UPDATE image_requests SET message_id=81 WHERE id=?',(row['id'],))
        store.set('handoff_files:'+row['id'],[str(path) for path in [archive,*files]])
    app.telegram.handoff=AsyncMock(side_effect=handoff)
    app.telegram.review=AsyncMock()
    app.text.write=AsyncMock(side_effect=lambda campaign,*args:sample(campaign))
    app.text.compliance=AsyncMock(return_value=ComplianceVerdict(passed=True))
    app.text.describe_reference=AsyncMock(return_value='Large left-aligned headline, one photo and generous negative space.')
    app.text.reference_guidance=AsyncMock(side_effect=lambda campaign,copy,directions:ReferenceGuidance(instructions=directions))
    now=datetime(2026,10,3,0,0,tzinfo=timezone.utc)
    for module in ('bot.app','bot.content','bot.pipeline'):
        monkeypatch.setattr(module+'.utcnow',lambda:now)
    for module in ('bot.app','bot.content','bot.images'):
        monkeypatch.setattr(module+'.ROOT',tmp_path)
    yield app
    store.close()


def image(path,size=(600,600)):
    Image.new('RGB',size,'#0F568C').save(path)
    return path


def newest(app):
    return app.store.one('SELECT * FROM drafts ORDER BY created_at DESC,rowid DESC LIMIT 1')


async def test_nonreply_is_idea_even_with_pending_draft(app):
    await app.idea('Post idea for LushNote: after-hours notes')
    first=newest(app)
    await app.text_message({'text':'Idea for LushNote: doctors finishing notes before leaving clinic'})
    choice=app.store.one("SELECT * FROM interactions WHERE kind='idea_format'")
    assert choice
    assert app.store.draft(first['id'])['version']==first['version']
    await app.callback({'id':'cb','data':f"x:{choice['id']}:story"})
    second=newest(app)
    assert second['day']=='2026-10-04' and second['kind']=='story'
    assert second['state']=='awaiting_content'
    assert not app.store.rows('SELECT * FROM image_requests')


async def test_campaign_and_format_questions_keep_references(app,tmp_path):
    ref=image(tmp_path/'reference.png')
    await app.idea('Make it look like this',references=[str(ref)])
    choice=app.store.one("SELECT * FROM interactions WHERE kind='idea_campaign'")
    await app.callback({'id':'one','data':f"x:{choice['id']}:lushnote"})
    choice=app.store.one("SELECT * FROM interactions WHERE kind='idea_format'")
    await app.callback({'id':'two','data':f"x:{choice['id']}:both"})
    drafts=app.store.rows('SELECT * FROM drafts')
    assert {draft['kind'] for draft in drafts}=={'post','story'}
    assert len({draft['idea_group'] for draft in drafts})==1
    assert all(app.pipeline.content.references(draft)[0]['path']==str(ref) for draft in drafts)
    assert not app.store.rows('SELECT * FROM interactions')


async def test_album_one_caption_all_photos_and_no_duplicates(app,tmp_path,monkeypatch):
    refs={1:image(tmp_path/'one.png'),2:image(tmp_path/'two.png'),3:image(tmp_path/'three.png')}
    app.telegram.download_image=AsyncMock(side_effect=lambda message,target:refs[message['message_id']])
    for message_id in (3,1,2,2):
        app.buffer_album({'message_id':message_id,'media_group_id':'album','photo':[{'file_id':str(message_id)}],
                          'caption':'Post for LushNote: finish clinic notes. Same layout but blue background.' if message_id==2 else ''})
    monkeypatch.setattr('bot.app.utcnow',lambda:datetime(2026,10,3,0,0,3,tzinfo=timezone.utc))
    await app.flush_albums();await app.flush_albums()
    assert len(app.store.rows('SELECT * FROM drafts'))==1
    refs_saved=app.pipeline.content.references(newest(app))
    assert [item['path'] for item in refs_saved]==[str(refs[1]),str(refs[2]),str(refs[3])]
    assert app.telegram.download_image.await_count==3
    assert app.store.one('SELECT status FROM media_albums')['status']=='completed'


async def test_bundle_exact_brand_rules_reference_names_and_story_format(app,tmp_path):
    refs=[image(tmp_path/'reference.png'),image(tmp_path/'reference2.webp')]
    await app.idea('Story for LushNote: finish notes. Same layout but blue background.\nHeadline: Home on time',references=[str(ref) for ref in refs])
    draft=newest(app)
    assert Copy.model_validate_json(draft['copy']).headline=='Home on time'
    hashes=[hashlib.sha256(ref.read_bytes()).hexdigest() for ref in refs]
    await app.pipeline.content.approve(draft)
    assert app.store.draft(draft['id'])['state']=='awaiting_image'
    assert app.store.draft(draft['id'])['approved_version'] is None
    call=app.telegram.handoff.await_args.args
    archive,files=call[-2:]
    with zipfile.ZipFile(archive) as bundle:
        names=set(bundle.namelist())
        assert {'prompt.txt','logo.png','style_reference_1.jpg','style_reference_2.jpg'}<=names
        assert any(name.startswith('screenshot_') for name in names)
        prompt=bundle.read('prompt.txt').decode('utf-8')
    assert '1080 × 1920 story (9:16)' in prompt
    brand=app.campaigns['lushnote'].brand_prompt
    assert brand.split('\nCAMPAIGN INPUT\n')[0] in prompt
    assert brand[brand.index('\nFINAL OUTPUT\n'):] in prompt
    assert '"[Exact headline]"' not in prompt
    assert 'HEADLINE:\nHome on time' in prompt
    assert 'STYLE REFERENCE ATTACHED (style_reference_1.jpg): follow its layout approach, composition, mood and visual treatment as closely as possible while keeping all LushNote brand rules (logo, colours, typography, text control). Do not copy any text, logos or brand names from the reference.' in prompt
    assert 'Same layout but blue background.' in prompt
    assert 'Large left-aligned headline' in prompt
    assert len(files)==len(names)
    assert hashes==[hashlib.sha256(ref.read_bytes()).hexdigest() for ref in refs]


async def test_vision_failure_still_exports_reference(app,tmp_path):
    ref=image(tmp_path/'ref.jpg')
    app.text.describe_reference.side_effect=RuntimeError('quota')
    await app.idea('Post for LushNote: coffee break. Make it look like this.',references=[str(ref)])
    draft=newest(app)
    await app.pipeline.content.approve(draft)
    archive=app.telegram.handoff.await_args.args[-2]
    with zipfile.ZipFile(archive) as bundle:
        assert 'style_reference_1.jpg' in bundle.namelist()


async def test_unsafe_reference_directions_are_rewritten_and_explained(app,tmp_path):
    ref=image(tmp_path/'reference.jpg')
    app.text.reference_guidance.side_effect=None
    app.text.reference_guidance.return_value=ReferenceGuidance(instructions='Same layout but deep blue background.',
        changes=['Changed red background to deep blue because LushNote designed backgrounds must follow the brand palette.'])
    await app.idea('Post for LushNote: clinic notes. Same layout but red background.',references=[str(ref)])
    draft=newest(app)
    assert 'Changed red background to deep blue' in draft['warnings']
    await app.pipeline.content.approve(draft)
    with zipfile.ZipFile(app.telegram.handoff.await_args.args[-2]) as bundle:
        prompt=bundle.read('prompt.txt').decode('utf-8')
    assert 'Same layout but deep blue background.' in prompt
    assert 'Same layout but red background.' not in prompt


async def test_content_card_shows_exported_special_requirements_and_footer(app,tmp_path):
    ref=image(tmp_path/'reference.jpg')
    app.telegram.content_review=app.telegram.__class__.content_review.__get__(app.telegram)
    ids=iter(range(80,100))
    app.telegram.send.side_effect=lambda *args,**kwargs:{'message_id':next(ids)}
    await app.idea('Post for AAC: awareness. Same layout but turquoise background.',references=[str(ref)])
    draft=newest(app)
    messages=[call.args[0] for call in app.telegram.send.await_args_list]
    text=''.join(messages)
    assert 'STYLE REFERENCE ATTACHED (style_reference_1.jpg)' in text
    assert 'Same layout but turquoise background.' in text
    assert app.campaigns[draft['campaign']].required_footer_rules['disclaimer'] in text
    assert all(len(message)<=3500 for message in messages)
    last=app.telegram.send.await_args_list[-1]
    assert last.args[1][0][0]['text']=='Approve content'
    assert len(app.store.rows('SELECT * FROM review_messages'))==len(messages)-1


async def test_aac_cure_rewrite_explained_and_category_assets_separated(app,tmp_path):
    await app.idea('Post for AAC: permanent cure for asthma\nHeadline: Permanent cure')
    draft=newest(app)
    assert not json.loads(draft['compliance'])
    assert 'prohibit' in draft['warnings'] and 'Permanent cure' in draft['warnings']
    await app.pipeline.content.approve(draft)
    archive=app.telegram.handoff.await_args.args[-2]
    with zipfile.ZipFile(archive) as bundle:
        assert 'logo.png' not in bundle.namelist()
        prompt=bundle.read('prompt.txt').decode('utf-8')
    block=prompt.split('\nCAMPAIGN INPUT\n')[1].split('\nFINAL STANDARD\n')[0]
    assert 'CAMPAIGN TYPE:\nEDUCATIONAL' in block
    assert 'LANGUAGE:\nENGLISH' in block
    assert 'PHONE:\nNONE' in block and 'CTA:\nNONE' in block
    assert app.campaigns[draft['campaign']].required_footer_rules['attribution'] in block
    assert 'Permanent cure' not in block
    institutional=sample(app.campaigns['allergy-asthma-centre'],category='INSTITUTIONAL')
    app.text.write.side_effect=None;app.text.write.return_value=institutional
    await app.idea('Story for AAC: clinic contact details')
    draft=newest(app)
    await app.pipeline.content.approve(draft)
    with zipfile.ZipFile(app.telegram.handoff.await_args.args[-2]) as bundle:
        assert 'logo.png' in bundle.namelist()
        prompt=bundle.read('prompt.txt').decode('utf-8')
    block=prompt.split('\nCAMPAIGN INPUT\n')[1].split('\nFINAL STANDARD\n')[0]
    assert 'DOCTOR ATTRIBUTION:\nNONE' in block
    assert 'EDUCATIONAL DISCLAIMER:\nNONE' in block


async def test_exact_native_text_and_body_survive_handoff(app):
    await app.idea('Post for AAC: awareness\nHeadline: ඇදුම පිළිබඳ අවබෝධය\nSubhead: පොදු සෞඛ්‍ය දැනුවත් කිරීමක්.\nBody: මෙම මාතෘකාව ගැන දැනුවත් වෙමු.')
    draft=newest(app)
    copy=Copy.model_validate_json(draft['copy'])
    assert copy.headline=='ඇදුම පිළිබඳ අවබෝධය'
    assert copy.body=='මෙම මාතෘකාව ගැන දැනුවත් වෙමු.'
    await app.pipeline.content.approve(draft)
    with zipfile.ZipFile(app.telegram.handoff.await_args.args[-2]) as bundle:
        prompt=bundle.read('prompt.txt').decode('utf-8')
    assert 'HEADLINE:\nඇදුම පිළිබඳ අවබෝධය' in prompt
    assert 'BODY:\nමෙම මාතෘකාව ගැන දැනුවත් වෙමු.' in prompt


async def test_exact_text_ending_with_internal_quote_is_preserved(app):
    await app.idea('Post for LushNote: after clinic\nHeadline: Say "done"\nCaption: Your notes, your words.')
    copy=Copy.model_validate_json(newest(app)['copy'])
    assert copy.headline=='Say "done"' and copy.caption=='Your notes, your words.'


async def test_content_gate_finished_image_replacement_and_reapproval(app,tmp_path):
    await app.idea('Post for LushNote: finishing notes')
    draft=newest(app)
    source=image(tmp_path/'finished.png')
    original=source.read_bytes()
    with pytest.raises(ValueError,match='Approve content'):
        await app.attach_image(draft,source)
    await app.pipeline.content.approve(draft)
    draft=app.store.draft(draft['id'])
    await app.attach_image(draft,source)
    draft=app.store.draft(draft['id'])
    assert draft['state']=='in_review'
    assert source.read_bytes()==original
    with Image.open(draft['post_path']) as result:
        assert result.size==(1080,1080)
    await app.pipeline.approve(draft)
    await app.attach_image(app.store.draft(draft['id']),source)
    draft=app.store.draft(draft['id'])
    assert draft['state']=='in_review' and draft['approved_version'] is None
    await app.draft_action(draft,parse_reply('Headline: Leave on time'))
    draft=app.store.draft(draft['id'])
    assert draft['state']=='awaiting_content' and draft['content_approved_version'] is None
    assert Copy.model_validate_json(draft['copy']).headline=='Leave on time'


async def test_wrong_aspect_does_not_crop_finished_copy(app,tmp_path):
    await app.idea('Story for LushNote: notes before home')
    draft=newest(app)
    await app.pipeline.content.approve(draft)
    source=image(tmp_path/'wrong.jpg')
    with pytest.raises(ValueError,match='wrong aspect ratio'):
        await app.attach_image(app.store.draft(draft['id']),source)
    assert source.is_file()
    assert app.store.draft(draft['id'])['state']=='awaiting_image'


async def test_content_rewrite_and_new_idea_callbacks_keep_the_slot(app):
    await app.idea('Post for LushNote: old topic')
    draft=newest(app)
    await app.callback({'id':'new','data':f"c:{draft['id']}:{draft['version']}:new"})
    await app.text_message({'text':'New topic: quiet mornings for LushNote','reply_to_message':{'message_id':80}})
    current=app.store.draft(draft['id'])
    assert current['idea']=='New topic: quiet mornings for LushNote'
    assert current['day']==draft['day'] and current['version']==draft['version']+1
    with pytest.raises(ValueError,match='outdated'):
        await app.callback({'id':'old','data':f"c:{draft['id']}:{draft['version']}:approve"})
    await app.callback({'id':'rewrite','data':f"c:{current['id']}:{current['version']}:rewrite"})
    assert app.store.draft(draft['id'])['version']==current['version']+1
    assert len(app.store.rows('SELECT * FROM drafts'))==1


async def test_captioned_photo_is_reference_even_when_an_image_waits(app,tmp_path):
    await app.idea('Post for LushNote: first idea')
    first=newest(app)
    await app.pipeline.content.approve(first)
    reference=image(tmp_path/'reference.png')
    app.telegram.download_image=AsyncMock(return_value=reference)
    await app.photo_message({'message_id':90,'photo':[{'file_id':'photo'}],
                             'caption':'Story for LushNote: new idea. Make it look like this.'})
    current=newest(app)
    assert current['id']!=first['id'] and current['state']=='awaiting_content'
    assert app.pipeline.content.references(current)[0]['path']==str(reference)
    assert app.store.draft(first['id'])['state']=='awaiting_image'


async def test_handoff_document_reply_maps_to_finished_draft(app,tmp_path):
    await app.idea('Post for LushNote: notes before home')
    draft=newest(app)
    app.telegram.handoff=AsyncMock(wraps=app.telegram.__class__.handoff.__get__(app.telegram))
    counter=iter(range(100,120))
    async def document(path,draft):
        message={'message_id':next(counter)}
        app.telegram.map_message(message['message_id'],draft)
        return message
    app.telegram.document=AsyncMock(side_effect=document)
    await app.pipeline.content.approve(draft)
    reference=app.store.one('SELECT * FROM review_messages WHERE message_id>=100 ORDER BY message_id DESC LIMIT 1')
    returned=image(tmp_path/'returned.png')
    app.telegram.download_image=AsyncMock(return_value=returned)
    await app.photo_message({'message_id':200,'document':{'file_id':'finished'},'reply_to_message':{'message_id':reference['message_id']}})
    assert app.store.draft(draft['id'])['state']=='in_review'
    assert not app.store.rows("SELECT * FROM interactions WHERE kind LIKE 'idea_%'")


async def test_interrupted_approved_content_handoff_recovers(app):
    await app.idea('Post for LushNote: after hours')
    draft=newest(app)
    app.store.transition(draft['id'],'awaiting_image',content_approved_version=draft['version'])
    await app.recover()
    assert app.store.one('SELECT * FROM image_requests WHERE draft_id=?',(draft['id'],))['mode']=='finished'
    app.telegram.handoff.assert_awaited_once()


async def test_generation_failure_has_retry_in_same_reserved_slot(app):
    app.text.write.side_effect=RuntimeError('Text quota exhausted')
    await app.idea('Both for LushNote: leaving clinic')
    drafts=app.store.rows('SELECT * FROM drafts')
    assert len(drafts)==2 and all(draft['state']=='failed' for draft in drafts)
    draft=drafts[0]
    app.text.write.side_effect=lambda campaign,*args:sample(campaign)
    await app.callback({'id':'retry','data':f"c:{draft['id']}:{draft['version']}:rewrite"})
    assert app.store.draft(draft['id'])['state']=='awaiting_content'
    assert len(app.store.rows('SELECT * FROM drafts'))==2


async def test_use_today_swaps_both_formats_atomically(app):
    await app.idea('Both for LushNote: clinic morning')
    existing=app.store.rows('SELECT * FROM drafts')
    await app.idea('Both for LushNote: home on time')
    idea=newest(app)
    assert idea['day']=='2026-10-04'
    await app.pipeline.content.use_today(idea)
    for old in existing:
        assert app.store.draft(old['id'])['day']=='2026-10-04'
    for new in app.store.rows('SELECT * FROM drafts WHERE idea_group=?',(idea['idea_group'],)):
        assert new['day']=='2026-10-03'
    for row in app.store.rows('SELECT * FROM drafts'):
        due=app.store.one('SELECT due_at FROM schedules WHERE draft_id=?',(row['id'],))['due_at']
        expected=slots_for(app.campaigns,app.campaigns[row['campaign']],date.fromisoformat(row['day']))[row['kind']=='story']
        assert due==expected.isoformat()
        assert app.store.one('SELECT day FROM ideas_queue WHERE draft_id=?',(row['id'],))['day']==row['day']


async def test_use_today_blocks_started_publication_without_schedule_changes(app):
    await app.idea('Post for LushNote: first idea')
    first=newest(app)
    pub=app.store.reserve_publication(first['id'],'facebook','post')
    app.store.publication_update(pub['idempotency_key'],status='unknown')
    await app.idea('Post for LushNote: next idea')
    next_idea=newest(app)
    with pytest.raises(ValueError,match='Publishing has already begun'):
        await app.pipeline.content.use_today(next_idea)
    assert app.store.draft(first['id'])['day']=='2026-10-03'
    assert app.store.draft(next_idea['id'])['day']=='2026-10-04'


async def test_idea_uses_today_free_even_after_publish_time(app,monkeypatch):
    monkeypatch.setattr('bot.content.utcnow',lambda:datetime(2026,10,3,8,0,tzinfo=timezone.utc))
    await app.idea('Post for LushNote: late idea')
    assert newest(app)['day']=='2026-10-03'


def test_format_and_revision_detection():
    assert detect_formats('Idea for LushNote')==[]
    assert detect_formats('Make a post and story for AAC')==['post','story']
    assert detect_formats('Both for LushNote')==['post','story']
    assert detect_formats('A story for AAC')==['story']
    reply=parse_reply('Headline: Home on time\nCaption: shorter\nVisual: same layout but blue background')
    assert reply.headline=='Home on time' and reply.caption=='shorter'
    assert reply.visual=='same layout but blue background'


async def test_gemini_vision_requests_only_text(app,tmp_path):
    reference=image(tmp_path/'reference.jpg')
    calls=[]
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'Simple split layout.'}]}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        text=TextClient(client,app.settings,{'GEMINI_API_KEY':'dummy'},app.store)
        assert await text.describe_reference(reference)=='Simple split layout.'
    assert calls[0]['generationConfig']['responseModalities']==['TEXT']
    assert calls[0]['contents'][0]['parts'][1]['inlineData']['mimeType']=='image/jpeg'


def test_additive_migration_keeps_legacy_visual_drafts(tmp_path):
    path=tmp_path/'old.sqlite3'
    store=Store(path)
    store.conn.execute('ALTER TABLE drafts DROP COLUMN delivery_mode')
    store.conn.execute('ALTER TABLE drafts DROP COLUMN idea_group')
    store.conn.execute('ALTER TABLE drafts DROP COLUMN content_approved_version')
    store.conn.execute('ALTER TABLE image_requests DROP COLUMN mode')
    store.conn.commit();store.close()
    store=Store(path)
    try:
        store.reserve_draft('lushnote',date(2026,10,3),'post',datetime(2026,10,3,tzinfo=timezone.utc))
        assert store.one('SELECT * FROM drafts')['delivery_mode']=='visual'
    finally:
        store.close()
