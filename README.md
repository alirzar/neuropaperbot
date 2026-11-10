# Neuro Paper Bot

Finds new neuroscience papers on bioRxiv, medRxiv and in journals you choose, and uses Gemini to pick and explain them. There are two versions:

- **[The public bot](#the-public-bot):** a bot other people can join, on Telegram and on Discord. Each person gets a daily digest matched to their interests, and can search recent papers, save them, follow authors and ask for plain-language summaries. It runs on a computer that stays on, such as a lab server.
- **[The GitHub version](#the-github-version):** runs hourly on GitHub Actions and sends only you a few drafts each weekday, to post to a channel, LinkedIn or Bluesky. The public bot replaces it.

Based on [oecs-bot](https://github.com/mcfrank/oecs-bot) by Michael Frank (MIT license).

## The public bot

[`scripts/bot.py`](scripts/bot.py) keeps running and talks to everyone who uses it, on Telegram or, once you [add it to Discord](#add-it-to-discord), on Discord:

- **Joining:** someone sends `/start`, and you get **✅ Approve / ❌ Deny**. Once approved, they pick from 12 areas, describe their interests in their own words, choose their sources, and pick a time zone, an hour, the days of the week, and how many papers they want each time (1 to 10, three to start with). Then they see a summary of their choices, can change any part, and tap **✅ Start my digest**: nothing is sent before that. `/settings` shows the same summary anytime.
- **A digest** at their hour, on the days they pick (every day to start with): the papers that suit them best and that they haven't seen yet, from the past week, or from as far back as they choose with `/range` (2 weeks, a month or 2 months). Each comes as a card with **👍 / 👎** (which tune later picks), **💾 Save**, **📃 Abstract** (the whole abstract as text: the card only has room for its start), **📄 Summary**, **📋 Copy** and **🔗 Similar** (the 5 closest recent papers). A 💡 line on each card says why it was picked: the interest it matched, and whether it came from their sources. Your own digest also has **💼 LinkedIn**. A **➡️ 3 more** button under the digest brings the next papers on their list, up to 5 times a day.
- **Sources:** `/sources` lists where papers come from: bioRxiv and medRxiv sections, neuroscience journals, and general journals such as Nature, Science and eLife, from which only the brain papers come. Nothing is ticked to start with: each person ticks the ones they want, or none. **➕ Add a journal** takes any journal PubMed indexes, by name, abbreviation or ISSN. The bot checks that it published in the past year, and then offers it to everyone.
- **On request:** `/find <topic>` searches the last 60 days of papers. Pasting a DOI or a bioRxiv or medRxiv link gets a plain-language summary. `/popular` shows this week's papers that at least two people gave 👍 or saved, as counts without names. `/export` sends someone's saved papers as a citation file for Zotero, EndNote or Mendeley (`/export bibtex` for LaTeX), with full details from Crossref.
- **Alerts:** `/follow <name>` sends a message when that author publishes, in any PubMed journal or on bioRxiv. `/watch <topic>` sends one the day a new paper mentions that topic or is among the week's closest matches to it, up to 3 a day.
- **Commands:** `/digest`, `/find`, `/popular`, `/saved`, `/export`, `/follow`, `/following`, `/unfollow`, `/watch`, `/watching`, `/unwatch`, `/topics`, `/keywords`, `/sources`, `/time`, `/days`, `/range`, `/settings`, `/privacy` and `/stop`; and for you, `/users` and `/stats` (people, papers, reactions and Gemini use over the past week).

**How it picks papers.** Every morning at 06:00 UTC it fetches new papers from the base sources (`sources` in `config.json`) and from every source someone has ticked. A source nobody had is fetched right away, the moment someone ticks it. Gemini turns each paper, and each person's topics and keywords, into a "fingerprint" (an embedding), so that similar subjects get similar fingerprints. A person's digest starts with the papers from the sources they ticked, over the past week or the range they chose, closest to their interests first, nudged toward papers like the ones they gave 👍, away from the 👎 ones, and slightly by Gemini's quality score. When those run out, the best matches from every other source follow, and someone who ticks nothing gets the best matches from everything. From general journals, PubMed sends only papers whose title or abstract mentions the brain or nervous system, and Gemini then sets aside any that aren't really neuroscience, such as a "neural network" for battery materials. Gemini works per paper, not per person, so costs grow with the number of papers rather than people. Summaries are the exception, which is why each person gets 10 a day (you have no limit).

**Privacy.** People's data stays in `data/db/bot.db` on the computer running the bot and is never committed to git: their Telegram or Discord name and ID, their interests and sources, what they were sent, and their 👍/👎, saves, follows and watched topics. Journals people add are kept without their names, and `/popular` shows counts, never who. `/privacy` tells people this, and `/stop` deletes everything about them.

### Try it on your computer

1. In Telegram, ask **@BotFather** for a new bot (`/newbot`). Don't reuse the GitHub version's bot: two programs can't read the same bot's messages.
2. In `.env`, set `TELEGRAM_BOT_TOKEN` to the new bot's token. `TELEGRAM_CHAT_ID` stays your own ID, which makes you the admin. `GEMINI_API_KEY` is needed too. `DISCORD_BOT_TOKEN` ([Discord](#add-it-to-discord)) and `LINKEDIN_ACCESS_TOKEN` are optional.
3. Run `python scripts/bot.py`, and send your new bot `/start` to set up your own digest. Its first paper update starts right away and takes a minute or two. Stop the bot with **Ctrl+C**.
4. Optional, with @BotFather: `/setuserpic` (use `assets/logo.png`), `/setdescription`, `/setjoingroups` → **Disable**, and `/setcommands` with the list below, so the commands show up in Telegram's menu.

```
digest - Today's papers, now
find - Search recent papers
saved - Papers you saved
follow - Get a message when an author publishes
following - Authors you follow
unfollow - Stop following an author
watch - Get a message when a new paper is on a topic
watching - Topics you watch
unwatch - Stop watching a topic
popular - This week's most liked and saved papers
export - Your saved papers as a file for Zotero
topics - The areas you follow
keywords - Your interests in your own words
sources - The journals and preprints you get
time - When your digest arrives
days - Which days your digest comes
range - How far back your papers go
settings - Your choices, and changing them
privacy - What the bot keeps
stop - Delete your data
help - What the bot can do
```

### Add it to Discord

The same program also runs the bot on Discord, for people without Telegram. Everyone shares one database and one daily paper update, so Discord adds no Gemini cost beyond each person's own summaries and searches.

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications), click **New Application**, and name it (for example *Neuro Paper Digest*). You can upload `assets/logo.png` as its icon.
2. On the **Bot** page, click **Reset Token**, copy the token, and paste it into `.env` as `DISCORD_BOT_TOKEN=...`. On the same page, turn off **Public Bot**, so only you can add it to servers. It needs none of the "privileged intents".
3. Discord only lets a bot message people who share a server with it, so the bot needs a server. Make one (in Discord: **+** → **Create My Own**), or use one you run.
4. Restart the bot. Its log shows a link starting with `https://discord.com/oauth2/authorize`: open it and add the bot to your server. The bot sets up its slash commands by itself.
5. Share the server's invite link. People join, find the bot in the member list, and send it a message or `/start`. You approve them in Telegram, as for everyone else.

On Discord, everything happens in a direct message with the bot, with the same setup, digest, buttons and commands. Lists with many choices (areas, sources, time zones) are dropdown menus, since Discord allows at most 25 buttons on a message. If someone leaves the server, the bot can no longer message them: their digest is skipped, and the log says so.

### Run it on a server

Any Linux machine that stays on and can reach the internet will do. It doesn't need open ports or a web address, because the bot fetches its messages from Telegram and Discord. If the server is a shared cluster with a job scheduler (such as SLURM), ask first: those often stop long-running programs.

**1. Install it**, here with conda:

```
git clone https://github.com/alirzar/neuropaperbot.git
cd neuropaperbot
conda create -n neurobot python=3.12 -y
conda activate neurobot
pip install -r requirements.txt
which python      # note this path: step 3 needs it
```

**2. Bring over your settings.** First stop the bot on your computer (Ctrl+C in its window): two copies can't run at once, and the database shouldn't change while you copy it. Then, from the project folder on your computer:

```
scp .env YOUR_USERNAME@SERVER:neuropaperbot/.env
ssh YOUR_USERNAME@SERVER "mkdir -p neuropaperbot/data/db"
scp data/db/bot.db YOUR_USERNAME@SERVER:neuropaperbot/data/db/bot.db
```

Copying the database keeps everyone's setup; without it, people set up again. Don't send `.env` by email: it holds your keys. On the server, check that the bot starts with `python scripts/bot.py`, then stop it with Ctrl+C.

**3. Keep it running.** Without admin rights, use [`scripts/run_bot.sh`](scripts/run_bot.sh). It restarts the bot whenever it stops, and cron starts the script again after the server restarts. Run `crontab -e` and add this line, with your Python path from step 1:

```
*/10 * * * * PYTHON=/path/to/envs/neurobot/bin/python flock -n $HOME/.neuropaperbot.lock $HOME/neuropaperbot/scripts/run_bot.sh
```

Within 10 minutes the bot is running. Its log is `logs/bot.log`, which you can follow with `tail -f logs/bot.log`. To stop the bot for good, delete that line with `crontab -e`, then run `pkill -f run_bot.sh; pkill -f scripts/bot.py`.

With admin rights, a systemd service does the same job. Save this as `/etc/systemd/system/neuropaperbot.service`, with your own username, folder and Python path:

```
[Unit]
Description=Neuro Paper Bot
After=network-online.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/neuropaperbot
ExecStart=/path/to/envs/neurobot/bin/python scripts/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Start it with `sudo systemctl enable --now neuropaperbot`, and follow its log with `journalctl -u neuropaperbot -f`.

Back up `data/db/bot.db` now and then: it's the only copy of everyone's settings.

Once the bot runs on the server, turn off the GitHub version: on GitHub, open **Actions → Run bot → ⋯ → Disable workflow**.

### The public bot's settings

In `config.json`:

| Key | What it does |
|---|---|
| `bot.approval` | `true`: you approve each new person. `false`: anyone can join. |
| `bot.update_hour_utc` | When (UTC) the daily paper update runs |
| `bot.papers_per_digest` | Papers per digest, until someone picks their own with `/settings` |
| `bot.recent_days` | How far back each daily update looks, and how far back digests reach unless someone picks a longer `/range` |
| `bot.search_days`, `bot.keep_days` | How far back `/find` looks, and when old papers are forgotten (saved ones are kept). `/range` never offers more than `keep_days`. |
| `bot.summaries_per_day`, `bot.searches_per_day` | Daily limits per person (searches include journal searches); you have none |
| `bot.more_per_day` | How many times a day each person can tap **➡️ more**; you have no limit |
| `bot.follows_per_person`, `bot.watches_per_person` | The most authors one person can follow, and topics they can watch |
| `sources` | The base sources: fetched every day whether or not anyone ticks them, so there are always papers to choose from (the GitHub version uses them too) |
| `more_sources` | The other sources `/sources` offers: `preprints` as `"server:category"` (bioRxiv or medRxiv), `journals` by PubMed abbreviation, and `general_journals`, from which only brain papers come |
| `topics` | The areas offered at setup: each has a label, and the description its fingerprint is made from |
| `gemini.embedding_model` | The Gemini model that makes the fingerprints |

## The GitHub version

Every weekday the GitHub version finds new papers, has Gemini pick the best few and write a short teaser (the "hook") for each, and sends you the drafts on Telegram. Each draft has a button per platform: tap **📢 Channel** (your public Telegram channel), **💼 LinkedIn** or **🦋 Bluesky**, or several, and it goes out there. Tap **Skip** and it doesn't. Nothing is posted until you tap. Under each draft is also a version ready to copy, for posting it anywhere else yourself.

### How it works

One GitHub Actions workflow, [`run.yml`](.github/workflows/run.yml), runs every hour and does whatever is due:

1. **Read your taps:** [`review.py poll`](scripts/review.py) applies the Post, Skip and Undo taps, and the replies you sent in Telegram.
2. **Post:** [`post.py`](scripts/post.py) posts approved drafts to the platforms you picked for them: at most one per platform per run, and at least 2 hours apart on each platform.
3. **Make the batch:** once each weekday, after 13:00 UTC, [`batch.py`](scripts/batch.py):
   - fetches new papers ([`fetch_papers.py`](scripts/fetch_papers.py))
   - has Gemini score each one from 1 to 10 ([`rank_papers.py`](scripts/rank_papers.py))
   - picks the best 2 preprints and 1 journal paper
   - has Gemini write a hook for each pick ([`generate_summaries.py`](scripts/generate_summaries.py))
4. **Send the drafts:** [`review.py send`](scripts/review.py) sends each new draft to Telegram, with its card image and the buttons, followed by the post ready to copy.
5. **Save:** the workflow commits `data/` back to the repo. The repo is the bot's memory.

A post looks like this. The paper's title, link and card image go in the link card under the text:

    {one or two sentences on what the paper found}

    Smith et al. · bioRxiv preprint
    #neuroscience

Preprints are always labeled as preprints, so readers know they haven't been peer reviewed yet.

## Setting up the GitHub version

You need a Telegram bot, a Gemini API key and a GitHub repository, plus an account on each platform you post to: a Bluesky account for the bot, and your own LinkedIn profile (step 6).

### 1. Telegram bot

1. In Telegram, open a chat with **@BotFather**, send `/newbot` and follow the prompts. At the end it gives you a token. That's `TELEGRAM_BOT_TOKEN`.
2. Open a chat with your new bot and send it any message, for example `/start`.
3. Put the token in `.env` (see step 4) and run `python scripts/review.py chat-id`. It prints your chat ID. That's `TELEGRAM_CHAT_ID`.

### 2. Bluesky account

Create an account for the bot, then go to **Settings → Privacy and security → App passwords** and add one.

- `BLUESKY_USERNAME` is the bot's handle, for example `neuropaperbot.bsky.social`.
- `BLUESKY_PASSWORD` is the app password, not the account password.

Then add `"bluesky"` to `posting.platforms` in `config.json`. Each platform listed there gets a button on every draft, including drafts already waiting in Telegram (the next run redraws them). While the list is empty, the bot still makes drafts and sends them to Telegram, but there's nothing to post them to.

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
5. Run `python scripts/linkedin_login.py`. It opens LinkedIn in your browser, saves the token in `.env`, and offers to update the `LINKEDIN_ACCESS_TOKEN` secret on GitHub for you. If that part was skipped, `python scripts/linkedin_login.py --upload` does it without logging in again.
6. Add `"linkedin"` to `posting.platforms` in `config.json`, then commit and push.

LinkedIn's token lasts 60 days and can't be renewed automatically, so run `linkedin_login.py` again every two months. If the token runs out first, drafts you've queued for LinkedIn wait, and their Telegram messages tell you to log in again.

### 7. Public Telegram channel (optional)

A channel anyone can join, where the bot posts the papers you pick: the card, the hook, the title, authors and journal, and the link.

1. In Telegram, create a channel (**New Channel**), make it **public**, and choose its link, for example `t.me/NeuroPaperDaily`.
2. In the channel's settings, open **Administrators**, add your bot, and let it **post messages**.
3. In `config.json`, set `posting.telegram_channel` to the channel's username, such as `"@NeuroPaperDaily"`, and add `"channel"` to `posting.platforms`. Then commit and push.

## Day to day with the GitHub version

- Drafts arrive in Telegram on weekdays, soon after 13:00 UTC.
- **📢 Channel**, **💼 LinkedIn** or **🦋 Bluesky** queues a draft for that platform; tap more than one to post it to each. Each platform posts at least 2 hours apart, oldest tap first.
- **📋 Ready to copy**, under each draft, has a short version for X and Bluesky and a longer one for anywhere else, link included. Tap a block to copy it.
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
| `posting.platforms` | The platforms drafts can go to: any of `"channel"`, `"linkedin"`, `"bluesky"` and `"x"`. Each gets a button on every draft. While it's empty (`[]`), drafts only have Skip. |
| `posting.telegram_channel` | Your public channel's username, such as `"@NeuroPaperDaily"` (step 7) |
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

## Tests

```
python tests/run_all.py
```

Each test works in a throwaway copy of the project with its own database, so your data is never touched. Telegram, Discord, Gemini, LinkedIn and Bluesky are replaced by stand-ins, so no keys are needed. Run them after changing the code, and before moving the bot to a server.

| Test | What it covers |
|---|---|
| `test_bot.py` | The public bot on Telegram: joining, setup and its summary, digests and **➡️ more**, sources and adding journals, summaries, search, author alerts and the daily schedule |
| `test_discord.py` | The same bot on Discord: its format, dropdowns, slash commands, and one person from joining to leaving |
| `test_quota.py` | Gemini's free-tier limits, and the daily update carrying on where it stopped |
| `test_channel.py`, `test_platforms.py` | The GitHub version: batches, review taps, and posting to the channel, Bluesky and LinkedIn. These two fetch real papers, so they need internet access. |

## Notes

- **Images:** every post uses the generated card. Papers' own figures aren't used, because many preprints don't allow reuse and journal figures are copyrighted.
- **X:** add `"x"` to `posting.platforms`, and add the `X_API_KEY`, `X_API_KEY_SECRET`, `X_ACCESS_TOKEN` and `X_ACCESS_TOKEN_SECRET` secrets. X posts are text only, with the link in a reply. This hasn't been tried with real X credentials.
- **No double posts:** before posting, `post.py` checks the bot's last 20 Bluesky posts for the same link. If an earlier run posted a draft but couldn't save that it had, the draft is recorded as posted instead of being posted again. LinkedIn doesn't let apps like this read posts back, so there's no such check there; each post is saved right after it goes out to keep the risk small.
- **LinkedIn posts** have the same text as Bluesky's, with a link card under it (title, abstract excerpt and card image). LinkedIn requires some characters, such as parentheses, to be escaped; `linkedin_api.py` does that, so a post isn't cut short.
