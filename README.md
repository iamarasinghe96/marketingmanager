# Marketing Manager

A Windows bot for LushNote and Allergy & Asthma Centre – Colombo. Groq writes campaign copy, Telegram handles content and final image approval, and the bot packages brand prompts and assets for your manual ChatGPT handoff. It publishes approved posts and stories to Facebook and Instagram. Gemini supplies text fallback and optional reference descriptions, never generated images. LinkedIn company posts are included but disabled until your API access is approved.

Images come from you. There is **no image API**, no automated ChatGPT interaction, no browser login automation, no dashboard, no public website and no inbound internet port. Chromium runs only while rendering your artwork.

## 1. Download on your Windows Server

Use [GitHub → Code → Download ZIP](https://github.com/iamarasinghe96/marketingmanager), extract it to a permanent folder such as `C:\MarketingManager`, and open that folder. Avoid a temporary Downloads folder or a network share. If you already use Git, clone the repository instead:

```powershell
git clone https://github.com/iamarasinghe96/marketingmanager.git C:\MarketingManager
```

Your trading bot can keep running. This application uses its own `.venv`, its own Chromium download and its own SQLite file. It never kills another process, changes PATH, modifies a firewall, runs Windows Update or reboots your server. Its bot and renderer run at BelowNormal priority. Large uploads are reduced to a separate composition copy before Chromium starts; the full-resolution original stays untouched. Only one rendering job runs at a time to keep memory near the approximately 400 MB target.

## 2. Fill `secrets.txt`

Copy `secrets.example.txt`, rename the copy `secrets.txt`, and open it in Notepad. Keep one `KEY=value` per line, without surrounding backticks. Never send this file, your `data` folder or logs to a public repository.

| Key | Where to get it |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Message [@BotFather](https://t.me/BotFather), use `/newbot`, and copy its HTTP API token. Use a dedicated bot. |
| `TELEGRAM_OWNER_CHAT_ID` | Open your new bot in Telegram and send `/start`. The setup wizard lists pending private chat IDs when this field is empty; enter **your own positive personal ID**. Only that private account may control the bot. |
| `GROQ_API_KEY` | Create a free key at [Groq Console](https://console.groq.com/keys). Keep billing disabled. |
| `GEMINI_API_KEY` | Create a key in an **unbilled, free** project at [Google AI Studio](https://aistudio.google.com/apikey). It is used for text fallback/compliance only. |
| `META_APP_ID` | Your Business app's Settings → Basic at [Meta for Developers](https://developers.facebook.com/apps/). |
| `META_APP_SECRET` | Same Meta app, Settings → Basic → App secret. |
| `META_USER_TOKEN` | A user token for that app from [Graph API Explorer](https://developers.facebook.com/tools/explorer/), following the steps below. |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | Leave blank while LinkedIn is disabled. Later use the Auth tab of your approved LinkedIn app. |
| `DRY_RUN` | Leave `true` initially. Set `false` only when you want approved content published. Restart after changing it. |

### Meta account preparation

1. Be an administrator of both Facebook Pages. Connect each Page to its corresponding **Instagram Business** account. Instagram story publishing needs a Business account.
2. Create a Business Meta app and configure Facebook Login and Instagram access. Your own managed Pages can be tested in development mode if your account has the appropriate app role and permissions. Account restrictions and Meta app review requirements still depend on your app configuration.
3. In Graph API Explorer, select **your app**, choose a **User token**, and grant the actual Pages. Request `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, `instagram_content_publish`, and `business_management` as applicable to your app.
4. Paste the token into `META_USER_TOKEN`. The wizard validates the app identity and scopes, exchanges it for a long-lived user token, fetches Page tokens from `me/accounts`, and checks the connected Instagram IDs. Tokens are stored in your private SQLite database.
5. Page tokens derived this way can have no scheduled expiry, but can still be revoked. The wizard reports the actual expiry returned by Meta, and the bot checks token health daily.

The configured accounts are:

| Campaign | Facebook Page | Instagram |
| --- | --- | --- |
| LushNote | `1377201872144623` | `17841415037476125` |
| Allergy & Asthma Centre | `1054872354373231` | `17841451553167746` |

## 3. Double-click `install.bat`

The installer checks for **Python 3.12**, offers a user-confirmed winget installation if missing, creates the private environment, installs dependencies and the private headless Chromium, downloads open-source fonts, and runs the setup wizard. If winget installs Python, reopen the installer to pick up the launcher.

The wizard makes live tests for Telegram, Groq, Gemini text and Meta, and prints a ✓ or ✗ with repair instructions. It sends you a Telegram connection test but never publishes anything. Fix failed items in `secrets.txt` and double-click `setup.bat` to rerun it. Stop Marketing Manager before rerunning setup.

When setup succeeds, installation creates **Marketing Manager** and **Stop Marketing Manager** desktop shortcuts and registers a boot task. Windows will ask for the Windows account/password so Task Scheduler can run it while you are logged out. The bot does not save that password. Run the installer from your Administrator RDP session; no other scheduled tasks are changed.

## 4. Use the desktop icon

Double-click **Marketing Manager**. It starts a hidden background process if needed, or reports that the existing instance is running. A small status window shows the accounts, token health, next jobs and last heartbeat, with **View log** and **Refresh** buttons. Closing that window leaves the bot running.

Use `/help` in Telegram for the complete command guide. **Stop Marketing Manager** requests a graceful shutdown; it lets the current action finish and saves the state. It does not terminate a process by name or PID.

## 5. The daily flow in Telegram

The bot works in one short session per day. Nothing is planned ahead and nothing carries over.

1. **08:00 Colombo time**: the bot says *Good morning 👋 Reply "hi" to start today's posts.* If you don't reply, **nothing happens that day**. You can also send `hi` any time during the day.
2. **You reply `hi`**: for each active campaign you get **one `prompt.txt`**. It is the full brand prompt, word for word, with today's CAMPAIGN INPUT filled in by Groq. The message says what to attach in ChatGPT; the bot also sends that file, e.g. the logo for institutional clinic posts or a screenshot for LushNote. Educational clinic posts need no attachments, because they must not carry the clinic logo.
3. **Make the image in ChatGPT** with that prompt, then **send the finished image** to the bot as a photo or a file. Sending as a file keeps full quality.
4. The bot replies with the **image and its caption** together, plus one line: *Reply "approve" to post, or tell me what to change.*
5. **To change the caption, just write what you want**, e.g. `make it shorter`, `remove the hashtags`, `add a line about dust mites`. The AI applies it, checks the brand and medical rules, and replies with only the new caption. Send a new image at any time to replace the image.
6. **Reply `approve`** (or `ok` / 👍). It is posted **immediately** to the campaign's Facebook Page and Instagram, as a **feed post and a story**. The story uses the same image, padded to 9:16 on the brand background without cropping. You get one confirmation with the links.

If you said `hi` but something is unfinished, you get **one** reminder 3 hours after your last message. At midnight Colombo time anything unfinished is dropped. Start a session with an idea by writing `hi, idea: <your idea>`.

Captions for the Allergy & Asthma Centre never include the doctor's name or credentials (the approved attribution belongs only in the educational artwork), and include the Sinhala disclaimer at most once.

**LushNote is paused until 1 November 2026** (`active_from` in `campaigns/lushnote/campaign.yaml`). Until then only the Allergy & Asthma Centre prompt is sent.

Settings in `config.yaml`:

```yaml
workflow: daily          # "scheduled" restores the older planned-draft workflow
daily:
  timezone: Asia/Colombo
  greeting_time: '08:00'
  reminder_after_hours: 3
```

In `DRY_RUN=true` everything works except the final step: approving says what would have been posted and nothing is published.

### Telegram commands

| Command | Action |
| --- | --- |
| `/status` | Accounts, tokens and today's progress. |
| `/campaigns` | Campaign names and slugs. |
| `/pause` / `/resume` | All campaigns. Add a slug to affect one campaign. |
| `/help` | The daily flow in short. |

## 6. Check the designs offline

From PowerShell in the project folder:

```powershell
.\.venv\Scripts\python.exe -m bot.selftest
```

Open `out\samples\index.html`. It renders every configured feed/story layout, plus clinic educational/institutional samples in English, Sinhala and Tamil: **63 images for the bundled campaigns**, without API calls. Fonts are shaped by Chromium. The clinic premises crop excludes the neighbouring pharmacy/laboratory signs. Sample illustrations are clearly offline layout examples.

The renderer auto-fits shaped text within readable bounds and refuses overflow. It never shrinks Sinhala/Tamil below the configured minimum to force excessive copy into a box. If copy is too long, shorten the on-image wording through Telegram. Hashtags appear only in captions.

## 7. Change schedules, brands or add a campaign

Stop the bot before editing configuration, then restart it. Each campaign's `campaign.yaml` contains the timezone, language rotation, sizes, times, accounts, palette, fonts, asset paths, reference paths, content mix, hashtags, banned phrases and footer rules. `brand_prompt.md` is read in full whenever copy or an image prompt is generated. You can edit it without changing Python.

Default publish times are LushNote 12:30 / 18:30 Sydney and the clinic 10:00 / 17:00 Colombo. Requests are two hours earlier. A deterministic UTC scheduler separates publish slots by at least 30 minutes and handles Sydney daylight saving time. If edited times collide, later jobs move forward; impossible schedules crossing midnight are rejected.

To add another campaign, create `campaigns\your-slug\`, copy one existing `campaign.yaml` and create `brand_prompt.md`. Give it a unique name/aliases, your official accounts/assets, rules and local schedule. Existing `lushnote` or `clinic` design systems supply the layouts; at least six feed and three story layouts are required. No code change is needed to add another campaign using those design systems. Asset paths must remain inside the project. Run `setup.bat` again to fetch the new Page token.

Clinic posts are strictly **EDUCATIONAL** or **INSTITUTIONAL**. Educational artwork/captions have no clinic logo, contact details, premises or booking CTA; only the exact approved attribution/disclaimer footer is added. Institutional content has the real logo/contact details and no doctor name, qualifications, title or portrait direction. Multilingual banned phrases and an LLM compliance check must pass. Failed drafts regenerate up to twice, then are shown flagged and cannot be approved until revised.

The clinic's `approved_facts` list is empty initially. That authorises neutral awareness topics only. Add clinically checked sentences there before asking for medical claims. RSS headlines are treated as untrusted news context and do not authorise medical claims. The bot does not invent research, services, prices, cures or specialties.

`config.yaml` also sets local daily text budgets (100 Groq requests and 50 Gemini fallback requests). `/status` and the 21:00 Sydney summary show usage and budget remaining. These are conservative local caps, not a promise about provider quota; provider limits can be lower, and rejected calls still consume the local budget. Keep both accounts unbilled to remain within free services.

## 8. Re-authorise Meta

Stop the bot, obtain a fresh `META_USER_TOKEN` in Graph API Explorer with the correct app, permissions and Pages, replace that line in `secrets.txt`, and double-click `setup.bat`. Start the bot again. Daily health checks alert you if Meta revokes access.

Images for Instagram are uploaded to Facebook as **unpublished** photos, and their CDN URLs are passed to Instagram's media container API. The bot waits for readiness before publishing. Facebook stories use the Page photo stories endpoint. The local ceiling is 25 Instagram API posts per rolling 24 hours; it also checks Meta's account quota.

If the network fails after a publishing request, Meta cannot guarantee exactly-once delivery. The bot stores a receipt/stage **before every write** and stops automatic retries when success is uncertain. It will not blindly create another post. Check your Page/account first, then use one of these owner-only commands:

```text
/resolve <draft ID> facebook post <existing remote ID>
/resolve <draft ID> instagram story <existing remote ID>
/resolve <draft ID> facebook post not-posted
/retry <draft ID>
```

Use `not-posted` only after confirming that the first request did not publish. `/retry` reuses successful platforms and saved containers. Read-only/transient explicitly rejected calls retry with exponential backoff, up to three attempts. Ambiguous writes stay blocked until reconciled. A receipt may have no public permalink, particularly for stories.

## 9. Enable LinkedIn later

Wait for LinkedIn **Community Management API** approval. Fill the two LinkedIn secrets, register `http://localhost:8765/callback` as a redirect URI, and put each company organization URN in its campaign configuration. Stop the bot, then run:

```powershell
.\.venv\Scripts\python.exe -m bot.linkedin_auth
```

Open the displayed authorisation URL in your VPS browser over RDP and approve `w_organization_social r_organization_social`. The helper listens on loopback only for up to ten minutes, validates the OAuth state and saves tokens in SQLite. It does not automate the browser. Set `linkedin.enabled: true` in `config.yaml`, rerun `setup.bat` and restart. Feed posts use the Images API and REST Posts API with the configurable LinkedIn version header. Stories are not sent to LinkedIn. Refresh tokens are used if your approved app receives them; otherwise Telegram reminds you seven days before expiry to re-authorise.

## 10. Update and troubleshoot

**Git installation:** double-click `update.bat`. It asks this bot to stop gracefully, waits without killing anything, uses `git pull --ff-only`, updates private dependencies/fonts/Chromium and restarts the bot if it was running. If you edited tracked brand/config files, save/commit those edits first; the update will refuse to overwrite them.

**ZIP installation:** stop the bot, download/extract the new ZIP somewhere else, and copy the updated `bot`, `scripts`, requirements and batch files into your installation. Preserve `secrets.txt`, `data`, your edited `config.yaml`, campaign rules and assets. Rerun `install.bat`. This preserves drafts and tokens.

| Symptom | Fix |
| --- | --- |
| No online message | Open the desktop status window → View log; rerun setup. Send `/start` to the Telegram bot first. |
| “Already running” | One instance is intentional. Use the Stop shortcut and allow the current action to finish. |
| Blank/missing glyphs | Rerun `install.bat` to download Noto fonts; run the offline samples. |
| Text too long | Reply `Caption: shorten on-image text too`; regenerate the copy. |
| No image request | Check `/pause`, timezone/times and `/queue`. Automatic requests begin two hours before each slot; `/generate` is immediate. |
| A new image replies to an old card | Send it again as a reply to the latest image request/review card. Approval is tied to the newest version. |
| Image file rejected | Use PNG/JPEG/WebP under 20 MB and 40 megapixels. Send as a document for original quality. |
| Text key/quota error | Check free quota and model availability in provider consoles. Names are editable in `config.yaml`. |
| Medical compliance blocked | Revise the flagged wording or supply checked facts. There is no approval bypass. |
| Page/Instagram access refused | Verify Page admin role, Business account connection, app roles and permissions; re-authorise Meta. |
| Uncertain publication | Check the account and use `/resolve` before `/retry`. |
| Missed slot after a reboot | New missed jobs are skipped and reported. Existing approved drafts can publish later on the same local day. |
| Boot task did not run | Inspect Task Scheduler → Marketing Manager, account/password and installation path. Rerun installer to update the saved Windows credentials. |

State is in `data\marketing.sqlite3`. Logs rotate in `logs\marketing.log` at 10 MB × five backups with secret redaction. Back up the project when the bot is stopped, keeping those files private. Stop requests and a process-held file lock prevent double starts and survive stale lock files after reboot.

For offline developer checks (no credentials or external publishing):

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m bot.selftest
```

`bot.images.ImageProvider` is the extension interface. `manual_telegram` is the only bundled/default provider. A future provider can return local image paths through the same request/submit/skip contract without altering Telegram review, rendering or publishing. Current code contains no image-generation API calls.
