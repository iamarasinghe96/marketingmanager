# Implementation and verification notes

The governing brief is `CODEX_BUILD_PROMPT.md`, together with both original, unchanged brand prompts and the owner's subsequent manual-image instructions. The manual Telegram provider replaces all Gemini image generation. Gemini remains a text fallback only.

The application uses one Python process, a private environment, SQLite WAL, long polling and a serialized orchestration loop. Posts and stories use independent daily slots/approvals because the owner requested image handoffs two hours before each respective publish time. The original draft lifecycle adds `awaiting_image` between generation and review.

## Boundaries

- `config.py`: validated campaigns from folders and secret-file loading.
- `db.py`: durable versions, per-slot state, schedules, messages, image requests, uploads, receipts, history and tokens.
- `generation.py`, `compliance.py`: Groq JSON copy/prompt generation, Gemini text fallback, exact brand policies, deterministic checks and fail-closed clinic LLM review.
- `images.py`: `ImageProvider` interface and `ManualTelegramProvider`. No image-generation API calls.
- `render.py`: deterministic HTML/CSS layouts, local fonts, shaped-text auto-fit, permitted exact assets and clinic crop. Only one browser is active during rendering; every context/browser closes in `finally`.
- `telegram.py`, `app.py`: owner-only routing, stale version checks, manual photo/document handling, persistent selections, reminders and schedules.
- `pipeline.py`, `meta.py`, `linkedin.py`: approval gates, dry-run, per-platform receipts and official API publishing.
- `runtime.py`, `launcher.py`, batch/PowerShell scripts: single-instance lock, BelowNormal priority, redacted rotating logs, local status controls, private installation and boot task.

## Reliability decisions

SQLite transactions reserve draft slots and idempotency keys. Every publication mutation saves its stage before calling the service. Remote receipts are saved before optional metadata requests. Completed platforms are never replayed. Unknown final writes stay blocked pending owner reconciliation; no claim of provider-supported exactly-once delivery is made.

Telegram updates checkpoint before processing to prevent a replay of a side effect after a reboot. Thus an interrupted owner action may require the owner to resend it. Draft/image request state is recovered from SQLite; missing review/request messages are resent. Owner selections and downloaded originals remain durable.

The bot can be stopped while a provider action runs; it completes that action before exiting. If forcibly interrupted externally, publication writes become `unknown`, and unfinished generation resumes only when before its due time. Previously approved same-day drafts remain eligible; expired unapproved drafts skip at local midnight.

Six feed and three story layout families are bundled per campaign, with composition variants to avoid repeating a layout signature within thirty days. Consecutive base layouts differ. Clinic scripts rebalance wide text boxes and line height rather than applying English metrics.

## Verification scope

Offline tests cover parsing, campaign detection, timezone/DST/collisions, next-free-day allocation, multilingual banned wording, category separation/exact footers, fitting bounds, rotation, draft versions, owner checks, manual prompts, ambiguous-write blocking and dry-run publishing. The selftest renders 63 sample PNGs and checks DOM overflow in Chromium, including English/Sinhala/Tamil and educational/institutional variants.

Verified on Windows with Python 3.12.10 and the pinned dependencies: 40 tests passed, all 63 layouts rendered, PNG dimensions matched the campaign settings, and both PowerShell scripts passed syntax parsing. The original supplied brand prompts and artwork remain unchanged. The temporary development runtimes are ignored by Git and do not occupy the installer's `.venv` path.

Live acceptance needs the owner's actual credentials, Meta app roles/permissions, connected Business accounts and Windows boot-task credentials. Offline tests cannot establish provider account eligibility or successful live publishing. No credentials are bundled, no live social post was made during development, and installation on the target VPS is not asserted until the wizard and acceptance scenario pass there.

Official contract references used for compatibility checks:

- [Meta IG user field definitions](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/iguser.py): Facebook Login IG user fields do not include `account_type`; the wizard checks the connected ID and reminds the owner to use Business accounts for stories.
- [Meta Page story edge](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py) and [story fields](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/stories.py): published story URLs are fetched from `stories`, rather than fabricated from post IDs.
- [LinkedIn token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection): live client credential/token/scope checks without requesting extra organisation-admin scopes.
- [LinkedIn REST Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-06): organisation posts, version headers and response IDs.
