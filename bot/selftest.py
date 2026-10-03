"""Render every layout offline, including native Sinhala and Tamil variants."""
import asyncio
import html
import sys
from bot.config import ROOT,load_config
from bot.models import Copy
from bot.render import rendering_browser,render_one,select_visual


def sample(campaign,language="en",category=None):
    if campaign.style == "lushnote":
        return Copy(category="MARKETING",language="en",topic="Clinical documentation",headline="Notes.\nMade simple.",
                    supporting="Clinical documentation for Australian clinicians.",caption="A sample layout for LushNote.",
                    visual_brief="Actual supplied app screenshot",visual_kind="screenshot")
    category = category or "EDUCATIONAL"
    headlines = {"en":"Understanding\nasthma", "si":"ඇදුම පිළිබඳ\nඅවබෝධය", "ta":"ஆஸ்துமாவைப்\nபுரிந்துகொள்வோம்"}
    supporting = {"en":"A public health education topic.","si":"පොදු සෞඛ්‍ය දැනුවත් කිරීමක්.","ta":"பொது சுகாதார விழிப்புணர்வு."}
    institutional = {"en":"Here in\nColombo.","si":"කොළඹ පිහිටි\nඅපගේ මධ්‍යස්ථානය.","ta":"கொழும்பில்\nஎங்கள் நிலையம்."}
    return Copy(category=category,language=language,topic="Sample awareness" if category=="EDUCATIONAL" else "Sample location",
                headline=headlines[language] if category=="EDUCATIONAL" else institutional[language],
                supporting=supporting[language] if category=="EDUCATIONAL" else "",
                caption="Offline design sample; no medical claims or API calls.",visual_brief="Simple editorial still-life",
                visual_kind="illustration" if category=="EDUCATIONAL" else "premises")


async def main():
    from bot.runtime import below_normal_priority
    below_normal_priority()
    _,campaigns,_ = load_config()
    folder = ROOT/"out"/"samples"
    folder.mkdir(parents=True,exist_ok=True)
    visual = folder/"editorial-still-life.svg"
    visual.write_text('''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="800" viewBox="0 0 900 800">
    <rect width="900" height="800" fill="#d8eeea"/>
    <path d="M0 650H900V800H0Z" fill="#c7dfda"/>
    <path d="M300 650L280 450H610L590 650Z" fill="#0B4257"/>
    <path d="M445 460V220" fill="none" stroke="#0B4257" stroke-width="12"/>
    <path d="M445 330Q250 325 285 130Q450 150 445 330" fill="#12CFD0"/>
    <path d="M445 420Q650 390 620 230Q455 240 445 420" fill="#63bca5"/>
    </svg>''',encoding="utf-8")
    links = []
    async with rendering_browser() as browser:
        for campaign in campaigns.values():
            cases = [("en","MARKETING")] if campaign.style=="lushnote" else [(language,category) for language in ("en","si","ta") for category in ("EDUCATIONAL","INSTITUTIONAL")]
            for language,category in cases:
                copy = sample(campaign,language,category)
                supplied = select_visual(campaign,copy,visual,folder)
                for kind,layouts in (("post",campaign.feed_layouts),("story",campaign.story_layouts)):
                    for i,layout in enumerate(layouts):
                        name = f"{campaign.slug}-{language}-{category.lower()}-{kind}-{layout}.png"
                        await render_one(browser,campaign,copy,layout,i%8,supplied,folder/name,kind)
                        print(name)
                        links.append(f'<figure><a href="{name}"><img loading="lazy" src="{name}"></a><figcaption>{html.escape(name)}</figcaption></figure>')
    (folder/"index.html").write_text('<!doctype html><meta charset="utf-8"><title>Marketing Manager layout samples</title><style>body{font:14px sans-serif;background:#eef1f3;margin:28px}main{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:24px}figure{margin:0}img{width:100%;height:400px;object-fit:contain;background:white}figcaption{padding:8px;overflow-wrap:anywhere}</style><h1>Offline layout samples</h1><p>Real supplied logos, screenshots and clinic crop. Sinhala and Tamil are shaped by Chromium. No API calls.</p><main>'+''.join(links)+'</main>',encoding="utf-8")
    print(f"\nRendered {len(links)} samples. Open out/samples/index.html.")


if __name__ == "__main__":
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8",errors="replace")
    asyncio.run(main())
