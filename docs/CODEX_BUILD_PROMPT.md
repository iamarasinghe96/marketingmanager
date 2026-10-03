# Build prompt: Marketing Manager bot

Tell Codex: "Read `docs/CODEX_BUILD_PROMPT.md` on branch `claude/kind-goodall-d0zj62` and build exactly what it describes." Everything it needs (brand prompts, logos, screenshots, reference posts) is already in the repo.

---

## Goal

Build a **working, production-ready** social-media marketing bot (not a dashboard, not a mockup). It runs unattended on my **Windows Server VPS**, generates one feed post + one story per campaign per day, sends each draft to me on **Telegram** for review, applies my revisions, and publishes approved content to **Facebook Pages and Instagram** (LinkedIn later). I only review; everything else is automatic.

Repository: `iamarasinghe96/marketingmanager`. Start from branch `claude/kind-goodall-d0zj62`, which already contains all brand assets in `assets/`. Ignore/delete any earlier React dashboard prototype; it is not wanted.

## Hard constraints

- **Language/stack:** Python 3.12, single process, no Docker, no external database server. Use SQLite for state.
- **Free services only:** Groq API (text), Gemini API (images, and text fallback), Telegram Bot API, Meta Graph API. No OpenAI API, no ChatGPT automation, no paid services.
- **No browser automation of Facebook/Instagram/LinkedIn logins.** Official APIs only.
- **No inbound internet access needed.** Telegram uses long polling (no webhook). Do not open ports or require a domain.
- **Never commit secrets.** `secrets.txt` and the SQLite DB must be in `.gitignore`.

## Target machine

- Windows Server, 64-bit, 4 GB RAM, KVM VPS in Sydney, accessed via Remote Desktop as `Administrator`.
- **A forex trading bot already runs on this machine and must never be interrupted.** Therefore:
  - Use a private virtual environment in the project folder; do not change global Python, PATH, or system packages beyond installing Python 3.12 if missing (via `winget`, user-confirmed).
  - Run the bot process at **BelowNormal priority**.
  - Keep memory under ~400 MB: launch the headless browser only while rendering, then close it.
  - Never kill, restart, or touch any other process. Single-instance lock so double-clicking the icon cannot start two copies.
  - No reboots, no Windows updates, no firewall changes.

## One-click experience (very important)

The user is not a developer. Deliver:

1. **`install.bat`** (double-click once). It:
   - checks for Python 3.12 (offers `winget install Python.Python.3.12` if missing),
   - creates `.venv`, installs requirements, installs Playwright Chromium,
   - downloads fonts into `fonts/` if not present,
   - runs the **setup wizard** (below),
   - creates a **desktop shortcut "Marketing Manager"** (with an icon) and a **"Stop Marketing Manager"** shortcut,
   - registers a Task Scheduler task so the bot starts automatically at boot (runs whether or not the user is logged in, BelowNormal priority).
2. **Desktop icon "Marketing Manager"**: if the bot is not running, start it in the background (hidden window, `pythonw`); if it is running, say so. Either way, show a small status window (or message box) with: running/stopped, connected accounts, next scheduled jobs, and a "View log" button. The bot also sends "Marketing Manager is online" to Telegram on start.
3. **Setup wizard** (`python -m bot.setup`, run by the installer and re-runnable): reads `secrets.txt`, validates every key with a live test call, and prints a clear ✓/✗ per item with plain-English fix instructions. Then exchanges the Meta token for permanent Page tokens (see Meta section) and stores them.

## Secrets file

`secrets.txt` in the project root, `KEY=value` per line. Create `secrets.example.txt` with comments explaining where each value comes from:

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_OWNER_CHAT_ID=
GROQ_API_KEY=
GEMINI_API_KEY=
META_APP_ID=
META_APP_SECRET=
META_USER_TOKEN=          # short-lived token from Graph API Explorer; wizard upgrades it
LINKEDIN_CLIENT_ID=       # leave blank until LinkedIn approves
LINKEDIN_CLIENT_SECRET=
```

Only messages from `TELEGRAM_OWNER_CHAT_ID` are ever processed; ignore everyone else silently.

## Campaigns (config-driven)

Each campaign is a folder `campaigns/<slug>/` with `campaign.yaml` + `brand_prompt.md` + references to `assets/<slug>/`. Adding a third campaign must require **only a new folder**, no code changes.

`campaign.yaml` includes: name, enabled, timezone, language rules, post size, story size, post time, story time, Facebook Page ID, Instagram account ID, LinkedIn org URN (blank), palette, fonts, logo path, screenshots, reference posts, content mix, hashtag rules, banned phrases, required footer rules.

### LushNote (`campaigns/lushnote/`)
- Clinical documentation / note-taking app for doctors and clinicians in **Australia**. Australian English. **No em dashes.** URL `www.lushnote.com.au`.
- Timezone `Australia/Sydney`. Feed post 12:30, story 18:30 (configurable).
- Facebook Page ID `1377201872144623`, Instagram ID `17841415037476125`.
- Post 1080×1080; story 1080×1920.
- Palette: white `#FFFFFF`, deep blue `#0F568C`, mint `#5CD5A8`, bright blue `#0687EF`, dark grey `#333333`.
- Fonts: headline Montserrat ExtraBold/Black (tight tracking), body Inter Regular, wordmark "LUSHNOTE" in a bold condensed sans (e.g. Oswald/Bebas Neue/Archivo Narrow Bold).
- Assets: `assets/lushnote/logo.png` (LN mark; the lockup is LN mark + "LUSHNOTE" set in type), app screenshots `assets/lushnote/screenshot-*.{png,jpg}` (use exactly as supplied, never redraw UI), style references `assets/lushnote/past-posts/*.webp`.
- Observed style from references: huge tight bold headline ending with a full stop ("Eyes up.", "Letters? Too easy."), one short grey supporting line, logo lockup, URL, one visual (phone mockup with real screenshot, real photo, flat illustration, or none), at most one motif (bright-blue organic corner blob, thin blue+mint wavy lines, blue dot grid). White background most of the time; deep-blue background for promotional posts.
- Follow the full LushNote brand prompt (pasted at the end) for all content and image decisions.

### Allergy & Asthma Centre – Colombo (`campaigns/allergy-asthma-centre/`)
- Sri Lankan clinic. Timezone `Asia/Colombo`. Feed post 10:00, story 17:00 (configurable). Stagger so no two campaigns publish within 30 minutes of each other.
- Facebook Page ID `1054872354373231`, Instagram ID `17841451553167746`.
- Post 1080×1350; story 1080×1920.
- Palette: turquoise ≈ `#12CFD0`, dark teal ≈ `#0B4257`, off-white ≈ `#F7F7F2`, logo green only from the logo file.
- Fonts: English headline Montserrat/Poppins Bold with a light-weight companion line; Sinhala **Noto Sans Sinhala** (or Noto Serif Sinhala for headlines); Tamil **Noto Sans Tamil**.
- Assets: `assets/allergy-asthma-centre/logo.png`, `premises.webp`, style references `past-posts/*.png`.
- Every post is classified **EDUCATIONAL** or **INSTITUTIONAL** first, and the rules are never mixed:
  - EDUCATIONAL: no clinic logo, phone, address, website, booking CTA, premises photo or prices. May include the approved doctor attribution + disclaimer footer.
  - INSTITUTIONAL: logo, phone `077 371 0528`, address `107 Vijaya Kumarathunga Mawatha, Colombo 5`, premises photo allowed. No doctor name, credentials, portrait or title.
- Approved footer (use exactly, EDUCATIONAL only):
  - Disclaimer: `මෙය පොදු සෞඛ්‍ය දැනුවත් කිරීමක් පමණි; වෛද්‍ය උපදෙසක් හෝ රෝග විනිශ්චයක් නොවේ. ඔබේ රෝග ලක්ෂණ සම්බන්ධයෙන් ලියාපදිංචි වෛද්‍යවරයෙකුගෙන් උපදෙස් ලබා ගන්න.`
  - Attribution (**approved, final**): `විස්තර කරන්නේ / Explained by: Dr. Ajith Amarasinghe — MBBS, DCH, MD (Paediatrics), MRCP (UK), MRCPCH (UK), MBA (Health Care)`
- Medical safety: never claim or imply cure/guaranteed results; no fear-based or diagnostic questions to the reader ("Do you have asthma?" / "ඔබට ඇදුමද?" is banned); no invented statistics; no specialty titles. Enforce with (a) a banned-phrase list in English/Sinhala/Tamil and (b) an LLM compliance check that must pass before a draft is sent for review. On failure, regenerate up to 2 times, then send to me flagged with the reason.
- Premises photo: when used, crop to the clinic side (left/centre). The neighbouring Wijaya Pharmacy and LHD signs must not appear prominently.
- Ignore "Vital Healthcare" / "Lewis Grant" in the reference images; those are template leftovers.
- Language: configurable rotation, default English and Sinhala alternating; Tamil supported. Sinhala/Tamil must be rendered with correct shaping (see rendering).
- Follow the full Allergy & Asthma Centre brand prompt (pasted at the end).

## Daily pipeline

For each enabled campaign, each day:

1. **Plan** (morning, local time, well before the post time): pick a content angle from the mix: marketing quotes/insights, relevant news (free RSS, e.g. Google News RSS queries configured per campaign; LushNote: clinical documentation/AI scribes/Australian healthcare; AAC: asthma/allergy/air quality/pollen in Sri Lanka), product features, relatable moments, education, institutional info. Avoid repeating any headline/topic/layout used in the last 30 days (store history).
2. **Write** (Groq, e.g. `llama-3.3-70b-versatile`; model name in config; fall back to Gemini text): headline, supporting copy, CTA (optional), caption, hashtags (caption only, ≤12 for AAC, ≤5 for LushNote), and a visual brief. Output strict JSON, validate with Pydantic.
3. **Visual** (Gemini image model, name in config, e.g. `gemini-2.5-flash-image`): generate **only the photo or illustration**, with **no text, no logos, no UI** in it. Fresh image for the post, and a story version (same theme, 9:16). Track daily image usage; if quota is exhausted or the call fails, fall back to a typography-only or screenshot-based layout and tell me in the Telegram message.
4. **Compose** with deterministic HTML/CSS templates rendered by Playwright Chromium to PNG (this gives correct Sinhala/Tamil shaping and exact fonts). Real logo file, real screenshots, exact text. Provide **at least 6 feed layouts and 3 story layouts per campaign**, matching the references and the brand prompts (e.g. typographic poster, photo in rounded rectangle, full-bleed photo with text panel, phone mockup with real screenshot, circle-crop illustration, numbered educational slide, institutional contact card with premises photo). Never use the same layout twice in a row for a campaign. Text must never overflow: auto-fit font size within min/max bounds and re-break lines.
5. **Review on Telegram:** send the post image + caption and the story image as one review card, labelled e.g. `LushNote · Post · Sat 4 Oct 12:30 AEST`, with inline buttons **Approve**, **New image**, **New text**, **Skip**. Then wait.
6. **Publish** at the scheduled time **only if approved**. If not approved by the scheduled time, do not publish. Remind me once, and publish immediately after a later approval (same day), or skip at midnight local time.

### My Telegram replies

The reply applies to the draft I reply to; if I don't use Telegram's reply feature, apply it to the most recent pending draft, and if there are several pending, ask me with buttons which one.

- `approve` / `ok` / `👍` → approve (post + story).
- `Photo: <instructions>` → regenerate the visual with my instructions, recompose, resend.
- `Caption: <instructions>` → rewrite caption (and on-image text if I say so), resend.
- Both in one message (two lines) → apply both.
- Anything else on a draft → treat as general revision feedback.
- Case-insensitive; tolerate typos like `photo -`, `caption-`.

### Ideas

If I send a message that is not a reply to a draft, e.g. `Idea: post about after-hours notes for LushNote` or just free text describing an idea:
- Detect the campaign (ask with buttons if unclear).
- Generate a draft from my idea and send it for review immediately.
- If that campaign already has a draft for today's slot, schedule my idea for the **next free day** and tell me the date.

### Commands
`/status` (accounts, tokens, quotas, next jobs), `/queue` (upcoming drafts), `/pause` and `/resume` (all or one campaign), `/generate <campaign>` (make a draft now), `/campaigns`, `/help`.

## Meta (Facebook + Instagram) publishing

- App type Business, development mode is enough (I am admin of the Pages). Graph API version in config (currently v26.0).
- **Token setup (wizard):** exchange `META_USER_TOKEN` for a long-lived user token (`oauth/access_token?grant_type=fb_exchange_token` with app id/secret), then call `me/accounts` to get **non-expiring Page tokens** for each configured Page ID. Store them in the DB (not in git). Daily health check (`debug_token`); alert me on Telegram if a token is invalid, with re-auth steps.
- **Facebook feed post:** `POST /{page-id}/photos` with the image file (multipart) + caption.
- **Hosting images for Instagram without a public server:** upload the PNG to the Page as an unpublished photo (`published=false`), read its CDN URL (`/{photo-id}?fields=images`), and pass that URL to Instagram.
- **Instagram feed post:** `POST /{ig-id}/media` (`image_url`, `caption`) → poll container `status_code` until `FINISHED` → `POST /{ig-id}/media_publish`.
- **Instagram story:** same flow with `media_type=STORIES`.
- **Facebook story:** upload unpublished photo → `POST /{page-id}/photo_stories` with `photo_id`.
- Respect Instagram's 25 API posts/24h limit. Retry transient errors with exponential backoff (max 3); never double-post (idempotency key per draft+platform stored before the call; check before retrying).
- After publishing, send me a Telegram confirmation with the permalinks. On failure, send the error in plain English plus what I should do.

## LinkedIn (built, disabled by default)

- Company Page posting only (no stories). Behind `linkedin.enabled: false` until my Community Management API access is approved.
- When enabled: a `python -m bot.linkedin_auth` helper runs OAuth on `http://localhost:8765/callback` (I'll open the link in the VPS browser over RDP), scopes `w_organization_social r_organization_social`, stores tokens, refreshes them, and reminds me on Telegram 7 days before expiry.
- Publish via the REST Posts API (image upload via Images API). Map each campaign to its LinkedIn organization URN in `campaign.yaml`.

## State, logging, safety

- SQLite tables: campaigns, drafts (with versions), assets, schedules, publications (per platform, with idempotency key and permalink), ideas queue, history, quota usage, tokens.
- Draft state machine: `planned → generating → in_review → approved → publishing → published | failed | skipped`.
- Rotating logs in `logs/` (10 MB × 5). Never log secrets.
- `DRY_RUN=true` mode: everything runs, but nothing is published (Telegram shows what would have been posted).
- Graceful shutdown; resume correctly after a reboot (missed jobs: regenerate if before publish time, otherwise skip and tell me).
- A daily 21:00 (Sydney) summary to Telegram: what was published, what's pending, quota left.

## Tests and acceptance

- Unit tests for: reply parsing, campaign detection, scheduling/next-free-day logic, banned-phrase checks, text auto-fit, idempotency.
- A `python -m bot.selftest` command that renders one sample post + story per campaign per layout into `out/samples/` (no API calls needed) so I can eyeball the designs, including Sinhala and Tamil samples.
- Done means: on a fresh Windows Server, I double-click `install.bat`, fill `secrets.txt`, the wizard shows all ✓, I click the desktop icon, receive "online" on Telegram, run `/generate lushnote`, receive a review card, reply `Caption: shorter`, get a revised card, reply `approve`, and see it on the LushNote Facebook Page and Instagram at the scheduled time (or immediately with `/publish_now` in DRY_RUN=false).

## Documentation

`README.md` written for a non-developer, Windows-only, with numbered steps: download the repo (GitHub "Code → Download ZIP" or `git clone`), fill `secrets.txt` (explain each key and where to get it), double-click `install.bat`, use the desktop icon, Telegram commands, how to add a campaign, how to re-authorise Meta, how to enable LinkedIn, how to update the bot (`update.bat` that pulls the latest code and restarts the bot without touching other processes), troubleshooting.

## Brand prompts

The full brand prompts are already in the repo. Read both completely before writing any code, and use them as the governing rules for copywriting, image briefs, layout choice and compliance checks:

- `campaigns/lushnote/brand_prompt.md`
- `campaigns/allergy-asthma-centre/brand_prompt.md`

They were written for a one-shot image generator, so apply them like this: the "CAMPAIGN INPUT" block is what the bot fills in each day (via Groq); the text, colour, typography, layout and logo rules are enforced by the HTML templates and the compliance check; and the photography/illustration rules go into the Gemini image prompt (always with "no text, no logos, no UI"). Keep both files as editable data, so I can change a brand rule without touching code.

## Repo map (already present)

```
assets/lushnote/logo.png
assets/lushnote/screenshot-*.png|jpg          real app UI, use as-is
assets/lushnote/past-posts/*.webp             style references only
assets/allergy-asthma-centre/logo.png
assets/allergy-asthma-centre/premises.webp    crop to clinic side
assets/allergy-asthma-centre/past-posts/*.png style references only
campaigns/lushnote/brand_prompt.md
campaigns/allergy-asthma-centre/brand_prompt.md
docs/CODEX_BUILD_PROMPT.md                    this spec
```
