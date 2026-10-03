# Implementation and verification notes

The governing brief is `CODEX_BUILD_PROMPT.md`, together with both original brand prompts and the owner's subsequent manual-image and idea/reference instructions. The manual Telegram provider replaces all image generation APIs. Gemini supplies text fallback and optional reference descriptions only.

The application uses one Python process, a private environment, SQLite WAL, long polling and a serialized orchestration loop. Posts and stories use independent daily slots/approvals. New daily drafts and user ideas follow `generating → awaiting_content → awaiting_image → in_review → approved`. Daily content review starts two hours before the slot; the manual ChatGPT handoff follows content approval. Existing visual-only drafts remain supported through additive SQLite migrations.

## Boundaries

- `config.py`: validated campaigns from folders and secret-file loading.
- `db.py`: durable versions, per-slot state, schedules, messages, image requests, uploads, receipts, history and tokens.
- `generation.py`, `compliance.py`: Groq JSON copy/prompt generation, Gemini text fallback, exact brand policies, deterministic checks and fail-closed clinic LLM review.
- `images.py`: `ImageProvider` interface and `ManualTelegramProvider`. No image-generation API calls.
- `content.py`, `bundles.py`: idea allocation, style references, exact owner wording, explained compliance rewrites, content approval, full brand-prompt exports, ZIP/individual files, finished artwork ingestion and transactional schedule swaps.
- `render.py`: deterministic HTML/CSS layouts, local fonts, shaped-text auto-fit, permitted exact assets and clinic crop. Only one browser is active during rendering; every context/browser closes in `finally`.
- `telegram.py`, `app.py`: owner-only routing, stale version checks, manual photo/document handling, persistent selections, reminders and schedules.
- `pipeline.py`, `meta.py`, `linkedin.py`: approval gates, dry-run, per-platform receipts and official API publishing.
- `runtime.py`, `launcher.py`, batch/PowerShell scripts: single-instance lock, BelowNormal priority, redacted rotating logs, local status controls, private installation and boot task.

## Reliability decisions

SQLite transactions reserve draft slots and idempotency keys. Every publication mutation saves its stage before calling the service. Remote receipts are saved before optional metadata requests. Completed platforms are never replayed. Unknown final writes stay blocked pending owner reconciliation; no claim of provider-supported exactly-once delivery is made.

Telegram updates checkpoint before processing to prevent a replay of a side effect after a reboot. Thus an interrupted owner action may require the owner to resend it. Draft/image request state is recovered from SQLite; missing review/request messages are resent. Owner selections and downloaded originals remain durable.

Albums are durably buffered by `media_group_id`, sorted by message ID and deduplicated, then flushed after a two-second quiet period. Polling shortens while albums wait. Albums interrupted during processing are flagged for resubmission rather than replayed. File checkpoints let a restart finish an interrupted handoff upload. Original references stay untouched; JPEG copies with stable names are exported in each selected format's bundle.

Ordinary non-reply text starts an idea. Captioned standalone images/albums become style references; explicit `Use this for ...` source photos and uncaptioned pending-image uploads keep their earlier handling. Campaign/format selections survive restarts. `Use today instead` validates every affected draft before swapping day, UTC schedule, history and idea queue in one transaction. Publishing/published/skipped drafts and uncertain live writes block the move.

Finished ChatGPT artwork already contains its text and branding, so it is resized without HTML overlays; wrong aspect ratios are rejected. Copy is checked automatically; the owner verifies the returned pixels, logo and medical footer in final review. No OCR or image medical-validation claim is made. Skip image and explicit source photos use deterministic HTML templates.

The bot can be stopped while a provider action runs; it completes that action before exiting. If forcibly interrupted externally, publication writes become `unknown`, and unfinished generation resumes only when before its due time. Previously approved same-day drafts remain eligible; expired unapproved drafts skip at local midnight.

Six feed and three story layout families are bundled per campaign, with composition variants to avoid repeating a layout signature within thirty days. Consecutive base layouts differ. Clinic scripts rebalance wide text boxes and line height rather than applying English metrics.

## Verification scope

Offline tests cover parsing, campaign detection, timezone/DST/collisions, next-free-day allocation, multilingual banned wording, category separation/exact footers, fitting bounds, rotation, draft versions, owner checks, manual prompts, ambiguous-write blocking and dry-run publishing. The selftest renders 63 sample PNGs and checks DOM overflow in Chromium, including English/Sinhala/Tamil and educational/institutional variants.

Verified on Windows with Python 3.12.10 and pinned dependencies: 63 tests passed, including idea routing, albums, selections, content-card payloads, exact prompt/ZIP contents, native text, unsafe reference-direction rewrites, vision failure, category separation, approval gates, aspect ratios, original preservation, restart recovery and atomic scheduling swaps. The earlier 63-layout render verification and PowerShell syntax checks remain applicable. Original brand prompts and artwork remain unchanged. Temporary development runtimes are ignored by Git and do not occupy the installer's `.venv` path.

Live acceptance needs the owner's actual credentials, Meta app roles/permissions, connected Business accounts and Windows boot-task credentials. Offline tests cannot establish provider account eligibility or successful live publishing. No credentials are bundled, no live social post was made during development, and installation on the target VPS is not asserted until the wizard and acceptance scenario pass there.

Official contract references used for compatibility checks:

- [Telegram Bot API](https://core.telegram.org/bots/api#message): album `media_group_id`, private-owner updates and `sendDocument` uploads.
- [Gemini image understanding](https://ai.google.dev/gemini-api/docs/image-understanding): inline reference input with text-only responses, independent of image generation.

- [Meta IG user field definitions](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/iguser.py): Facebook Login IG user fields do not include `account_type`; the wizard checks the connected ID and reminds the owner to use Business accounts for stories.
- [Meta Page story edge](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/page.py) and [story fields](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/stories.py): published story URLs are fetched from `stories`, rather than fabricated from post IDs.
- [LinkedIn token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection): live client credential/token/scope checks without requesting extra organisation-admin scopes.
- [LinkedIn REST Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-06): organisation posts, version headers and response IDs.
