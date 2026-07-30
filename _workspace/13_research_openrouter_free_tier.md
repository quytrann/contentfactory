# Research: OpenRouter Free Tier & Free-Tier LLM Alternatives for the Script-Gen Gate

**Date of research:** 2026-07-30
**Researcher:** `researcher` agent
**Question:** ContentFactory generates Vietnamese video scripts via `claude -p` (Claude Code headless, billed to a subscription). If the subscription ends, we need a provider-abstraction gate that can select among multiple FREE-TIER models. Owner named OpenRouter as the backend router.

**Constraints that govern the answer:** free only (no paid API without explicit owner approval), local Windows, RTX 2070 Max-Q 8GB, Vietnamese output, STRICT JSON output required, long prompts (10-60+ min transcripts, sometimes batched).

---

## 0. Evidence legend

Throughout this document:

- **[VERIFIED]** — I fetched the source today (2026-07-30) and the URL is cited. For OpenRouter I additionally hit the live API.
- **[THIRD-PARTY]** — only non-official sources (blogs/aggregators) confirm it; official docs did not state it.
- **[CONFLICTING]** — sources disagree; both positions recorded.
- **[UNVERIFIED]** — I could not confirm it; stated as a gap, not as a fact.

---

## 1. OpenRouter free tier — current rules

### 1.1 How `:free` variants work

A model ID ending in `:free` is a **zero-cost variant** of a model, served by an endpoint whose `pricing.prompt` and `pricing.completion` are both `"0"`. The same base model usually also exists as a paid slug (e.g. `google/gemma-4-26b-a4b-it` is paid; `google/gemma-4-26b-a4b-it:free` is free), and the free variant is routed to a **different, capacity-limited endpoint**. **[VERIFIED via live API]**

### 1.2 Live inventory of `:free` models (queried 2026-07-30)

I pulled `https://openrouter.ai/api/v1/models` directly (HTTP 200, 599 KB, 367 total models) and filtered for `:free`. **There are only 14 `:free` variants right now.** This is a much smaller and very different catalogue than the widely-circulated 2025-era lists (no `deepseek-r1:free`, no `llama-3.3-70b-instruct:free`, no Qwen free variants). **[VERIFIED via live API]**

| Model ID | Context | Max out | response_format | structured_outputs | tools |
|---|---|---|---|---|---|
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1,000,000 | 65,536 | no | no | yes |
| `inclusionai/ling-3.0-flash:free` | 262,144 | 32,768 | no | no | yes |
| `poolside/laguna-s-2.1:free` | 262,144 | 32,768 | no | no | yes |
| `poolside/laguna-xs-2.1:free` | 262,144 | 32,768 | no | no | yes |
| `google/gemma-4-26b-a4b-it:free` | 262,144 | 32,768 | **yes** | **yes** | yes |
| `google/gemma-4-31b-it:free` | 262,144 | 32,768 | **yes** | no | yes |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262,144 | 262,144 | **yes** | **yes** | yes |
| `cohere/north-mini-code:free` | 256,000 | 64,000 | no | no | yes |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 256,000 | 65,536 | no | no | yes |
| `nvidia/nemotron-3-nano-30b-a3b:free` | 256,000 | — | no | no | yes |
| `openai/gpt-oss-20b:free` | 131,072 | 32,768 | **yes** | **yes** | yes |
| `nvidia/nemotron-3.5-content-safety:free` | 128,000 | 8,192 | no | no | no |
| `nvidia/nemotron-nano-12b-v2-vl:free` | 128,000 | 128,000 | no | no | yes |
| `nvidia/nemotron-nano-9b-v2:free` | 128,000 | — | **yes** | **yes** | yes |

**Only 5 of 14 free models advertise `response_format`, and only 4 advertise `structured_outputs`.** For a strict-JSON pipeline that is the single most important column.

Additionally there are **3 zero-priced models without the `:free` suffix**: `openrouter/free` (see below), plus `google/lyria-3-pro-preview` and `google/lyria-3-clip-preview` (music/audio generation — irrelevant here). **[VERIFIED via live API]**

**Caveat on volatility:** the official free-router doc example shows a response served by `upstage/solar-pro-3:free`, a model **not present in my dump today**. The free catalogue therefore rotates on a timescale of days-to-weeks. Any gate must discover models at runtime, never hardcode a list.

### 1.3 `openrouter/free` — the Free Models Router **[VERIFIED]**

A new meta-model (introduced Feb 2026 per third-party reports; **[THIRD-PARTY]** on the date):

- ID `openrouter/free`, declared `context_length` 200,000, pricing `0`/`0`. **[VERIFIED via live API]**
- Docs: "openrouter/free is a router that selects free models **at random** from the models available on OpenRouter."
- It filters first: "Your request is analyzed to determine required capabilities (e.g., vision, tool calling, structured outputs)" then "A model is randomly selected from the filtered pool."
- Response `model` field tells you which model actually served it.
- Documented caveats, verbatim: "Free model availability can vary; some may be temporarily unavailable", "Free models may have higher latency during peak usage", "You cannot control which specific model is selected."
- Its `supported_parameters` includes `response_format` and `structured_outputs`. **[VERIFIED via live API]**

Source: https://openrouter.ai/docs/guides/routing/routers/free-router , https://openrouter.ai/openrouter/free

**Assessment for ContentFactory:** attractive as a *last-resort* tier because it self-heals against catalogue churn, but "randomly selected model" is actively bad for a pipeline where Vietnamese fluency and pace calibration matter. A random draw could hand a Vietnamese narration job to a code-specialist model (`cohere/north-mini-code:free`, `poolside/laguna-*`). **Do not make it the primary.**

### 1.4 Rate limits — exact current numbers **[VERIFIED]**

Verbatim table from the official docs:

| Credits purchased (all time) | Requests per minute | Requests per day |
|---|---|---|
| Less than 10 | 20 | 50 |
| At least 10 | 20 | 1000 |

Additional documented rules:
- Limits apply only to IDs ending in `:free`.
- "The daily limits reset on UTC days."
- "Creating additional accounts or API keys **will not affect your rate limits, as we govern capacity globally**." — i.e. multi-account farming is explicitly defeated.
- A **negative account balance triggers rate limiting even for free models**.

Source: https://openrouter.ai/docs/api_reference/limits (also reachable as `/docs/api-reference/limits`)

> ### ⚠️ FREE-ONLY RULE VIOLATION FLAG
> **The `$10` top-up is a PAID action.** It is a one-time purchase of credits, not a subscription, and the credits remain spendable — but it is money leaving the owner's pocket and therefore **requires explicit owner approval** under the project's "local & free only" constraint. Without it, OpenRouter free models are capped at **50 requests/day**.
>
> **Why 50/day is probably fatal for this pipeline:** script-gen for a long source is already batched (see memory note `scriptgen-batch-budget-pace`: 300s/batch budget for long sources, multiple chunks, plus regen attempts on duration-gate failure). A single 60-minute source video can plausibly consume 5-15 requests once chunking + regeneration is counted. 50 RPD is therefore on the order of **3-8 videos per day, worst case fewer**, with zero headroom for debugging. At 1000 RPD (post-$10) it is a non-issue.
>
> **Consequence:** OpenRouter-on-the-free-tier is viable as a *fallback leg*, but it is NOT a credible drop-in replacement for the current `claude -p` volume unless the owner approves the $10.

There is a documented conflict here worth recording: one third-party page claims free models are "typically 20 requests/minute, 200 requests/day". **[CONFLICTING]** — the official docs say 50/1000. Trust the official docs.

### 1.5 Is payment / a credit card required at all? **[VERIFIED, partially]**

- **No.** The FAQ states "All new users receive a very small free allowance to be able to test out OpenRouter", and free models are usable at $0 balance under the 50 RPD cap.
- The official FAQ does **not** explicitly say "no credit card required for signup". **[UNVERIFIED]** on the literal card question; but since the 50-RPD row of the limits table is defined as "credits purchased (all time) < 10", a $0 account is clearly a supported state.
- Source: https://openrouter.ai/docs/faq

### 1.6 Data / privacy policy for free models — **this is a real catch**

**[VERIFIED, official]** OpenRouter has an account-level privacy control: "On your account settings page, you can set whether you would like to allow routing to providers that may train on your data", and there are "**separate settings for paid and free models**". Requests can also be restricted per-request to providers with a given data policy.
Source: https://openrouter.ai/docs/features/privacy-and-logging

**[THIRD-PARTY]** Multiple secondary sources state the specific toggles are named:
- "Enable paid endpoints that may train on inputs"
- "Enable free endpoints that may train on inputs"
- "Enable free endpoints that may publish prompts"
- "Enable 1% discount on all LLMs"
- "ZDR Endpoints only"

and — importantly — that **you must enable the free-endpoint toggles or free models return an error**. I could not confirm the verbatim toggle names or the error text from the official docs page I fetched. **[UNVERIFIED on exact names/error]**

**Practical reading for ContentFactory:** using OpenRouter free models most likely means **accepting that prompts may be trained on and possibly published**. What we would be sending is: (a) transcripts of *public* YouTube videos, and (b) our script-generation prompt engineering. (a) is not sensitive. (b) is mildly proprietary but not a secret. **No credentials, tokens, or owner PII should ever be in a script-gen prompt** — worth an explicit assertion in the gate code.

**Interaction gotcha:** the provider-routing option `data_collection: "deny"` filters to providers that do not log — which would **exclude most/all free endpoints**. You cannot have both "free" and "deny data collection". **[VERIFIED that both features exist; the mutual exclusion is my inference — [UNVERIFIED] as a documented statement.]**

### 1.7 Structured output / JSON mode **[VERIFIED]**

- Mechanism: `response_format` with `type: "json_schema"` and a `json_schema` object; the model "respond[s] with a JSON object that strictly follows your schema."
- **Support is per-ENDPOINT, not per-model**: "the same model may be served by multiple providers, and only some of those providers may support structured outputs."
- Failure modes, verbatim: unsupported model/provider → "The request will fail with an error indicating lack of support"; bad schema → "The model will return an error if your JSON Schema is invalid".
- **The documented mitigation is `require_parameters: true`** in provider preferences, which restricts routing to providers supporting every parameter in your request.

Source: https://openrouter.ai/docs/features/structured-outputs , https://openrouter.ai/docs/features/provider-routing

**Live proof that this matters.** I queried the per-endpoint detail for the free variants (`/api/v1/models/<id>/endpoints`) — `google/gemma-4-26b-a4b-it:free` has **two** free endpoints with *different* capabilities:

| Model | Free endpoint provider | ctx | max_out | structured_outputs | response_format | uptime 30m / 1d |
|---|---|---|---|---|---|---|
| `google/gemma-4-26b-a4b-it:free` | **Darkbloom** | 131,072 | 32,768 | **yes** | yes | 97.03% / 93.71% |
| `google/gemma-4-26b-a4b-it:free` | **Google AI Studio** | 262,144 | 32,768 | **NO** | yes | n/a / 99.42% |
| `openai/gpt-oss-20b:free` | Darkbloom | 131,072 | 32,768 | yes | yes | 97.25% / 97.05% |
| `nvidia/nemotron-3-super-120b-a12b:free` | Nvidia | 262,144 | 262,144 | yes | yes | 99.60% / 99.65% |

**[VERIFIED via live API]** So the *same* `:free` ID can silently land on an endpoint that cannot do `structured_outputs`. **`require_parameters: true` is mandatory for this project, not optional.**

Note also: the top-level `context_length` in the models list is the **best** endpoint's context; the free endpoint can be half that (262k advertised → 131k on Darkbloom). A gate must read the *endpoint* context, not the model context, before deciding a 60-minute transcript fits.

### 1.8 API shape **[VERIFIED]**

- **Base:** `https://openrouter.ai/api/v1/chat/completions`, POST, JSON body.
- **Auth:** `Authorization: Bearer <OPENROUTER_API_KEY>`.
- **Optional attribution headers:** `HTTP-Referer` (app ranking) and `X-OpenRouter-Title` (formerly/aliased `X-Title`) for display name; also `X-OpenRouter-Categories`. Note the docs now lead with `X-OpenRouter-Title` rather than `X-Title`.
- **OpenAI compatibility:** docs state schemas are "very similar to the OpenAI Chat API" and responses "comply with the OpenAI Chat API specification, enabling use of existing OpenAI-compatible SDKs."
- **Model listing:** `GET https://openrouter.ai/api/v1/models` — **no auth needed** (I got HTTP 200 unauthenticated). Response shape: `{"data": [ ... ]}` where each entry has `id`, `canonical_slug`, `hugging_face_id`, `name`, `created`, `description`, `context_length`, `architecture{modality,input_modalities,output_modalities,tokenizer,instruct_type}`, `pricing{prompt,completion}`, `top_provider{context_length,max_completion_tokens,is_moderated}`, `per_request_limits`, `supported_parameters[]`, `default_parameters{}`, `knowledge_cutoff`, `expiration_date`, `links{details}`.
- **Per-endpoint detail:** `GET https://openrouter.ai/api/v1/models/<author>/<slug>[:free]/endpoints` → `{"data":{..., "endpoints":[{provider_name, context_length, max_completion_tokens, pricing, quantization, supported_parameters[], status, uptime_last_30m, uptime_last_5m, uptime_last_1d, ...}]}}`. **This is the endpoint a capability-aware gate should actually use.** Also unauthenticated.
- Sources: https://openrouter.ai/docs/api-reference/overview + live calls.

**Model fallback** (`models: [...]`): **[VERIFIED]**
```json
{
  "models": ["~anthropic/claude-sonnet-latest", "gryphe/mythomax-l2-13b"],
  "messages": [{"role": "user", "content": "Your question here"}]
}
```
- "any error can trigger the use of a fallback model", specifically: context-length validation errors, moderation flags, **rate-limiting**, and downtime.
- The model that actually served is in the response's `model` attribute.
- "When the last one also fails, that error comes back to you; there's no retry chain beyond what you listed."
- Source: https://openrouter.ai/docs/guides/routing/model-fallbacks

**Important doc change:** the old `route: "fallback"` parameter is no longer what the docs describe — the current mechanism is the `models: [...]` array (and `openrouter/auto-beta` for the smart Auto Router, configured via a `plugins` array with `id: "auto-router"`, `allowed_models`, `cost_quality_tradeoff` 0-10). Note `openrouter/auto` has `pricing: {"prompt":"-1","completion":"-1"}` i.e. **it is PAID/variable, not free**. **[VERIFIED via live API]** Do not confuse `openrouter/auto` (paid) with `openrouter/free` (free).

**Provider preferences fields** (the `provider` object): `require_parameters`, `order[]`, `allow_fallbacks` (default true), `only[]`, `ignore[]`, `data_collection` ("allow"|"deny"), `sort` ("price"|"throughput"|"latency"), `quantizations[]`. **[VERIFIED]**

### 1.9 Reliability of free models in practice

- **Hard uptime numbers I measured today** (from `/endpoints`): free endpoints ran **93.7%–99.7%** 1-day uptime, with the Darkbloom-served Gemma free endpoint the worst at **93.71% over 1 day / 97.03% over 30 min**. Nvidia's own free endpoint was best at 99.65%. **[VERIFIED via live API]** — that is genuinely useful, objective data: expect roughly a **1-in-15 to 1-in-30 request failure rate** on the weaker free endpoints.
- **429 behaviour** **[VERIFIED]**: 429 = "You are being rate limited — either by an OpenRouter platform limit (free-model caps, DDoS protection) or by the upstream provider." When OpenRouter enforces the limit, responses carry `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`; a `Retry-After` header may appear when the upstream provider suggests timing. 402 = insufficient credits / negative balance.
- **Community sentiment** **[THIRD-PARTY]**: consistent reports that ":free models are throttled the hardest", are "best-effort capacity", and that "free queues are overcrowded, resulting in 429 errors". I did not find a single authoritative bug thread; this is aggregated blog/Reddit-summary sentiment, so treat it as directional, not quantitative.
- **Upstream deprecation risk is REAL and I measured it**: the free catalogue today (14 models) shares almost nothing with 2025-era free lists, and even OpenRouter's own current doc example cites a model (`upstage/solar-pro-3:free`) that is not in the live list. **A hardcoded free-model ID will break, probably within weeks.**

---

## 2. Direct free-tier alternatives (not via OpenRouter)

### 2.1 Google AI Studio / Gemini API — **the strongest candidate**

- **[VERIFIED]** The official rate-limits page **no longer enumerates limits**: "Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio", pointing to https://aistudio.google.com/rate-limit. This is a documentation regression that makes exact numbers hard to cite. Source: https://ai.google.dev/gemini-api/docs/rate-limits
- **[VERIFIED]** The pricing page lists a Free-tier column. Models shown as having free-tier availability include: Gemini 3.6 Flash, 3.5 Flash, 3.5 Flash-Lite, 3.1 Flash-Lite, 3 Flash Preview, 2.5 Flash, 2.5 Flash-Lite, 2.0 Flash, 2.0 Flash-Lite, Gemma 4, plus some Live/embedding/robotics variants. Source: https://ai.google.dev/gemini-api/docs/pricing
- **[VERIFIED, and this is the important one]** Free tier: "Content used to improve our products". Paid tier: "Content **not** used to improve our products". **So Gemini's free tier trains on your prompts too** — same privacy posture as OpenRouter free. Same conclusion: fine for public transcripts, never for secrets.
- **[CONFLICTING] Gemini Pro on the free tier.** The official pricing page appears to still show a free-tier entry for Gemini 2.5 Pro, but multiple third-party sources state Google **removed Pro models from the free tier around 1 April 2026** ("Pro-tier models (3.1 Pro, 3 Pro, 2.5 Pro) had their free tier removed entirely"; surfaced via `This model requires a billing-enabled project` errors rather than a formal changelog). One source claims 2.5 Pro remains at a token 5 RPM. **I could not resolve this.** → **Plan on Flash/Flash-Lite only.** Do not architect around free Pro.
- **[THIRD-PARTY, treat as approximate] Free-tier numbers** circulating for mid-2026: Gemini 2.5 Flash ≈ 10 RPM / 250k TPM / **1,500 RPD**; Gemini 2.0 Flash ≈ 15 RPM / 1M TPM; Flash-Lite ≈ 2× the RPM (30). Gemini 3 Flash preview free with a 1,500 RPD cap. **These are not officially confirmed and must be checked in AI Studio against the real project key.**
- **Context:** Flash models are 1M-token input class — vastly more than a 60-minute transcript needs. **[THIRD-PARTY]** for the exact current figure per model, but the 1M class is long-established.
- **Structured output:** **[VERIFIED]** first-class. Current docs show a `response_format` config with `type: "text"`, `mime_type: "application/json"`, and `schema` (Pydantic `model_json_schema()` / Zod supported). Documented limits: "Not all JSON Schema features are supported", "Very large or deeply nested schemas may be rejected", and output is syntactically valid JSON but "always validate values in your application". Combining structured outputs *with tools* is preview and "only to Gemini 3 series models". Source: https://ai.google.dev/gemini-api/docs/structured-output
- **Credit card:** **[UNVERIFIED]** from official docs. Historically AI Studio free keys need no card; I could not confirm this on an official page today. **[THIRD-PARTY]** sources say "no card".
- **Vietnamese quality:** best of the free options in my judgement — see §3.

**Bottom line: if there is one free replacement for `claude -p` for Vietnamese script-gen, it is Gemini Flash via AI Studio.** ~1,500 RPD (if the third-party figure holds) is ~30× OpenRouter's free 50 RPD, and Google's Vietnamese is strong.

### 2.2 Groq **[VERIFIED]**

Free-tier limits table from official docs:

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| `llama-3.1-8b-instant` | 30 | 14,400 | 6,000 | 500,000 |
| `llama-3.3-70b-versatile` | 30 | 1,000 | 12,000 | 100,000 |
| `openai/gpt-oss-120b` | 30 | 1,000 | 8,000 | 200,000 |
| `openai/gpt-oss-20b` | 30 | 1,000 | 8,000 | 200,000 |
| `qwen/qwen3.6-27b` | 30 | 1,000 | 8,000 | 200,000 |
| `whisper-large-v3` | 20 | 2,000 | — | — |
| `whisper-large-v3-turbo` | 20 | 2,000 | — | — |

Also: "Rate limits apply at the organization level, not individual users" and "Cached tokens do not count towards your rate limits."
Source: https://console.groq.com/docs/rate-limits

**The killer constraint for us is TPM/TPD, not RPD.** `llama-3.3-70b-versatile` at **12,000 TPM / 100,000 TPD** cannot ingest many long transcripts: a 60-minute video transcript is easily 8-15k tokens, so **one or two long jobs per minute maxes TPM, and ~7-12 long jobs exhausts the entire day.** `qwen/qwen3.6-27b` is worse (8k TPM / 200k TPD, though double the daily). This is a *token*-starved free tier, which is exactly the wrong shape for our workload.

**Structured outputs** **[VERIFIED]**: true constrained decoding (`strict: true`) only on `openai/gpt-oss-20b` and `openai/gpt-oss-120b`; best-effort (`strict:false`) additionally on `openai/gpt-oss-safeguard-20b`. All other models get **JSON Object Mode only**, which "may not match your schema." Also: "Streaming and tool use are not currently supported with Structured Outputs."
Source: https://console.groq.com/docs/structured-outputs

**Credit card:** **[UNVERIFIED]** — the rate-limits doc does not say.

### 2.3 Cerebras — **now violates the free-only rule** **[VERIFIED]**

- Free-trial models: `gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b`.
- Limits, all three identical: **5 RPM**, 30k TPM, 1M TPH, **1M TPD**.
- **A credit card IS required.** Verbatim: "New accounts receive **$5 in free credits** after adding a verified payment method. These credits expire 30 days after they're granted." And: "If you skip adding a payment method at sign-up, Playground and API access remain inactive until you do."
- Source: https://inference-docs.cerebras.ai/support/rate-limits

> ⚠️ **FREE-ONLY RULE FLAG:** Cerebras now gates *all* API access behind a verified payment method, and its "free" allowance is **$5 of expiring trial credit**, not a standing free tier. This is a **card-on-file requirement** and an expiring trial. **Recommend excluding Cerebras.** (This is a change from its earlier genuinely-free tier — do not rely on older notes.)

### 2.4 Mistral La Plateforme **[THIRD-PARTY only — official URL 404'd]**

- The doc URL I tried (`docs.mistral.ai/deployment/laplateforme/tier/`) returned **HTTP 404**, so I have **no official confirmation**.
- **[THIRD-PARTY]** A free "Experiment" tier exists at $0 with access to all API models, ~**1 billion tokens/month** cap but a brutal **~2 RPM**; "Mistral no longer publishes exact free-tier rate numbers publicly — check Admin Console → Limits"; "It is for evaluation, not production"; no credit card required.
- **Phone verification:** **[UNVERIFIED]** — historically Mistral required phone-number verification for the free tier; I found no confirmation or denial today.
- **Assessment:** 2 RPM is workable for a batch pipeline (we are not latency-sensitive), and 1B tokens/month is enormous. But "evaluation, not production" plus unpublished limits plus 404'd docs = **too uncertain to build on**, and Mistral's Vietnamese is weaker than Gemini's or Qwen's (see §3).

### 2.5 GitHub Models **[VERIFIED, but muddled]**

- Official doc gives limits by plan and model tier. **Low-tier models**: 15 RPM (Free/Pro), 20 (Enterprise); **150-450 requests per day**; **8,000 tokens in, 4,000 out**; 5-8 concurrent. **High-tier models**: 10 RPM, 50-150/day, 2-4 concurrent. Embedding models: 64,000 tokens/request.
- Source: https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models

> ⛔ **DISQUALIFIER: 8,000 input tokens.** An 8k input cap makes GitHub Models **unusable** for 10-60 minute transcripts. Even aggressive chunking would fragment the source so badly the script would lose narrative coherence — which is the whole point of Recap/Commentary modes. **Exclude.**

- Note: **[THIRD-PARTY]** Copilot entitlements and Models API quotas are managed separately; a paid Copilot tier does not grant unlimited Models API access. Also **[THIRD-PARTY]** The Register reported (2026-04-15) customer backlash over Copilot rate-limiting changes — a signal that this surface is unstable.

### 2.6 Together AI / Fireworks AI **[THIRD-PARTY]**

- **Together:** the $25 signup credit was retired (reported July 2025); reports now say a **minimum $5 credit purchase is required upfront**. Sources conflict on whether any signup credit remains. **[CONFLICTING]**
- **Fireworks:** "$1 in free starter credits" (~1M tokens on a 70B-class model). "There is no permanently free model tier — it is a small credit on top of pay-as-you-go."

> ⚠️ **FREE-ONLY RULE FLAG:** Both are **expiring trial credits on a pay-as-you-go account**, not free tiers. Together may require a $5 purchase. **Exclude both** from a "must stay free" design.

### 2.7 Chinese providers

- **DeepSeek** **[THIRD-PARTY]**: one-time **5M-token grant** on signup, **valid 30 days**, **no credit card required**. After that, paid (V4-Flash ~$0.14/M). "There is no permanent free API tier, though promotional credits appear from time to time." → **A trial, not a free tier. Exclude as a standing leg**; usable as a one-off burst.
- **Alibaba DashScope / Qwen (Model Studio)** **[THIRD-PARTY]**: new accounts get **1M free tokens *per eligible model***, valid **90 days**, and only on the **International (Singapore) endpoint** — "The free quota exists only in the Singapore region (Beijing, Global, US, and EU have none)." Reports of "70M+ tokens free across Qwen models". Also: "The old free OAuth API tier was discontinued on **April 15, 2026**". → Large but **time-boxed (90 days)**, so again a trial. Signup friction: Alibaba Cloud account creation is heavier than Google's. Vietnam access via the Singapore endpoint should be fine (Singapore is the standard APAC region) — **[UNVERIFIED]** for Vietnam specifically.
- **Z.ai / Zhipu GLM** **[VERIFIED on the pricing page]**: **GLM-4.7-Flash**, **GLM-4.5-Flash** (text) and **GLM-4.6V-Flash** (vision) are listed at **Free** for input, cached input, storage, and output — i.e. a **standing $0 tier, not a trial.** Source: https://docs.z.ai/guides/overview/pricing
  - Context windows and rate limits are **not stated on that page** **[UNVERIFIED]**. **[THIRD-PARTY]** estimates: ~1 request/second, ~1,000 requests/day.
  - Credit-card requirement: **[UNVERIFIED]** officially; **[THIRD-PARTY]** pages advertise "no credit card required".
  - **This is the most interesting non-Google finding**: a genuinely perpetual free tier from a vendor whose models are decent at Vietnamese, and Cerebras independently serves `zai-glm-4.7`, corroborating that GLM 4.7 is a real current model.
- **Moonshot / Kimi**: **[UNVERIFIED]** — I did not get to a primary source on a current Moonshot free tier. Gap.

### 2.8 NVIDIA NIM (build.nvidia.com) **[THIRD-PARTY, CONFLICTING]**

- Reports: free API key via the NVIDIA Developer Program, **no credit card, no identity verification** (email only); **40 RPM** (extendable to 200 on request); 100+ models incl. DeepSeek, Llama, Qwen, Mistral, Nemotron.
- **[CONFLICTING]** on the quota: some sources say ~1,000 inference credits (up to 5,000 total); others say the free tier "never expires and requires no credit card". Cannot reconcile.
- **[THIRD-PARTY]** one source flags "a major privacy catch" — presumably prompt retention. Not verified.
- **Relevance:** NVIDIA is *already* the upstream for several of OpenRouter's free Nemotron endpoints (I saw `provider_name: "Nvidia"` with 99.65% uptime on `nemotron-3-super-120b-a12b:free`), so going direct to NIM would bypass OpenRouter's 50 RPD cap for those same models. **Worth a follow-up spike** — potentially the best way to get free Nemotron capacity without the $10.

### 2.9 Local fallback: Ollama / llama.cpp on RTX 2070 Max-Q 8GB

**[THIRD-PARTY]** consensus on VRAM fit:
- 7-9B class at **Q4_K_M** fits 6-8GB "though on the edge — best to close everything unnecessary."
- Suggested 2026 picks for 8GB: **Qwen3.5-4B** or Phi-4-Mini (3.8B) at Q4_K_M as the comfortable choice; Qwen3-8B / Llama-3.1-8B at Q4_K_M as the stretch.
- Qwen family is repeatedly called the strongest multilingual small-model family ("strongest across all model families", "best local model for non-English languages ... at near-native quality") — but note **every source I found emphasises CJK, not Vietnamese**. Vietnamese-specific evidence for local models is essentially absent. **[UNVERIFIED for Vietnamese.]**

**My honest engineering assessment (this part is reasoning, not a cited fact):**

1. **VRAM contention is the real blocker, not model quality.** The machine already runs SDXL (~6-7GB) and F5-TTS/VieNeu sequentially. Loading an 8B Q4 model (~5-6GB weights + KV cache) means the LLM must also be **loaded and unloaded around** the image/TTS steps. Ollama's `keep_alive` would have to be set aggressively short (or 0) so the model evicts before ComfyUI starts, adding **model-load latency to every script-gen call**.
2. **KV cache for long context is the hidden cost.** A 15k-token transcript at 8B Q4 needs meaningful KV memory on top of weights. On 8GB with SDXL potentially resident, expect to be forced to `num_ctx` far below the model's nominal 128k. **This is the single most likely failure mode**: the local fallback will not swallow a 60-minute transcript in one pass, so the gate needs a summarise-then-write two-stage path for the local leg specifically.
3. **Quality expectation for Vietnamese long-form narration at 4-8B Q4: poor-to-marginal.** Quantised sub-10B models tend to produce Vietnamese that is grammatical but stilted, with diacritic and register errors, and they are weak at *sustained* stylistic consistency over a multi-scene script. Given the project already fights pace/word-budget precision (see the `scriptgen-batch-budget-pace` and `narration-speed-rule` memory notes), a small local model will very likely miss word budgets and need more regeneration cycles.
4. **Therefore: position local as the "never fully offline" safety net, not a quality tier.** It guarantees the pipeline never hard-stops when every cloud free tier is exhausted, and it should be expected to produce drafts the owner reviews.

---

## 3. Vietnamese-language quality — and how weak the evidence actually is

### What I found

**VMLU** (https://vmlu.ai/leaderboard) **[VERIFIED]** — Vietnamese multitask knowledge benchmark:
- *From-scratch* category top entries are **stale**: QwQ-32B 76.13 (eval 13/03/2025), Qwen2.5-72B-Instruct-AWQ 69.17 (20/02/2025), Llama-3-70B 66.44 (23/04/2024), GPT-4 65.53 (08/01/2024). No 2026 frontier model has been submitted to this category.
- *Fine-tuned* category is current but dominated by **closed Vietnamese commercial models we cannot use**: VAI-LLM-v1.6 87.63 (22/06/2026), V-LLM v1.2 87.2 (04/05/2026), axis-sovereign 85.75, VAI-LLM-v1.5 85.47, Vi-Qwen3.6 85.36 (04/05/2026) — all **proprietary**.
- The Vietnamese open models the owner asked about — **SeaLLM, VietCuna, VinaLLaMA** — score **25-56 avg**, i.e. *far* below even 2024 general-purpose frontier models.

**SEA-HELM** (https://leaderboard.sea-lion.ai/) **[VERIFIED partially]** — AI Singapore's SEA benchmark, page dated **2026-07-10**, covering **62 open-weight + 9 closed-weight models** across "Southeast Asian chat, instruction-following in Southeast Asian languages, Southeast Asian linguistic tasks and performance on a suite of English tasks", including Vietnamese. **I could not extract the actual Vietnamese scores** — the page renders them dynamically and the fetched content had only placeholders. **[UNVERIFIED on numbers.]** This is the *most relevant* benchmark (it has generation and instruction-following categories, not just MCQ) and it is a real gap in this report.

**[THIRD-PARTY]** A 2026 Vietnamese-legal-text study (arXiv 2604.16270) benchmarked GPT-4o / Claude 3 Opus / Gemini 1.5 Pro / Grok-1 on Accuracy, **Readability**, and Consistency and reported "a distinct failure mode in fluency-accuracy trade-off — models can generate highly fluent, logically sounding legal reasoning that contains subtle interpretation errors." Directly relevant warning for our use case: **fluent Vietnamese ≠ factually faithful to the source transcript.** For Recap/Commentary modes, a hallucinated-but-fluent script is the dangerous failure, and it is the hardest to catch by eye.

**[THIRD-PARTY]** Aggregator recommendations for "best open-source LLM for Vietnamese 2026" name Qwen3-235B-A22B, Qwen3-8B, Llama-3.1-8B-Instruct. These are SEO-grade sources; low confidence.

### Honest conclusion on §3

**The evidence for "which free-tier model writes the best Vietnamese long-form narration" is weak, and I will not pretend otherwise.**

- Every benchmark I could actually read (VMLU) measures **multiple-choice knowledge**, not **generative fluency, register, or narration pacing** — which is what this project needs.
- The one benchmark that *does* measure generation for Vietnamese (SEA-HELM) I could not extract numbers from.
- The models that win Vietnamese benchmarks are **closed Vietnamese commercial models** unavailable on any free tier.
- Vietnamese-specific *open* models (Vistran/Vintern, Vistral, PhoGPT, SeaLLM, VinaLLaMA) are **benchmark-weak** (25-56 on VMLU) and mostly **stale** (2023-2024 era, 7B class). I found no evidence any of them beats a current general-purpose Flash-class model at Vietnamese narration. **My recommendation is to NOT pursue Vietnamese-specific finetunes** — the general frontier models have overtaken them.
- **`chưa tìm được bằng chứng dứt khoát`** for ranking free-tier models on Vietnamese narration quality.

**What I recommend instead of trusting benchmarks: an in-house A/B.** The project already has the tooling to judge this objectively-ish — generate the same scene list through 3-4 candidate providers, run the existing TTS + faster-whisper path, and have the owner rate fluency/pace. That is a `tester`/`content-strategist` task and would settle in one afternoon what no public benchmark answers.

**My prior, clearly labelled as a prior and not a verified fact:** Gemini Flash (3.x) is most likely the best free-tier Vietnamese writer, because Google has the largest Vietnamese web-corpus exposure and Gemini's Vietnamese has been consistently strong; GLM-4.7-Flash and Qwen-class models are the plausible runners-up; the Nemotron and poolside/Ling free models on OpenRouter are the ones I'd expect to be **weakest** at Vietnamese, being English/code-centric.

---

## 4. Claude Code headless — can `claude -p` be repointed?

### 4.1 Yes, and OpenRouter officially supports it **[VERIFIED]**

**OpenRouter exposes an Anthropic-compatible Messages endpoint.** I confirmed the route exists by probing it:

```
POST https://openrouter.ai/api/v1/messages       -> HTTP 401  {"error":{"message":"No cookie auth credentials found","code":401}}
POST https://openrouter.ai/api/v1/chat/completions -> HTTP 401
```

401 (not 404) on `/api/v1/messages` proves the Anthropic-shaped route is live. **[VERIFIED via live probe]**

Official setup (OpenRouter's own docs and blog, blog dated **2026-06-16**):

```bash
export OPENROUTER_API_KEY="<your-openrouter-api-key>"
export ANTHROPIC_BASE_URL="https://openrouter.ai/api"   # NOT .../api/v1/messages
export ANTHROPIC_AUTH_TOKEN="$OPENROUTER_API_KEY"
export ANTHROPIC_API_KEY=""                              # must be explicitly empty
export ANTHROPIC_DEFAULT_OPUS_MODEL="~anthropic/claude-opus-latest"
export ANTHROPIC_DEFAULT_SONNET_MODEL="~anthropic/claude-sonnet-latest"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="~anthropic/claude-haiku-latest"
export CLAUDE_CODE_SUBAGENT_MODEL="~anthropic/claude-opus-latest"
```

Claude Code **appends `/v1/messages` itself**, so the base URL must stop at `/api`.

Sources: https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration , https://openrouter.ai/blog/tutorials/claude-code-openrouter/

### 4.2 What breaks — and why this does NOT solve our problem **[VERIFIED]**

Verbatim caveats from OpenRouter's own docs/blog:

- **"Claude Code is optimized for Anthropic models and may not work correctly with other providers."**
- **"The native integration is built for Anthropic models and is only guaranteed to work with the Anthropic first-party provider."**
- **"Keep the models Anthropic, though."**
- What *does* work (with Anthropic models): "Thinking blocks, native tool use, streaming, and multi-turn context all work" identically to direct Anthropic API calls.
- On free models: they work but "have smaller context windows than paid Anthropic models, making them unsuitable for production sessions", and are under the 50/1000 RPD caps.
- Setup footgun: "If `OPENROUTER_API_KEY` is defined *later* in the file ... the auth token silently expands to an empty string and every request fails."
- Cached Anthropic logins must be cleared with `/logout` or you get "confusing model-not-found errors".
- Fast mode is Opus-only (4.6/4.7/4.8/5).

> ### 🔑 THE DECISIVE POINT FOR THIS MIGRATION
> Pointing `claude -p` at OpenRouter **does not achieve the owner's goal**. The goal is *"if the Claude subscription ends, keep generating scripts for free."* But:
> - Routing `claude -p` → OpenRouter → **Anthropic models** is **PAID per-token** — it replaces a subscription with metered API billing. That is strictly worse than today and **violates the free-only rule**.
> - Routing `claude -p` → OpenRouter → **`:free` models** is explicitly **not guaranteed to work** ("only guaranteed to work with the Anthropic first-party provider") and is capped at 50 RPD.
>
> **Therefore the recommended architecture is NOT to proxy `claude -p`.** It is to put the provider gate **inside the application** (`generate.py`) and make `claude -p` just *one* backend behind that gate. Script-gen is a single-shot "prompt in → JSON out" call — it does not need Claude Code's agentic machinery (tools, file access, multi-turn, thinking blocks) at all. A plain HTTP POST to an OpenAI-compatible endpoint replicates 100% of what script-gen actually uses, with none of the compatibility risk.

### 4.3 Third-party proxies

- **claude-code-router** (`musistudio/claude-code-router`) **[VERIFIED]** — 36.3k stars, 728 commits, v3.0.16, active. "A local model gateway and control plane" exposing one endpoint at `http://127.0.0.1:3456`; supports OpenAI, Gemini, OpenRouter, DeepSeek, SiliconFlow, Moonshot (Kimi), Mistral, and custom endpoints; does "protocol probing" and "model discovery". Windows note: "Windows app packaging must run on Windows x64 because better-sqlite3 ships a native Electron module." **871 open issues** — mature but churny. No explicit model-compatibility limitations documented.
- **y-router** (`luohy15/y-router`) **[VERIFIED it exists]** — "A Simple Proxy enabling Claude Code to work with OpenRouter". Lighter-weight alternative. I did not read its README in depth. **[UNVERIFIED on details]**
- **LiteLLM** — the obvious general-purpose option (Anthropic-shaped `/v1/messages` passthrough plus 100+ providers). **[UNVERIFIED]** — I did not fetch LiteLLM docs today; do not treat its current Anthropic-passthrough behaviour as confirmed.
- **llama.cpp** now implements the **Anthropic Messages API** natively (HF blog "New in llama.cpp: Anthropic Messages API") **[VERIFIED it exists as a published post]**, which means a *local* server could in principle answer `claude -p`. Interesting for the offline leg. **[UNVERIFIED on maturity/completeness]**

**Recommendation on proxies: skip them for script-gen.** They add a daemon, a config file, and a large open-issue surface to solve a problem (agentic protocol translation) that script-gen does not have. Keep a proxy in mind only if the owner later wants *interactive* Claude Code sessions on non-Anthropic models — a different use case.

---

## 5. OpenRouter SDKs and the OpenRouterTeam org

### 5.1 There IS now an official Python SDK **[VERIFIED]**

- Repo: https://github.com/OpenRouterTeam/python-sdk (146 stars, last push **2026-07-29**).
- PyPI package name: **`openrouter`** — `pip install openrouter`. Live PyPI metadata: **version 1.1.16**, `requires_python >=3.10`, summary "Official Python Client SDK for OpenRouter.", **115 releases**, latest upload **2026-07-29T22:55:53**. **[VERIFIED via PyPI JSON API]**
- README: "The OpenRouter Python SDK is **stable as of v1.0**." Usage `from openrouter import OpenRouter`. Docs at https://openrouter.ai/docs/sdks/python.
- It is a Speakeasy-generated, Pydantic-based typed client (README ships the standard Speakeasy sections + a PyCharm Pydantic plugin note).

**My recommendation for ContentFactory anyway: use plain `httpx`, not the SDK.**
1. The gate must talk to **Gemini, Z.ai, Groq and a local Ollama** too — a vendor SDK only abstracts one leg, so you end up writing raw HTTP for the others regardless.
2. `httpx` is almost certainly already in the API venv (FastAPI ecosystem); `openrouter` at 115 releases in its first cycle is a fast-moving dependency to pin.
3. The request bodies we need (`model`, `messages`, `response_format`, `models[]`, `provider.require_parameters`) are trivial JSON. The SDK buys nothing here.
4. If you want *some* abstraction, the `openai` package pointed at `base_url="https://openrouter.ai/api/v1"` covers OpenRouter **and** Groq **and** Z.ai **and** Ollama — one client shape, four providers. That is a better abstraction boundary than the OpenRouter SDK.

### 5.2 Notable OpenRouterTeam repos (36 public repos, queried live 2026-07-30) **[VERIFIED via GitHub API]**

| Repo | Stars | Lang | Last push | What it is |
|---|---|---|---|---|
| `ai-sdk-provider` | 672 | TS | 2026-07-23 | OpenRouter provider for Vercel AI SDK |
| `awesome-openrouter` | 402 | JS | 2026-07-29 | Curated list of apps using OpenRouter |
| `openrouter-examples` | 366 | TS | 2026-03-30 | API integration examples |
| `typescript-sdk` | 230 | TS | 2026-07-30 | Official TS SDK |
| `skills` | 194 | TS | 2026-07-29 | (no description) — Agent Skills |
| **`python-sdk`** | **146** | **Python** | **2026-07-29** | **Official Python SDK (`pip install openrouter`)** |
| `openrouter-examples-python` | 89 | Python | 2025-04-16 | Python calling examples (**stale ~15 months**) |
| `go-sdk` | 54 | Go | 2026-07-29 | Official Go SDK |
| `typescript-agent` | 19 | TS | 2026-07-29 | OpenRouter Agent SDK |
| `docs` | 5 | MDX | 2026-07-30 | The docs site source — **useful for exact current wording** |
| `terraform-provider-openrouter` | 2 | Go | 2026-07-29 | Official Terraform provider (keys, guardrails, workspaces, BYOK) |
| `sign-in-with-openrouter` | 6 | TS | 2026-03-10 | OAuth "Sign in with OpenRouter" templates |
| `search-benchmarks` | 0 | Python | 2026-07-18 | Eval framework for search APIs |

**Notable:** many of the org's repos are **forks of third-party projects** (`open-webui`, `deepclaude`, `openclaw`, `nanoclaw`, `amica`, `mindcraft`, `persona-hub`, `opik`, `cdp-agentkit`, `make-real`, `lux`, `llama-stack-evals`, `fern-platform`), most untouched since 2024-2025. **Do not mistake those for OpenRouter products.** The genuinely first-party, actively-maintained set is: the four SDKs (TS/Python/Go/agent), `ai-sdk-provider`, `docs`, `skills`, `terraform-provider-openrouter`, `awesome-openrouter`.

Also worth knowing: `openfreerouter/freerouter` (a *different* org) is a self-hosted OpenRouter alternative using your own API keys. **[THIRD-PARTY, not evaluated]**

---

## 6. Comparison table

RPD/RPM figures are per the sources cited above. **Bold = verified from official docs today.**

| Provider | Free quota (RPD / RPM / tokens) | Context | JSON mode | VN quality (evidence level) | Card required? | Notes / risks |
|---|---|---|---|---|---|---|
| **Gemini API (AI Studio)** — *Flash/Flash-Lite* | **[THIRD-PARTY]** ~1,500 RPD; 2.5 Flash ~10 RPM/250k TPM; 2.0 Flash ~15 RPM/1M TPM; Flash-Lite ~30 RPM. Official page no longer publishes numbers | ~1M in **[THIRD-PARTY]** | **Yes, first-class** (`response_format` + `mime_type: application/json` + `schema`; Pydantic/Zod). Big/nested schemas may be rejected | **Best guess = best of the free options.** Evidence: WEAK (no readable VN generation benchmark). My prior, not a fact | **[UNVERIFIED]** officially; **[THIRD-PARTY]** no | **Free tier trains on your prompts (official).** Pro removed from free tier ~Apr 2026 **[CONFLICTING]** → plan Flash only. Highest RPD of all options |
| **Z.ai / Zhipu GLM Flash** | **[THIRD-PARTY]** ~1 req/s, ~1,000 RPD | **[UNVERIFIED]** | **[UNVERIFIED]** — not on pricing page | Unknown; GLM generally decent multilingual. Evidence: VERY WEAK | **[UNVERIFIED]**; **[THIRD-PARTY]** no | **GLM-4.7-Flash / 4.5-Flash / 4.6V-Flash listed "Free" on official pricing** — a *standing* free tier, not a trial. Best non-Google find. Needs a verification spike |
| **OpenRouter `:free`** | **50 RPD / 20 RPM** at $0 → **1,000 RPD** after **$10** (paid) | Per-endpoint; **131k–1M** measured. Free endpoint often **half** the advertised ctx | **Yes on 5/14 free models** (`gemma-4-26b-a4b-it:free`, `gemma-4-31b-it:free`, `nemotron-3-super-120b-a12b:free`, `gpt-oss-20b:free`, `nemotron-nano-9b-v2:free`). **Per-endpoint** → `require_parameters:true` mandatory | Free catalogue is Nemotron/gpt-oss/poolside/Ling-heavy — I expect **weak** VN. Gemma 4 the best bet. Evidence: NONE | **[VERIFIED]** not for $0 tier | ⚠️ **$10 = paid, needs owner OK.** 14 free models only; catalogue rotates fast. Measured uptime **93.7–99.7%/1d**. **Requires enabling "may train / may publish prompts" toggles [THIRD-PARTY]**. One API for many models = best *abstraction* value |
| **NVIDIA NIM** | **[THIRD-PARTY, CONFLICTING]** 40 RPM (→200 on request); quota either ~1k–5k credits or unlimited | 100+ models incl. Nemotron/Qwen/Llama | **[UNVERIFIED]** | Unknown. Evidence: NONE | **[THIRD-PARTY]** **No** — email only | Bypasses OpenRouter's 50 RPD for the *same* Nemotron endpoints (I saw `provider_name: "Nvidia"`, 99.65% uptime). **[THIRD-PARTY]** privacy concerns. **Worth a spike** |
| **Groq** | **llama-3.1-8b: 30 RPM / 14,400 RPD / 6k TPM / 500k TPD; llama-3.3-70b: 30 / 1,000 / 12k TPM / 100k TPD; qwen3.6-27b & gpt-oss: 30 / 1,000 / 8k TPM / 200k TPD** | model-dependent | **Strict `json_schema` ONLY on `gpt-oss-20b`/`120b`**; all others JSON-object mode which "may not match your schema". No streaming/tools with structured outputs | Qwen3.6-27b plausible. Evidence: NONE | **[UNVERIFIED]** | ⛔ **TPM/TPD-starved: 12k TPM / 100k TPD ≈ 7-12 long transcripts/day total.** Wrong shape for our workload. Org-level limits. Fastest latency if it fits |
| **Mistral La Plateforme** | **[THIRD-PARTY]** ~2 RPM, ~1B tokens/month, "Experiment" tier | all models | Mistral supports JSON mode **[UNVERIFIED today]** | Weaker VN than Gemini/Qwen (my prior). Evidence: NONE | **[THIRD-PARTY]** no; phone verify **[UNVERIFIED]** | **Official docs URL 404'd today.** Limits unpublished ("check Admin Console"). "For evaluation, not production." Too uncertain to build on |
| **GitHub Models** | **Low tier: 15 RPM, 150-450 RPD; High tier: 10 RPM, 50-150 RPD** | ⛔ **8,000 tokens IN / 4,000 out** | Varies by model | n/a | No (GitHub account) | ⛔ **DISQUALIFIED — 8k input cap cannot hold a 10-60 min transcript.** Copilot quota ≠ Models quota |
| **Cerebras** | **5 RPM, 30k TPM, 1M TPD**; `gpt-oss-120b`, `zai-glm-4.7`, `gemma-4-31b` | model-dependent | **[UNVERIFIED]** | n/a | ⚠️ **YES — verified payment method mandatory** | ⚠️ **VIOLATES FREE-ONLY: "$5 free credits after adding a verified payment method", expire in 30 days; API inactive without a card.** Recommend exclude |
| **DeepSeek** | **[THIRD-PARTY]** one-time **5M tokens, 30-day expiry** | large | Yes (OpenAI-compat) **[UNVERIFIED today]** | Decent VN reported. Evidence: NONE | **[THIRD-PARTY]** no | ⚠️ **A TRIAL, not a free tier** — "no permanent free API tier". Exclude as standing leg |
| **Alibaba DashScope / Qwen** | **[THIRD-PARTY]** 1M tokens **per model**, **90-day expiry**, **Singapore endpoint only** | large | Yes (OpenAI-compat) **[UNVERIFIED today]** | Qwen = strongest multilingual small-model family **[THIRD-PARTY, CJK-focused]** | **[UNVERIFIED]** | ⚠️ **Time-boxed trial (90d).** Free OAuth tier killed 2026-04-15. Heavier signup. VN-from-Singapore fine **[UNVERIFIED]** |
| **Together AI** | **[CONFLICTING]** $25 signup credit retired Jul 2025; reports of **$5 minimum purchase required** | large | Yes | n/a | Likely yes | ⚠️ **VIOLATES FREE-ONLY.** Exclude |
| **Fireworks AI** | **[THIRD-PARTY]** **$1** starter credit (~1M tokens on 70B) | large | Yes | n/a | Likely yes | ⚠️ **"No permanently free model tier."** Exclude |
| **Local Ollama / llama.cpp (RTX 2070 8GB)** | Unlimited (electricity only) | ⚠️ **KV-cache-bound far below nominal** — likely a few k to ~16k usable with SDXL contention | llama.cpp/Ollama support grammar/JSON-schema constrained decoding **[UNVERIFIED today]** | **Poor-to-marginal** at 4-8B Q4 for VN long-form (my assessment). Evidence: NONE for VN | **No** | ✅ **Only truly unlimited & private option.** But: **VRAM contention with SDXL/TTS** forces load/unload per call; won't swallow a 60-min transcript in one pass → needs a 2-stage summarise-then-write path. Picks: Qwen3.5-4B / Qwen3-8B / Llama-3.1-8B @ Q4_K_M |

---

## 7. Recommendation

### 7.1 Provider ladder (my recommendation)

1. **Primary: Gemini Flash via AI Studio** — highest free RPD by ~30×, 1M context (no chunking needed for a 60-min transcript, which removes a whole class of coherence bugs), first-class JSON schema, and the best expected Vietnamese. This, not OpenRouter, is the real answer to "what replaces `claude -p` for free."
2. **Secondary: OpenRouter** — keep it as the **breadth** leg, and use it for what it is uniquely good at: one API + `models: [...]` fallback + runtime model discovery. At $0 it is a 50-RPD *spillover*, not a primary. If the owner approves the $10, it becomes a genuine 1,000-RPD second primary.
3. **Tertiary: Z.ai GLM-4.x-Flash** — the only other apparently-perpetual $0 tier found. **Needs a verification spike** (context window, RPD, card requirement) before being trusted.
4. **Investigate: NVIDIA NIM** — potentially free Nemotron capacity without OpenRouter's cap. Cheap to test.
5. **Floor: local Ollama** (Qwen3.5-4B or Qwen3-8B Q4_K_M) — guarantees the pipeline never hard-stops. Expect draft quality; needs its own reduced-context, two-stage prompt path and careful `keep_alive` so it does not fight SDXL for VRAM.

**Excluded on the free-only rule:** Cerebras (card mandatory), Together (purchase required), Fireworks ($1 credit only), DeepSeek & DashScope (expiring trials — usable for a burst, not a standing leg).
**Excluded on capability:** GitHub Models (8k input cap), Groq (TPM/TPD too small for long transcripts).

### 7.2 Design notes the gate must honour (derived from the findings above)

1. **Do NOT proxy `claude -p`.** Put the gate in `generate.py` and make `claude -p` one backend among several. Script-gen uses none of Claude Code's agentic features. (§4.2)
2. **Discover models at runtime, never hardcode.** The OpenRouter free catalogue changed beyond recognition since 2025 and even its own docs cite a model that no longer exists. Poll `/api/v1/models` and filter `pricing.prompt == "0"`. (§1.2)
3. **Check the ENDPOINT, not the model.** Use `/api/v1/models/<id>/endpoints` and read that endpoint's `context_length` and `supported_parameters` — the free endpoint can have half the advertised context and lack `structured_outputs`. (§1.7)
4. **Always send `provider: {"require_parameters": true}`** on OpenRouter when using `response_format`, or requests will land on incapable endpoints. (§1.7)
5. **Never rely on `response_format` alone — always validate the parsed JSON.** Both Gemini's and OpenRouter's docs say output is *syntactically* valid but semantically unvalidated; Groq's non-gpt-oss models can't honour a schema at all. Keep a repair/retry path.
6. **Budget by TOKENS, not just requests.** Groq and Mistral cap TPM/TPD, and that binds long before RPD does. The gate's quota accounting needs both counters per provider.
7. **Treat all free tiers as "prompts may be trained on / published."** Verified for Gemini free tier (official) and strongly indicated for OpenRouter free endpoints. Assert in code that no credential, token, `Dashboard/secrets/**` path content, or owner PII ever enters a script-gen prompt. Note that `data_collection: "deny"` and "free" are mutually exclusive on OpenRouter.
8. **Expect ~1-in-15 to 1-in-30 failures on weak free endpoints** (measured 93.7% 1-day uptime on one). Retry-with-different-provider must be the default path, not an error case. Honour `Retry-After` / `X-RateLimit-Reset` on 429.
9. Use **`openai` package or plain `httpx`**, not the OpenRouter SDK — one client shape covers OpenRouter + Groq + Z.ai + Ollama. (§5.1)

### 7.3 Open gaps I did NOT resolve (honest list)

- **SEA-HELM Vietnamese scores** — the most relevant benchmark; page is JS-rendered and I could not extract numbers. Biggest single gap in §3.
- **Gemini free-tier exact RPM/TPM/RPD** — Google stopped publishing them; only third-party figures. Must be read from AI Studio with a real key.
- **Whether Gemini Pro still has any free tier** — official pricing page and third-party reports contradict each other.
- **Z.ai GLM free tier**: context window, rate limits, card requirement — all unconfirmed.
- **NVIDIA NIM quota** — sources contradict (credits vs unlimited).
- **OpenRouter privacy toggle exact names + the error when a free endpoint is blocked** — third-party only.
- **Whether "free" and `data_collection: "deny"` are formally mutually exclusive** — my inference, not documented.
- **Mistral free tier** — official docs URL 404'd; everything is third-party.
- **Moonshot / Kimi free tier** — not researched to a primary source.
- **LiteLLM's current Anthropic-passthrough behaviour** — not verified today.
- **Credit-card requirements for Groq and Gemini** — not stated on the official pages I read.

**Recommended next step before any code is written:** a short verification spike that (a) creates a $0 Gemini AI Studio key and reads the real limits off `aistudio.google.com/rate-limit`, (b) creates a $0 Z.ai key and measures GLM-4.7-Flash's context + RPD, and (c) runs the *same* Vietnamese scene-list prompt through Gemini Flash / Gemma-4-26B:free / GLM-4.7-Flash / local Qwen3-8B and has the owner rank the narration. That answers the two questions no public source can: real quotas, and real Vietnamese quality.

---

## 8. Source list

**OpenRouter (official)**
- https://openrouter.ai/api/v1/models — live, unauthenticated (367 models, 14 `:free`)
- https://openrouter.ai/api/v1/models/{id}/endpoints — live, per-endpoint capabilities + uptime
- https://openrouter.ai/docs/api_reference/limits — free-model rate limit table
- https://openrouter.ai/docs/faq — $0 accounts, free allowance
- https://openrouter.ai/docs/api-reference/overview — API shape, headers, auth
- https://openrouter.ai/docs/features/structured-outputs — `json_schema`, per-provider support
- https://openrouter.ai/docs/features/provider-routing — `require_parameters`, `data_collection`, etc.
- https://openrouter.ai/docs/features/model-routing — Auto Router / `plugins`
- https://openrouter.ai/docs/guides/routing/model-fallbacks — `models: [...]` array
- https://openrouter.ai/docs/guides/routing/routers/free-router — `openrouter/free`
- https://openrouter.ai/openrouter/free
- https://openrouter.ai/docs/features/privacy-and-logging — training toggles
- https://openrouter.ai/docs/guides/privacy/provider-logging
- https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration — Claude Code env vars
- https://openrouter.ai/blog/tutorials/claude-code-openrouter/ — dated 2026-06-16
- https://api.github.com/orgs/OpenRouterTeam/repos — live, 36 repos
- https://github.com/OpenRouterTeam/python-sdk
- https://pypi.org/pypi/openrouter/json — live, v1.1.16, uploaded 2026-07-29

**Google**
- https://ai.google.dev/gemini-api/docs/pricing — free-tier column, training statement
- https://ai.google.dev/gemini-api/docs/rate-limits — now defers to AI Studio
- https://ai.google.dev/gemini-api/docs/structured-output
- https://aistudio.google.com/rate-limit — referenced, requires login (not fetched)

**Other providers (official)**
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/structured-outputs
- https://inference-docs.cerebras.ai/support/rate-limits — card requirement
- https://docs.z.ai/guides/overview/pricing — GLM Flash models "Free"
- https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models
- https://docs.mistral.ai/deployment/laplateforme/tier/ — **HTTP 404 today**

**Benchmarks**
- https://vmlu.ai/leaderboard
- https://leaderboard.sea-lion.ai/ — page dated 2026-07-10; VI scores not extractable
- https://arxiv.org/html/2604.16270v1 — Vietnamese legal text, fluency-accuracy trade-off

**Proxies / tooling**
- https://github.com/musistudio/claude-code-router — v3.0.16, 36.3k stars
- https://github.com/luohy15/y-router
- https://huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp

**Third-party (lower confidence, used only where marked)**
- https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know
- https://flo2.com/blog/openrouter-rate-limits
- https://www.getaiperks.com/en/ai/gemini-pro-free-tier-killed
- https://help.apiyi.com/en/google-gemini-api-free-tier-changes-april-2026-guide-en.html
- https://tokenmix.ai/blog/gemini-api-free-tier-limits
- https://yangmao.ai/en/providers/nvidia-build/
- https://yangmao.ai/en/providers/qwen/
- https://pricepertoken.com/endpoints/fireworks/free
- https://pricepertoken.com/endpoints/mistral/free
- https://www.free-llm.com/provider/z-ai
- https://awesomeagents.ai/tools/free-ai-inference-providers-2026/
- https://localaimaster.com/blog/best-local-ai-models-8gb-ram
- https://www.siliconflow.com/articles/en/best-open-source-LLM-for-Vietnamese
- https://www.theregister.com/2026/04/15/github_copilot_rate_limiting_bug/
