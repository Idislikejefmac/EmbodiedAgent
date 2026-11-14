# Cost estimate for the brain's model usage

Date: 13 Nov 2026. Purpose: size the interim spend caps (DEC-068) against Jeff's $200 of API credit in the "Jeff's Individual Org" account, using expected usage. The development sessions themselves (Claude Code, the agents that write and review the code) run on Jeff's Ultra subscription and do not draw on these API credits; only the brain's own model calls at run time do.

## Assumptions (all from the plan's own tables; prices are NOT independently verified, re-check on the pricing page before relying on them)

| Item | Value |
| --- | --- |
| Prices, $ per million input / output tokens | Haiku 5.5 0.10 / 0.50 (prompts up to 100k; above that the whole request bills at 5x); Sonnet 5.5 2 / 10; Opus 5.5 4 / 20; Fable 5.2 8 / 40 (not used at run time) |
| Prompt cache | A cache hit costs 10 percent of normal input. System prompt, tool definitions and memory block (about 4,000 tokens) are cached |
| One 640 x 480 still | 414 tokens (plan). At most two images stay in context |
| A conversational turn | About 1,200 tokens of uncached history, 60 of new input, 90 of output, 1.5 model calls (tool round trips) |
| Session summary | Opus through the Batch API at half price, 20,000 tokens in, 800 out |
| Not included | Speech-to-text and text-to-speech vendor charges (unverified; check at P2-04), image decoding, anything outside the model API |

## Results (computed by script; the script is `costmodel.py` in the session scratchpad)

| Item | Cost |
| --- | --- |
| Plain Haiku conversational turn | $0.0003 |
| Haiku turn with two stills in context | $0.0004 |
| Sonnet "look carefully" turn | $0.009 |
| Waypoint description (Sonnet, one still) | $0.005 |
| End-of-session summary (Opus, Batch) | $0.05 |
| Light development day (40 turns, 10 with images, 3 careful looks, 2 waypoints, 1 summary) | about $0.10 |
| Heavy development day (300 turns, 80 with images, 20 careful looks, 8 waypoints, 2 summaries) | about $0.42 |
| A 10-minute voice session (30 turns) | about $0.03 |
| A month of 20 heavy days | about $8.50 |
| A month of 30 light days | about $3.00 |

**Conclusion: normal use costs pennies a day. $200 of credit is roughly two years of heavy development use.**

## Where the money could really go: runaways

The plan names the failure: an image sent every second with the history growing and no trimming. Each call re-bills every earlier image.

| Failure | Cost |
| --- | --- |
| Naive 1 Hz image loop on Haiku, 10 minutes | about $33 |
| The same on Sonnet, 10 minutes | about $150 (most of the credit) |
| The same on Sonnet, 60 minutes | about $5,400 |
| The same loop WITH the two-image rule, Haiku, 60 minutes | about $1 |
| The same with the two-image rule on Sonnet, 60 minutes | about $21 |
| Tool-call loop at the limit (20 turns x 8 calls, Sonnet) | under $1 |

So the real risks are a looping bug and a model sent too many images, not ordinary conversation. This is why the caps and the two-image rule are enforced in code, not left to the prompt.

## Recommended interim caps (until the full guard in P1-04)

| Cap | Value | Reason |
| --- | --- | --- |
| Per session | $1 | About 30 times a heavy session |
| Per day | $5 | About 10 times a heavy day |
| Hard stop (total since the brain started) | $10 | A runaway cannot reach the credit |
| Requests per hour | 300 | Loop guard; about 1 per 12 s, above normal use |
| Tool calls per turn | 8 (existing) | Loop breaker |
| Images in context | 2 (existing rule) | The runaway above |
| `max_tokens` per call | 1,024 | Bounds a single reply |
| Vendor-console limit on the API key | $25 per month | A second wall outside our code, in case our cap has a bug |

Setup (Jeff does these; do not paste keys into chat): create a dedicated API key in Jeff's Individual Org with a monthly spend limit of $25 in the Anthropic console, and put the key in `.env` as `ANTHROPIC_API_KEY`.

## What would change these numbers

Using Sonnet for every turn instead of Haiku is about 30 times the cost; sending every frame to the model; keeping full prompts does not change cost (it is only logging); speech vendors (unknown, likely the larger cost once voice is added); a very long conversation history (history is the main uncached cost). Re-run the model when P2-04 fixes the speech vendor and when real usage logs exist: the request log records tokens per call, so the first week of real use replaces these estimates with measurements.