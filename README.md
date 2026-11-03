# Neuro Paper Bot

Every weekday this bot finds new neuroscience papers on bioRxiv and in journals you choose. Gemini picks the best few and writes a short teaser (the "hook") for each, and the bot sends you the drafts on Telegram. Each draft has a button per platform: tap **🦋 Bluesky** or **💼 LinkedIn** (or both) and it goes out there. Tap **Skip** and it doesn't. Nothing is posted until you tap.

Based on [oecs-bot](https://github.com/mcfrank/oecs-bot) by Michael Frank (MIT license).

## How it works

One GitHub Actions workflow, [`run.yml`](.github/workflows/run.yml), runs every hour and does whatever is due:

1. **Read your taps:** [`review.py poll`](scripts/review.py) applies the Post, Skip and Undo taps, and the replies you sent in Telegram.
2. **Post:** [`post.py`](scripts/post.py) posts approved drafts to the platforms you picked for them: at most one per platform per run, and at least 2 hours apart on each platform.
3. **Make the batch:** once each weekday, after 13:00 UTC, [`batch.py`](scripts/batch.py):
   - fetches new papers ([`fetch_papers.py`](scripts/fetch_papers.py))
   - has Gemini score each one from 1 to 10 ([`rank_papers.py`](scripts/rank_papers.py))
   - picks the best 2 preprints and 1 journal paper
   - has Gemini write a hook for each pick ([`generate_summaries.py`](scripts/generate_summaries.py))
4. **Send the drafts:** [`review.py send`](scripts/review.py) sends each new draft to Telegram, with its card image and the buttons.
5. **Save:** the workflow commits `data/` back to the repo. The repo is the bot's memory.

A post looks like this. The paper's title, link and card image go in the link card under the text:

    {one or two sentences on what the paper found}

    Smith et al. · bioRxiv preprint
    #neuroscience

Preprints are always labeled as preprints, so readers know they haven't been peer reviewed yet.

## Setup

You need a Telegram bot, a Gemini API key and a GitHub repository, plus an account on each platform you post to: a Bluesky account for the bot, and your own LinkedIn profile (step 6).

### 1. Telegram bot

1. In Telegram, open a chat with **@BotFather**, send `/newbot` and follow the prompts. At the end it gives you a token. That's `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message, for example `/start`.
3. Put the token in `.env` (see step 4) and run `python scripts/review.py chat-id`. It prints your chat ID. That's `TELEGRAM_CHAT_ID`.

### 2. Bluesky account

Create an account for the bot, then go to **Settings → Privacy and security → App passwords** and add one.

- `BLUESKY_USERNAME` is the bot's handle, for example `neuropaperbot.bsky.social`.
- `BLUESKY_PASSWORD` is the app password, not the account password.

Then add `"bluesky"` to `posting.platforms` in `config.json`. Each platform listed there gets a button on every draft. While the list is empty, the bot still makes drafts and sends them to Telegram, but there's nothing to post them to.

### 3. Gemini API key

Create one at [Google AI Studio](https://aistudio.google.com/apikey). That's `GEMINI_API_KEY`.

### 4. Try it on your computer

On Windows (on macOS or Linux, use `.venv/bin/python`):

    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    copy .env.example .env                                # then fill in the values

    .venv\Scripts\python scripts\fetch_papers.py          # what's new (needs no keys)
    .venv\Scripts\python scripts\batch.py --dry-run       # what would be picked, with hooks
    .venv\Scripts\python scripts\batch.py --force         # make a real batch of drafts
    .venv\Scripts\python scripts\review.py send           # send them to your Telegram
    #   ...tap Post under one of them in Telegram...
    .venv\Scripts\python scripts\review.py poll           # pick up the tap
    .venv\Scripts\python scripts\post.py --now            # post it to Bluesky

These steps change the files in `data/`. Commit them along with everything else, so the bot on GitHub carries on from there. Once GitHub is running the bot, don't also run `review.py poll` or `post.py` on your computer: two copies would be handling the same taps.

### 5. Run it on GitHub

1. Create a repository on GitHub. In a public repository, Actions minutes are free. A private one uses about 700–1,500 of the 2,000 free minutes a month.
2. Before you push, open **Settings → Secrets and variables → Actions** in the repository and add these secrets: `GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `BLUESKY_USERNAME` and `BLUESKY_PASSWORD`. `NCBI_API_KEY` is optional. (Without them, every hourly run fails.)
3. Push this folder.
4. Open the **Actions** tab, choose **Run bot**, and click **Run workflow** to check that it works. After that it runs every hour on its own.

### 6. LinkedIn (optional)

The bot posts to LinkedIn as you, from your own profile, and only the drafts you tap **💼 LinkedIn** on.

1. At [LinkedIn Developers](https://www.linkedin.com/developers/apps), create an app. LinkedIn asks you to attach it to a LinkedIn Page you manage.
2. On the app's **Products** tab, add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**.
3. On its **Auth** tab, add the redirect URL `http://localhost:8765/callback`.
4. Copy its Client ID and Client Secret into `.env` as `LINKEDIN_CLIENT_ID` and `LINKEDIN_CLIENT_SECRET`. They stay on your computer.
5. Run `python scripts/linkedin_login.py`. It opens LinkedIn in your browser, saves the token in `.env`, and offers to update the `LINKEDIN_ACCESS_TOKEN` secret on GitHub for you.
6. Add `"linkedin"` to `posting.platforms` in `config.json`, then commit and push.

LinkedIn's token lasts 60 days and can't be renewed automatically, so run `linkedin_login.py` again every two months. If the token runs out first, drafts you've queued for LinkedIn wait, and their Telegram messages tell you to log in again.

## Day to day

- Drafts arrive in Telegram on weekdays, soon after 13:00 UTC.
- **🦋 Bluesky** or **💼 LinkedIn** queues a draft for that platform; tap both to post it to both. Each platform posts at least 2 hours apart, oldest tap first.
- **❌ Skip** drops a draft. **↩️ Undo** takes back a Skip, or whatever hasn't been posted yet.
- **Reply to a draft** with new wording to replace its hook, for example to fix the phrasing or add an `@handle`.
- The bot checks Telegram once an hour, so a message can take up to an hour to update.
- Drafts you don't answer expire after 3 days.
- When a step fails, the run turns red in the Actions tab and GitHub emails you. A post that fails 3 runs in a row is marked as failed in its Telegram message.

## Tuning

| To change | Edit | Check with (saves nothing) |
|---|---|---|
| What gets picked | The "MY INTERESTS" part of `scripts/ranking_prompt.txt` | `python scripts/rank_papers.py --start 2026-10-28 --end 2026-10-30` |
| How hooks read | `scripts/summary_prompt.txt` | `python scripts/generate_summaries.py` (rewrites the latest drafts' hooks) |
| Both, end to end | | `python scripts/batch.py --dry-run --start 2026-10-28 --end 2026-10-30` |
| The card | The palette at the top of `scripts/card.py` | `python scripts/card.py` (saves images in `previews/`) |

## config.json

| Key | What it does |
|---|---|
| `name` | The bot's name, shown on the card |
| `sources.biorxiv.category` | The bioRxiv subject category |
| `sources.pubmed.journals` | Journals to follow. Each key is PubMed's abbreviation for the journal (the "NLM Title Abbreviation" in the [NLM Catalog](https://www.ncbi.nlm.nih.gov/nlmcatalog/journals)), and its value is the name shown in posts. |
| `sources.pubmed.exclude_types` | PubMed publication types to ignore, such as errata and editorials |
| `sources.pubmed.email` | Your email address. NCBI asks for one in API requests, but it's optional. |
| `selection.lookback_days` | How many days back each batch looks. Papers already seen are skipped, so overlapping windows don't matter. |
| `selection.daily_picks` | The most drafts per batch from each source |
| `selection.min_score` | Papers scoring lower than this are never picked |
| `schedule.batch_hour_utc` | The earliest hour (UTC) the weekday batch runs |
| `schedule.weekdays_only` | Set to `false` to make a batch every day |
| `schedule.draft_expire_days` | Drafts nobody answers expire after this many days |
| `posting.platforms` | The platforms drafts can go to: any of `"bluesky"`, `"linkedin"` and `"x"`. Each gets a button on every draft. While it's empty (`[]`), drafts only have Skip. |
| `posting.min_hours_between_posts` | Minimum gap between posts |
| `posting.hashtag` | The hashtag at the end of each post, or `null` for none |
| `posting.hook_max_chars` | The longest hook Gemini may write |
| `gemini.model` | The Gemini model used for scoring and hooks |
| `gemini.fallback_model` | The model to use when the main one is overloaded |

## Files in data/

| File | What it holds |
|---|---|
| `drafts.json` | Every draft and its status: `ready`, then `sent`, `approved` and `posted`; or `skipped`, `expired` or `failed`. Finished drafts are removed after 14 days. |
| `posted.json` | A record of everything posted, one entry per platform |
| `seen.json` | Papers already scored, so none is considered twice. Entries are kept for 60 days. |
| `state.json` | The date of the last batch, and how far the bot has read in Telegram |

## Notes

- **Images:** every post uses the generated card. Papers' own figures aren't used, because many preprints don't allow reuse and journal figures are copyrighted.
- **X:** add `"x"` to `posting.platforms`, and add the `X_API_KEY`, `X_API_KEY_SECRET`, `X_ACCESS_TOKEN` and `X_ACCESS_TOKEN_SECRET` secrets. X posts are text only, with the link in a reply. This hasn't been tried with real X credentials.
- **No double posts:** before posting, `post.py` checks the bot's last 20 Bluesky posts for the same link. If an earlier run posted a draft but couldn't save that it had, the draft is recorded as posted instead of being posted again. LinkedIn doesn't let apps like this read posts back, so there's no such check there; each post is saved right after it goes out to keep the risk small.
- **LinkedIn posts** have the same text as Bluesky's, with a link card under it (title, abstract excerpt and card image). LinkedIn requires some characters, such as parentheses, to be escaped; `linkedin_api.py` does that, so a post isn't cut short.
