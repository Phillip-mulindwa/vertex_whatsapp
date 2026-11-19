# Vertex WhatsApp — project brief for Claude Code

## What this app is
`vertex_whatsapp` is a Frappe custom app for Vertex International Recruitment Ltd (UK-registered recruitment company).
It connects the company WhatsApp number to Frappe and adds an AI assistant that replies to enquiries in a warm, human way.

Owner/builder: Phillip (IT). He is learning as we go: explain what you are about to do in plain English before doing it,
keep changes small, and report clearly what changed.

## Environment
- Dev machine: VirtualBox Ubuntu VM, user `frappe`, bench at `~/frappe-bench`, Frappe version 15.
- Dev site: `vertex.localhost`. Production is Frappe Cloud and is NEVER touched from this machine.
- During development, WhatsApp traffic uses Meta's free TEST number only. The real Vertex number is connected later
  through a WhatsApp Business Solution Provider (BSP) in coexistence mode. Keep all Meta/BSP calls behind one module
  so the provider can change without touching the rest of the code.

## Business rules (the AI must follow these; the code must enforce the hard ones)
- Two kinds of people write in:
  - **Agencies** (recruitment agencies / manpower suppliers): collect company name, country, contact name, email.
    Save them to the **Partner Lead** list (one row per agency, no duplicates by email or WhatsApp number).
    Phillip exports this list to Excel for the operations team. No automatic emails.
    Partner form: https://www.vertexintern.com/#partner-form
  - **Candidates** (job seekers): send them to apply at https://www.vertexintern.com/apply and say they will be
    connected with a recruitment agency in their country.
- Fees, salary, visas, timelines or anything beyond the usual answers: redirect to the apply link and say the team will be in touch.
- Candidates from **Ethiopia, the Philippines, Uganda, South Africa**: polite decline. Agencies from these countries are
  still saved, with a "flagged country" marker.
- The AI ignores the ad's prefilled text ("What benefits do I get from partnering with Vertex?") as proof of being an agency.
- **Internal contacts are never answered** (IT team, Doctor, colleagues). They are blocked in code BEFORE the AI is called.
  WhatsApp groups never reach the API at all.
- **Hand-over (AI sends nothing, chat marked "Needs Phillip")**: possible minor, bank/payment details, threats/legal/police/press,
  scam reports, strong distress.
- No calls on this line. Partnership discussions happen by email.
- The AI must never invent facts (history, offices, fees, salaries, timelines, vacancies), never promise jobs or visas,
  never claim to have done something it hasn't, and must answer honestly if asked whether it is a bot.
- The full AI instructions live in `vertex_whatsapp/ai/instructions.md` (Phillip approves every change to that file).

## AI provider
- Replies are written by **OpenAI (ChatGPT) via the API**. Keep an `ai_provider` setting ("openai" now, "anthropic" possible later)
  and put ALL model calls behind one module: `vertex_whatsapp/services/ai_client.py`. Nothing else calls a model directly.
- The model returns JSON only: category, language, flags, action (reply | silent | hand_over), reply, agency, reason.
  Validate it strictly; anything invalid = no send, log the error.

## Safety checks enforced in code (never rely on the AI alone)
- Internal-contact blocklist checked first.
- A reply must contain the correct link for its situation; no money amounts, phone numbers or other email addresses;
  no sentence already sent in this chat; max 3 automatic replies per contact per day.
- Kill switch + per-topic mode (Off / Draft / Auto) in **AI Settings**. Default for everything: Draft.
- Respect Meta's 24-hour customer service window.
- Every AI decision is stored in **AI Decision Log** (input summary, output, action, reason, who approved).

## Secrets
- Secrets never go in code, git, Desk-visible fields, logs or test fixtures. They live in `site_config.json` only,
  read with `frappe.conf`: `wa_token`, `wa_app_secret`, `wa_verify_token`, `openai_api_key` (and later the BSP key).
- Mode switch `frappe.conf.wa_mode`: `live` sends for real; `fake` (default when unset, and always in tests)
  records the outgoing request and sends nothing. Same for AI: `frappe.conf.ai_mode` = `live` or `fake`.
  No test may ever make a real network call.
- Never log full message bodies or phone numbers at INFO level.

## Code conventions
- DocTypes under `vertex_whatsapp/vertex_whatsapp/doctype/`; logic in controllers, `vertex_whatsapp/api/`, `vertex_whatsapp/services/`.
- All Meta/BSP calls go through `vertex_whatsapp/services/wa_cloud.py` only.
- Webhook handlers are idempotent (Meta retries; message `wamid` is unique, duplicates are skipped).
- Slow work goes to background jobs (`frappe.enqueue`, `enqueue_after_commit=True`); the webhook only verifies, stores and returns.
- Schema changes via DocType JSON; data fixes in `patches/` listed in `patches.txt`.
- Every feature has tests (Frappe test runner) using Meta's documented webhook payload shapes.

## How we work
- One feature per git branch (e.g. `webhook`, `ai-drafts`). Never commit straight to the main branch.
- Before finishing a task: run `bench --site vertex.localhost run-tests --app vertex_whatsapp`, run
  `bench --site vertex.localhost migrate` cleanly, then report what changed, which DocTypes were added or altered,
  and how to undo it.
- Commit messages: plain English sentences, no prefixes like "feat:" or "chore:".
- Commits and PRs contain NO AI attribution: no "Co-Authored-By" lines, no "Generated with" footers.
- Ask before: deleting files or data, changing site_config.json, installing new Python/JS packages, or anything touching production.
