# API key setup

This explains how to get an Anthropic API key and where to put it so the brain can make real model calls. Until a key is in place the brain runs against a scripted fake model: nothing is spent and nothing leaves the laptop.

**Never paste a key into chat, an email, a document, a commit, or a screenshot.** The only place it belongs is the `.env` file described below.

## Two different things named "credits"

- **Your Claude subscription (Ultra)** pays for the development sessions (Claude Code and the agents that write and review the code). It does not pay for the brain's own model calls.
- **API credits** pay for the brain's own calls at run time. They live in an Anthropic Console organization (here: "Jeff's Individual Org", with $200 of credit) and are used through an API key.

Real usage is small: normal conversation costs a fraction of a cent per turn, about 10 cents on a light development day (see `docs/cost-estimate.md`). The real risk is a runaway loop, which is why there are caps in code and a limit on the key.

## 1. Get a key (about 5 minutes)

1. Open the Claude Console at **platform.claude.com** and sign in.
2. Switch to **Jeff's Individual Org** (organization switcher, top left).
3. Open **Billing** and confirm the credit balance shows the $200.
4. Open **Settings, then Workspaces**, and create a workspace named `embodied-agent-dev`. In its limits, set a **monthly spend limit of $25**. This is a second wall outside our code: even a bug in our cap cannot spend more than this.
5. Inside that workspace open **API keys**, click **Create key**, and name it `embodied-agent-dev`.
6. **Copy the key now.** It is shown only once. If you lose it, delete it and create another.

## 2. Save the key

1. The file `C:\jeffsource\EmbodiedAgent\.env` already exists (git-ignored, so it can never be committed). If it does not, copy `.env.example` to `.env`.
2. Open it in VS Code: `code C:\jeffsource\EmbodiedAgent\.env`
3. Paste the key right after the equals sign, with no quotes and no spaces:

   ```
   ANTHROPIC_API_KEY=your-key-here
   ```

4. Save the file. Leave `ELEVENLABS_API_KEY` and `DEEPGRAM_API_KEY` empty for now (they are for voice, later).

## 3. Check it worked (the key is never shown)

```powershell
cd C:\jeffsource\EmbodiedAgent
.\.venv\Scripts\python -m brain info
```

You should see `key ANTHROPIC_API_KEY: present`. The command prints only present or missing, never the value.

## 4. Turn live calls on, deliberately

Live calls are off by default as a second safety. To allow them, open `config.toml` and set, under `[models]`:

```toml
allow_live_calls = true
```

Then start a live chat:

```powershell
.\.venv\Scripts\python -m brain chat --live
```

It refuses with a clear message unless all of these hold: live calls allowed, a key present, prices known for both models, the API host allowed in `[egress]`, and the configured model ids exist on your account. It prints the cost after each turn.

## 5. The built-in limits (all in `config.toml`, under `[caps]`)

| Limit | Default | What it does |
| --- | --- | --- |
| Per session | $1.00 | Stops model calls for this run |
| Per day | $5.00 | Stops model calls for the day (remembered across restarts) |
| Hard stop | $10.00 | Total since the counter began; never resets by itself |
| Requests per hour | 300 | Loop guard |
| Reply length | 1,024 tokens | Bounds one reply |

At 80 percent of a cap you get one warning; at 100 percent further calls are refused. The key's monthly limit in the Console ($25) is the outer wall.

## 6. Optional: one tiny real test

This makes one small Haiku call (a fraction of a cent) and checks the usage numbers and that the only destination was `api.anthropic.com`. It only runs when you ask for it:

```powershell
.\.venv\Scripts\python -m pytest tests\test_real_vendor.py --run-real-vendor
```

## 7. Prompt capture (development)

While developing, the brain keeps the full prompt and full reply of every model call in a private file under `data\capture\`, so any unexpected result can be traced to its root cause. It records whatever is typed or said. It is size- and age-limited and is never committed. It can be turned off with `full_prompts = false` under `[logging]`.

## 8. A second person (for example Jeff's wife)

Each person gets their own profile, with their own key, their own limits and their own data. See `docs/profiles.md` (`python -m brain profile create NAME`, then `python -m brain chat --profile NAME`). In short:

- Create a second workspace in the same org (for example `embodied-agent-wife`) with its own monthly limit, and a key inside it; or she can use her own Console account and credits.
- Her key goes only in `profiles\<name>\.env`. A profile never uses your key or your environment variable.
- Prompt capture is off by default for her until she says yes.

## If something goes wrong

- **A key leaked** (pasted somewhere, committed, shown on screen): delete it in the Console right away and create a new one. Spend is capped by the workspace limit meanwhile.
- **`present` does not appear:** open `.env` and check there is no space, quote or line break around the key.
- **Calls refused with "budget":** a cap was reached; see section 5. A new day or a raised cap in `config.toml` clears it.
- **Unknown config key:** the brain stops at start and suggests the nearest valid key. Fix the spelling in `config.toml`.