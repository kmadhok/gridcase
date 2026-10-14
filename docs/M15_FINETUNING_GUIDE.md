# M15 fine-tuning guide: how labs do it, where we stand, what to decide

Date 2026-10-14. Written for the M15 owner (data scientist, new to LLM fine-tuning). Analysis only: nothing here changes code, data or plans, and no paid calls were made.

**Sources.** This guide synthesises six fact-checked research notes in `data/rl/diagnosis/research/` (all six `*.verified.md` files exist and were used: `frontier-process`, `sft-distillation`, `rl-eval-practice`, `input-design`, `reward-design`, `forecasting-llm`) plus `docs/M15_DIAGNOSIS.md`. Those notes hold the full citations, verification logs and repo recomputations; this guide links the primary sources inline. All web sources were accessed 2026-10-14. Where two notes disagreed, the reconciliation and its reason are stated in place. A critic's review was applied; see §10.

**Money left: ≈ $70, not $80.** Spent $64.41 before the pilot (`docs/DECISIONS.md:696`) plus $15.61 since (pilot $12.10, Phase 4 probes $3.51; `data/rl/spend.jsonl`) = $80.02 of $150. The ledger on this machine started empty (`DECISIONS.md:737`), so the `Spend` guard currently believes ≈ $134 is left. Seed it with the earlier $64.41 before any paid call.

---

## 1. Bottom line

1. Our plan's *order* (fix inputs → short supervised warm start → short guarded RL → one-shot holdout) is the order labs use. The money spent before it was not: we ran 45 RL steps ($54) before testing a $0.04 prompt change that, **in a no-tool probe**, did more for direction than all of them. It is untested with tools until P3.
2. Approve D1 (show drops as rises of the other outcome, and state the market-implied number q). Use the sentence that was actually tested (arm D); a shorter gloss would be a new, untested arm. In the no-tool probe BD moved spike Brier 0.311 → 0.226 and direction corr −0.64 → +0.47.
3. Approve D2: stop verifying the free-text summary as a claim, and let claims cite the task facts. The penalty currently measures a broken spec, not forecast quality. It costs $0 and needs a DECISIONS.md line (rule 5).
4. Choose D3 = **match the market with correct structure**. No published system we found, fine-tuned or not, beats a current, liquid market price on its own; the fine-tuned evidence is two papers, one a vendor blog. Our tools carry no news, which is where published edges come from.
5. But note: **q itself passes the "match" bar** (it is non-inferior to itself, right-signed, calibrated as well as itself). So fine-tuning must earn its ≈ $30–60 by adding something beyond q (a significant blend gain, better placeholder handling), or by being the learning exercise the owner wants. Say which before spending.
6. **Training on Claude outputs is, on our reading, not permitted without Anthropic's authorisation.** The Usage Policy bars using outputs "to train an AI model (e.g., 'model scraping' or 'model distillation') without prior authorization"; the in-force text says "inputs and outputs", the 12 Nov 2026 text "outputs". This is our reading, not legal advice; asking Anthropic is a route, not something to plan around. So the Sonnet-trajectory P4 is dropped. Our Sonnet transcripts have no thinking text anyway, only short after-the-fact summaries.
7. Climb a **cost ladder** and choose a rung (§7): (1) prompt + recalibration + sample averaging, ≈ $17–39; (2) add self-training SFT, ≈ $31–45; (3) add RL, ≈ $62–70 with no contingency. **Recommendation: rung 1 now, decide on rung 2 after P3. Do not plan on RL**: no RL run we can afford can show a measurable Brier gain (holdout minimum detectable effect ≈ 0.013–0.020).
8. If SFT runs, use **rejection-sampled self-training (RFT)** on Qwen3-8B's own thinking-on answers on train tasks, not distillation (we have no teacher that beats q). Cost ≈ $13–17.
9. **First next step:** free offline work (seed the ledger, re-score old evals under D2 with a tightened matcher, build the mirroring tool wrapper, recalibration and blend checks), then **P3: a full-tool validation eval of BD, thinking off and on, plus averaging, few-shot and spread arms, ≈ $6.**

---

## 2. Fine-tuning in one page, for a data scientist

An LLM is a conditional density model over the next token, p_θ(token | everything before it). A **tokenizer** splits text into these tokens; **logprobs** are the model's log-probabilities of each token. Every training method below is a different way of choosing which sequences to raise the likelihood of.

| Stage | What it does | Familiar DS analogue |
|---|---|---|
| **Pretraining** | Maximum likelihood of next tokens over trillions of web tokens | Fitting a huge generative model; this sets the *prior* the later stages adjust |
| **SFT** (supervised fine-tuning) | Cross-entropy on chosen (prompt, ideal response) pairs; loss only on the response tokens | Ordinary supervised learning, or behaviour cloning. Each example gives a dense signal (every token is a label) |
| **Preference tuning** (DPO and kin) | Raise the chosen answer over the rejected one in pairs | A pairwise (Bradley–Terry) ranking loss |
| **RL with verifiable rewards** (RLVR; GRPO is one variant) | Sample several answers per prompt, score each with a program, raise the log-likelihood of above-average answers and lower the rest | REINFORCE with a per-prompt baseline: an importance-weighted MLE whose weights are centred rewards. The group mean is a control variate |
| **Distillation** | Train a small *student* on a stronger *teacher*. Off-policy: SFT on the teacher's samples. On-policy: the student samples and the teacher scores every token (needs the teacher's logprobs) | Model compression; on-policy is like fitting the student to the teacher's density on the student's own data |
| **Rejection sampling / RFT / STaR** | Sample from the model itself, keep samples that pass a filter, SFT on them | Self-training or pseudo-labelling with a filter |
| **LoRA** | Freeze the weights W; learn a low-rank update ΔW = BA (rank 32 here), about 1% of the parameters | Reduced-rank regression; a strong constraint on the update, not on the model |

Five facts drive almost every decision in this guide:

- **Information per episode.** SFT gives O(number of tokens) bits per example. Policy-gradient RL gives O(1) bits per episode, because the only feedback is one scalar ([LoRA Without Regret](https://thinkingmachines.ai/blog/lora/)). So RL is a slow learner, and LoRA rank is not our bottleneck: LoRA matches full fine-tuning for RL "even with ranks as low as 1".
- **RL re-weights; it does not invent.** If the right answer has near-zero probability under the model, almost no samples contain it, so the gradient towards it is near zero. This is importance sampling without support. OpenAI's RFT guide: "If a model has a 0% success rate at a given task, you cannot bootstrap to higher performance levels through RFT" ([OpenAI RFT guide](https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning)). Yue et al. show RL-trained models win at pass@1 (one try) but base models win at pass@k for large k (best of k tries) ([arXiv 2504.13837](https://arxiv.org/abs/2504.13837)). ProRL argues the opposite under thousands of GPU-hours ([arXiv 2505.24864](https://arxiv.org/abs/2505.24864)), which we cannot afford. This is our run 1: 5.6% of thinking-off samples were on the correct side of held down-moves. (Both papers are maths/code RL, not forecasting.)
- **SFT moves the answer directly but narrows spread.** Cross-entropy pulls the model towards the targets. That is good for teaching a new behaviour and bad for later RL, which needs varied samples to compare ([GEM, arXiv 2408.16673](https://arxiv.org/abs/2408.16673); [Llama 4 blog](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)).
- **Proper scoring rule, noisy target.** Our reward is 1 − Brier − penalty on one binary outcome. In expectation E[(p − y)²] = (p − P)² + P(1 − P), so it points at the true probability P. Per episode it is a coin flip, so each episode carries very little signal. Labs that do forecasting RL use 10k–110k questions ([Turtel et al.](https://arxiv.org/html/2505.17989v4)). The same identity says a *random* answer p pays an extra Var(p): averaging several samples per task removes most of it for free (§5 P3).
- **Thinking on/off.** Qwen3 is a "hybrid" model: the same weights can write a `<think>` block before answering or skip it. With thinking off our model writes the number first with no reasoning (144/144 answers; 11 of 1,101 turns contain any text). With thinking on (T = 1.0), its answers vary much more within a task (sd 0.187 against 0.03–0.06), which is the variety RL needs.

---

## 3. How frontier labs do it, and what we copy

### 3.1 The process, step by step

Closed labs (OpenAI, Anthropic, Google) publish almost nothing specific about post-training, so the detailed evidence comes from open reports: DeepSeek, Qwen, Kimi, AI2 and Meta. Most of it is maths and code at scale. Their shared process:

| # | Step | What labs do | Sources |
|---|---|---|---|
| 1 | **Evals and baselines first** | Build dev and *unseen* eval splits and measure baselines before training. Tülu 3 "did not examine scores on our unseen set when developing our models" | [Tülu 3](https://arxiv.org/html/2411.15124v3); [OpenAI model optimization](https://developers.openai.com/api/docs/guides/model-optimization); [Anthropic: define success](https://platform.claude.com/docs/en/test-and-evaluate/define-success) |
| 2 | **Climb the cheap rungs** | Prompt and input work, then examples, and only then fine-tuning. OpenAI: "The prompt engineering process may be all you need." Anthropic: "finding the simplest solution possible, and only increasing complexity when needed" | [OpenAI](https://developers.openai.com/api/docs/guides/model-optimization); [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) |
| 3 | **Curate and decontaminate** | Drop easy or guessable items (Kimi excludes true/false questions as guessable) and remove overlap with eval sets | [Kimi k1.5](https://arxiv.org/html/2501.12599v1); [Tülu 3](https://arxiv.org/html/2411.15124v3) |
| 4 | **Short SFT "cold start"** | A few thousand long-reasoning examples before RL. Qwen3: "minimize both the number of training samples and the training steps during this preparatory phase". Llama 4 calls it "lightweight SFT" and pruned >50% of SFT data, because heavy SFT "can over-constrain the model, restricting exploration during the online RL stage" | [DeepSeek-R1 v1](https://arxiv.org/html/2501.12948v1); [Qwen3](https://arxiv.org/html/2505.09388); [Llama 4](https://ai.meta.com/blog/llama-4-multimodal-intelligence/) |
| 5 | **Small models: distil from a stronger teacher** | Qwen3-8B (our model) was made by distillation from 32B/235B teachers. On it, on-policy distillation beat RL on AIME'24 (a maths-competition benchmark: 74.4 vs 67.6) at about 1/10 of the GPU hours. DeepSeek: RL on a 32B base reached 47.0 against 72.6 for distillation. **Not our case:** this needs a teacher much stronger than the student, and we have none that beats q (Sonnet 0.213 vs q 0.212) | [Qwen3 Table 21](https://arxiv.org/html/2505.09388); [DeepSeek-R1](https://arxiv.org/html/2501.12948v1) |
| 6 | **Optional preference tuning** | DPO between SFT and RL (Tülu 3, OLMo 3) | [OLMo 3](https://arxiv.org/html/2512.13961) |
| 7 | **RL with programmatic rewards** | Correctness plus a format reward, no neural reward model ("susceptible to reward hacking"), medium-hard prompts, attention to exploration and to truncated samples | [DeepSeek-R1](https://arxiv.org/html/2501.12948v1); [DAPO](https://arxiv.org/html/2503.14476) |
| 8 | **Develop small, then scale** | OLMo 3: the recipe "was developed on 7B or smaller and applied to 32B rapidly" | [OLMo 3](https://arxiv.org/html/2512.13961) |
| 9 | **Monitor and select** | Log reward, response length, truncation, entropy/KL and degenerate groups (groups where every sample gets the same reward, so they give no gradient); save checkpoints and pick on dev evals | [TRL GRPO docs](https://huggingface.co/docs/trl/main/en/grpo_trainer); [RLHF book](https://rlhfbook.com/c/06-policy-gradients) |

Two qualifications, so this is not read as dogma:
- **RL without SFT also works.** DeepSeek-R1-Zero, OLMo 3 RL-Zero and SimpleRL-Zoo all start RL from a base model. SimpleRL-Zoo finds SFT first "significantly limits the model's ability to explore freely during RL" ([arXiv 2503.18892](https://arxiv.org/abs/2503.18892)).
- **In forecasting specifically, SFT before RL is not standard.** OpenForecaster (Qwen3-8B, thinking on, GRPO) trains RL only. Its SFT→RL ablation gave "much better calibration albeit with a slightly lower brier score" on a higher-is-better scale, i.e. slightly *worse* Brier ([arXiv 2512.25070](https://arxiv.org/html/2512.25070v2)). The Tinker Cookbook forecasting recipe is also RL-only.

### 3.2 What forecasting RL precedents look like

| Precedent | Model | Data / compute | Result |
|---|---|---|---|
| [Turtel et al. 2025](https://arxiv.org/html/2505.17989v4) | R1-Distill-Qwen-14B (reasoning), news, no price in prompt | 10k real + 100k synthetic questions, one chronological pass; entropy-loss coef 0.001, initial KL 0.005 (verl trainer) | Base 0.215 → 0.199 (10k); a 7-sample ensemble reaches 0.190. Polymarket: 0.151 |
| Tinker Cookbook `recipes/forecasting` (installed) | Qwen3.8-27B, low-reasoning renderer | 16 questions × 32 samples × 100 steps = 51,200 episodes | Brier 0.199 → 0.175; first measured gain +0.006 at step 20 (≈10k episodes) |
| [OpenForecaster](https://arxiv.org/html/2512.25070v2) | Qwen3-8B, thinking on, full fine-tune | ≈52k questions, ≈1,300 steps, ≈1,000 H100-hours | Better forecaster, open-ended format; no market comparison |
| [Mantic × Thinking Machines](https://thinkingmachines.ai/news/training-llms-to-predict-world-events/) | gpt-oss-120b on Tinker, with tools | ≈10k questions, group 8, batch 64 | +7 Metaculus baseline-score points (+3 without tools); most useful inside an ensemble |

Every one trains a reasoning (thinking-on) model, and none starts from 8B with ~10 steps. At thinking-on prices our whole remaining budget buys under ≈ 3,500 RL episodes, about a fifteenth of the Cookbook run.

### 3.3 Copy, skip, and why

| Copy (cheap, high value) | Skip (lab scale, or wrong setting) |
|---|---|
| Eval-first gates; holdout touched once (Tülu dev/unseen discipline) | Reward models and DPO: we have no preference data, and Brier is already a proper reward |
| Inputs before weights (BD framing); free recalibration before training | Multi-round pipelines (Llama 3 ran six rounds) |
| A *light* SFT warm start, 1 epoch, a few hundred examples | Long RL runs, data-mixture research, GRPO-variant hunting (DAPO, GSPO, "clip-higher" change how far one batch may move the policy when it is reused for several updates; we take one on-policy step per batch) |
| Fix format and reward spec before RL | On-policy distillation from another model: no Tinker teacher shares Qwen3-8B's tokenizer, and Sonnet exposes no logprobs |
| Thinking on, with an adequate token budget | Training on Claude outputs (our reading of the Usage Policy, §5 P4) |
| Small smoke tests; per-direction monitoring; select on validation | Trying to *beat* q outright |

---

## 4. Where we deviated, and what it cost

| Deviation | What happened | Cost |
|---|---|---|
| **RL before the cheap rungs** | Run 1 (39 steps) and the pilot (6 steps) ran before any prompt/input A/B. When the A/B ran, BD (≈ $0.04, no tools, single turn) gave direction corr +0.47. Run 1 reached a better Brier (0.247) only by collapsing to 0.65 | $41.97 for run 1 (measured) + $12.10 for the pilot, against $3.51 for all of Phase 4's probes |
| **The key baseline came late** | The market-implied rule q (0.212) was computed after run 1. With it, run 1's "improvement" reads as a collapse towards a constant that is still worse than the best constant (0.2425) | Lost a cheap early stop signal |
| **Copied a recipe from a different task** | The config came from the Cookbook *search_tool* recipe: Qwen3.5-4B, thinking off, exact-match QA, 512 groups per step. Every forecasting precedent uses a reasoning model, and our 16 groups per step is 1/32 of its batch | The model wrote numbers with no reasoning, and samples had almost no spread for RL to use |
| **No warm start, from a backwards prior** | RL started from an anchored prior (p ≈ 0.44 + 0.42·price_now − 0.27·down, R² 0.82) whose samples almost never contained the right answer | RL could only shift the level ("answer higher after drops"), not learn the rule |
| **Reward spec bug live during RL** | The answer tool asks for a summary "without numbers", but 389/390 summaries contain one, and each is dropped and counted towards the 0.25 penalty. Malformed answers (−0.25) were ≈ 48% of within-group reward variance and the claim penalty ≈ 23%; Brier only 29% | Advantages correlated +0.39 with distance-to-q, against +0.50 with a 0.05 penalty (both under the current claim rules, before D2). A noisier gradient, not a biased one |
| **Over-correction on exploration** | The pilot's spread term is 0.1. In our trainer it is the Cookbook's KL-into-advantages form with a uniform reference, added to batch-std-normalised advantages (`gridcase/rl/train.py:490-492`). Turtel's 0.001 and Cui et al.'s 0.01 (which made entropy "explode", [arXiv 2505.22617](https://arxiv.org/html/2505.22617v1)) are entropy-loss coefficients in other trainers, so the numbers are not directly comparable | Our judgment: 0.1 is probably too strong and part of "no collapse, no learning" |
| **Long run before a smoke test** | 39 steps first, then the toy and the 6-step pilot | Reversed lab practice |
| **SFT data plan conflicts with terms** | P4a planned to train on ≈400 Sonnet trajectories; see §5 P4 | P4 must be redesigned |

What we did right, and should keep: frozen eval, validation and holdout sets; the `trades_as_of` leakage rule; a fresh holdout later in time than everything trained on; a thorough diagnosis before spending more; a collapse guard.

**Two corrections to the diagnosis found by the research.**
- The "with clean summaries the penalised share would fall to 17% / 7%" figure counts a clean summary as a kept claim. The code (`gridcase/rl/agent.py:212`) removes it, so clean summaries alone give about 36% / 33%. Adding "claims may cite the task facts" brings it to ≤ 12% / ≤ 1% (upper bounds, under the current lenient matcher; a tighter matcher will raise them).
- The 19% thinking-on malformed rate is mostly **format failures**, not truncation: the model wrote the submit JSON as plain text, hallucinated a tool response, or wrote "**Final Answer:** 0.35". At most one or two of 15 look like length cut-offs. The fix is mainly a parser/retry fix, not more tokens. (The thinking-off default cap is 1,024 tokens per turn, `gridcase/rl/models.py:109`; thinking-on runs must not use it.)

---

## 5. Each step of the plan (P3–P6)

### P3: full-tool validation eval of the BD framing

- **Best practice.** Test a framing change in the real setting (with tools) before building on it. Present the transform identically in training and serving ([Rules of ML #32, #37](https://developers.google.com/machine-learning/guides/rules-of-ml)). Report results across formats, because LLMs are format-sensitive: up to 76 accuracy points across meaning-preserving formats ([Sclar et al.](https://arxiv.org/html/2310.11324)).
- **Planned.** BD with tools, thinking off and on, ≈ $2.
- **Verdict: keep, and add cheap arms.**
  1. **Mirror in the tool layer, not only the prompt.** Tools currently report first-outcome prices (`gridcase/tools/hist.py`). One shared `Frame` object should drive the prompt and the own-market tools (`hist.move`, `hist.series`, `hist.market.price_is_for`). Leave sibling prices raw but labelled. The receipt records the frame. The system sentence (`agent.py:90-91`) and the `hist.market` description (`agent.py:47-48`) must be reworded, not deleted (arm E, which deleted the "prices are probabilities of the first outcome" sentence, hurt). Exit test: the prompt's price equals `hist.move.price` in the declared frame; a "frame-mismatch flag" is any episode where they differ. Resolve tasks are not mirrored.
  2. **Use the tested D sentence** (`experiments/m15_diagnosis/p1_framing.py:98-103`): "the probability implied by the current price that the price ends on the side this move points to is {q}", put in a citable receipt. A shorter gloss on the shown price would be a new arm whose effect P3 must measure. Note that neither wording is the question asked ("half the move still there at 72h"); the two events are proxies, which is why q's ECE is ≈ 0.055, not 0.
  3. **Thinking-on settings:** eval at T = 0.6, top-p 0.95, top-k 20 (top-p/top-k sample only from the most likely tokens), per the [Qwen3-8B card](https://huggingface.co/Qwen/Qwen3-8B). Keep the probe's 8,192-token cap; raise it only if truncation exceeds ≈ 3%. Add the Cookbook's retry-on-parse-error (`ParseErrorPolicy.max_consecutive=1`) and a fallback parser for a submit JSON written as text. Report malformed answers split into truncated, unparsed and over-budget.
  4. **Averaging arm (≈ $1.5):** thinking on, k = 3 samples per task, report the mean p. A random answer pays its variance Var(p) in expected Brier; at T = 1.0 that was ≈ 0.035 (sd 0.187), larger than the whole BD-vs-q gap. At T = 0.6 it will be smaller, so measure it. Turtel's 7-sample ensemble gained 0.009.
  5. **Few-shot arm (≈ $1):** BD plus two worked examples in the prompt (a held down-move, a launch placeholder). The one cheap rung never tried.
  6. **Spread-and-yield probe (≈ $1.4):** T = 1.0, 10 up + 10 down tasks × 8 samples. It measures within-task spread and how many down tasks yield a usable correct-side sample, which sets P4's stop rule.
  7. **Report a copy statistic** (share of answers within 0.01 of q, and mean |p − q|) next to Brier. Comparing an LLM to the market is "circular, because the LLM can just copy the market probabilities" ([Paleka et al., ICLR 2026](https://arxiv.org/html/2506.00723v1)).
- **Free follow-up: per-direction recalibration.** The remaining BD loss is up-move inflation (up Brier 0.268; 62% of BD up-move samples are exactly 0.65). Fit a per-direction Platt (logistic on logit p) or isotonic map of BD outputs, cross-fitted on validation or fitted on train-task samples, before any SFT whose main job would be the same. $0.
- **Gates (validation, per direction):** spike Brier ≤ 0.25; direction corr > 0; down corr(p, q) ≥ 0.4; malformed ≤ 10% thinking off and ≤ 15% thinking on; zero frame-mismatch flags; verifier drop rate no worse than framing A (the current wording).
- **Cost ≈ $6** (thinking off $0.92, thinking on ≈ $1.3–2, averaging ≈ $1.5, few-shot ≈ $1, spread probe ≈ $1.4), at the 8k cap.

### P4: SFT warm start (rung 2 only)

- **Best practice.** A light cold start: hundreds to a few thousand curated examples, one epoch, loss only on the model's own turns. Keep it short, because heavy SFT narrows exploration ([Qwen3](https://arxiv.org/html/2505.09388); [Llama 4](https://ai.meta.com/blog/llama-4-multimodal-intelligence/)).
- **Planned.** P4a: ≈400 Sonnet trajectories on train tasks (≈ $13–17); P4b fallback: scripted q-answer transcripts (≈ $4).
- **Verdict: change the data source.**
  - **P4a is dropped.** The Usage Policy in force (15 Sep 2025) prohibits "Utilization of inputs and outputs to train an AI model (e.g., 'model scraping' or 'model distillation') without prior authorization from Anthropic" ([in force](https://www.anthropic.com/legal/archive/22742366-2ef0-4c7a-a833-6523f10d3944)); the version effective 12 Nov 2026 says "Use outputs to train an AI model … without prior authorization" under "Do Not Abuse Our Platform" ([AUP](https://www.anthropic.com/legal/aup)). The Commercial Terms incorporate the Policy (§D.2) and separately bar training "competing AI models" (§D.4) ([Commercial Terms](https://www.anthropic.com/legal/commercial-terms)). Tinker's terms forbid uses that violate third-party terms ([Tinker terms](https://thinkingmachines.ai/legal/terms/)), which is an indirect link only. The text says "an AI model", with examples that leave room for interpretation, and offers prior authorisation as a route. **This is our reading, not legal advice.** Asking Anthropic is an option; do not plan around it. Sonnet stays usable as an eval baseline.
  - **P4b fits only a thinking-off, no-RL product.** Scripted transcripts contain no reasoning, so they would teach a thinking model to skip thinking and copy q, leaving no spread for RL. It is the only sensible SFT if Gate A routes to thinking off (thinking-off self-sampling gives 5.6% correct-side samples, too few to filter).
  - **New P4 = P4-self: rejection-sampled self-training (RFT)** ([STaR](https://arxiv.org/abs/2203.14465), [RFT](https://arxiv.org/abs/2308.01825)): Qwen3-8B's own filtered samples, so no stronger teacher and no terms issue (Qwen3 is Apache-2.0). Not to be confused with "self-distillation" (SDFT, `tinker_cookbook/recipes/sdft`), a different method.
- **Revised design.**
  1. *Pool:* ≈280 train spike tasks (balanced up/down, stratified by price band) plus ≈50 resolve tasks. Exclude the 45 `condition_id`s that also appear in eval, validation or holdout (34/850 and 42/1,872 tasks overlap by market). Hold out 10% of tasks for validation loss.
  2. *Sampling:* BD framing, thinking on, T = 1.0, 3 samples per spike task and 2 per resolve task, with the same renderer and `strip_thinking_from_history` setting (whether earlier turns' thinking is removed from the context) P5 would use.
  3. *Filters:* well-formed; within budget; non-summary claims survive the verifier; within 0.15 of q (Halawi's band); no "half the move" misreading; stated reasoning agrees with the number. The last two need a labeller: use rules or Qwen itself, **not Claude**, since Claude labelling training data is a grey zone under the same policy. Then, per bucket (direction × price band), trim until the mean kept p is within ≈ 0.03 of the train hold rate. That step uses **aggregate** train labels (fine, but not label-free) and targets up-move inflation. Keep at most 2 *different* traces per task.
  4. **Never filter on per-sample outcome Brier.** Keeping samples close to the realised y selects extreme answers on both sides and breeds overconfidence. Halawi et al. say crowd-beating selection "can inadvertently cause overconfidence" ([arXiv 2402.18563](https://arxiv.org/html/2402.18563)).
  5. *Training:* LoRA rank 32 (same adapter as RL), LR 2e-4 with linear decay, batch 16–32, 1 epoch, `TrainOnWhat.ALL_ASSISTANT_MESSAGES`, checkpoints at 0.5 and 1.0 epochs. Count tokens with the Cookbook's `viz_sft_dataset` before paying.
  6. *Stop rule:* yield is unknown. The ≈ 55% figure came from 4 samples, one filter, 10 tasks and the old prompt. Set the threshold from the P3 spread probe's per-task yield under the full filter stack; stop if projected kept traces < ≈ 150.
  7. *Tension to accept:* traces kept near q, in a prompt that shows q, teach copying q. Under D3 = match that is expected, so copy share is **report-only**, and the spread gate below is what protects RL.
- **Gates (validation):** spike Brier ≤ 0.23; direction corr > 0; within-direction corr(p, y) > 0; up-move mean p within 0.08 of the hold rate; across-task sd(p) ≥ 0.12; malformed ≤ 10%; resolve no worse than the market price. Then, only if RL is planned, a **post-SFT spread probe** (≈ $0.7): within-task sd ≥ 0.12 and ≥ 20% correct-side samples on held down-moves at T = 1.0.
- **Cost ≈ $13–17:** sampling $6–9, training $4.5–5 per epoch (per-turn splitting, i.e. one training example per assistant turn, makes ≈ 27k training tokens per episode), gates ≈ $2.6.
- **Legitimate alternative: skip SFT.** If P3's thinking-on arm already has ≤ 10% malformed after the parser fix, good spread and a right-signed down side, RL directly from BD is defensible (OpenForecaster and the Cookbook recipe do exactly that).

### P5: guarded RL (rung 3; not recommended within budget)

- **Best practice.** RL on a reasoning model from a right-signed start. Group ≈ 8, T = 1.0, mean-centred advantages, little or no KL, no entropy bonus. Watch answer-level collapse, not just token entropy. Pick checkpoints on held-out data.
- **Planned.** 10 steps, thinking on, spike-only, malformed masked, penalty 0.05, entropy ≤ 0.01 or KL-to-base 0.02 (≈ $21).
- **Verdict: optional, with these settings.**

| Setting | Planned | Revised | Why |
|---|---|---|---|
| Spread term | ≤ 0.01 | **0** (judgment) | Different mechanism from the published coefficients (§4), so no clean transfer. A survey finds "weak to no gains" from entropy bonuses in LLM RL ([arXiv 2509.03493](https://arxiv.org/abs/2509.03493)). Caveat: run 1 had no spread term and collapsed; the pilot had one and did not. Setting 0 relies wholly on thinking-on spread and the sd(p) < 0.08 stop rule |
| KL | 0.02 to base | **0.01 to the SFT checkpoint**, or 0 | KL to base pulls back to the backwards prior. Labs mostly drop KL ([DAPO](https://arxiv.org/html/2503.14476), [OLMo 3](https://arxiv.org/html/2512.13961)). The Cookbook supports a checkpoint reference (`KLReferenceConfig.load_checkpoint_path`, `tinker_cookbook/rl/train.py:83-95`), but our trainer builds its reference from the base model: engineering work, plus reference-logprob cost (`gridcase/rl/train.py:494`) |
| Malformed | masked | **Masked from the group baseline, rate logged, stop if it rises**; alternative: a fixed reward strictly below the worst valid one (e.g. −0.10 with penalty 0.05) | Current reward is 1 − Brier − penalty, malformed −0.25 (`gridcase/rl/reward.py:11,31`). Malformed answers were ≈ 48% of within-group reward variance, so keeping them at a large gap keeps them the main noise source. [Skywork-OR1](https://arxiv.org/html/2505.22312v2)'s case against masking (truncation creeping up over thousands of steps) does not arise in 10 steps. Fix format at the source first (retry + parser) |
| Length normalisation | per-episode (`length_norm=True`) | **Keep** (with the ×4 answer-turn weight); log answer-token weight | [Dr. GRPO](https://arxiv.org/html/2503.20783)'s fix (divide by a constant, not each sequence's length) comes from long maths runs; over 10 steps the bias is small, and it would further dilute the answer digits (≈ 0.2% of token weight) |
| Penalty | 0.05 | **0.05, after the D2 fix** | Within the Cookbook's 0.1 |

Kept as is: LoRA rank 32, LR 4e-5 constant (inside the 1e-5 to 8e-5 range of Tinker recipes), group 8 × 16 tasks, T = 1.0 for training, `batch_std` advantages (rewards minus the group mean, divided by the batch's standard deviation), one on-policy step per batch, spike-only stratified batches, Brier on y (not q) as the reward. A q target is noise-free but its optimum is copying q, and its "+0.96 alignment" in our analysis was circular.

- **Monitoring.** Training reward cannot show learning: its per-step standard error is 0.058–0.071. Log per step: degenerate-group share, malformed/truncated rate, response length, sd(p), distinct p, share of p outside [0.1, 0.9], per-direction mean p, KL to the start, copy-q share. Every 5 steps run a fixed 60-task validation probe (T = 0.6, 2 samples).
- **Stop rules (any one):** malformed > 15% for 2 steps; sd(p) < 0.08; > 30% of answers outside [0.1, 0.9] (Turtel's collapsed GRPO had 39%); mean length up > 50%; gibberish or language mixing in the thinking; zero-claim rate > 5%.
- **Keep a checkpoint only if**, against its start on 150 validation tasks: distance to q not worse by > 0.005; direction corr ≥ start − 0.05; sd(p) ≥ 0.12; within-direction corr(p, y) > 0 both ways; malformed ≤ 10%. Copy share and Brier are reported, not gated (validation MDE ≈ 0.04).
- **Cost ≈ $27–35:** 10 steps × ≈ $2.5 sampling (1.36 × $1.85, from the thinking-on probe) plus up to ≈ $1.5 per step training tokens on long traces and ≈ $0.5–0.7 per step reference logprobs if KL is on, plus 2 probe evals. Re-estimate after step 1 and stop if the projection exceeds the rung.
- **Expectation, honestly.** At the Cookbook's pace, a 10-step run gives roughly +0.008 Brier even if 10× faster, below the holdout's detectable effect. Judge P5 on "no harm plus better structure".

### P6: final holdout eval

- **Best practice.** Develop on validation; score the frozen holdout **once** with pre-declared models, metrics and margins. Reusing a holdout adaptively breaks its guarantees ([Dwork et al.](https://research.google/blog/the-reusable-holdout-preserving-validity-in-adaptive-data-analysis/)). Use paired tests on identical questions, and check robustness to correlated outcomes in the same period ([Paleka et al.](https://arxiv.org/html/2506.00723v1)).
- **Planned.** Holdout eval, ≈ $10–13, gate spike Brier ≤ 0.22 and direction corr > 0.
- **Verdict: keep, and pre-register.** Before running, write into DECISIONS.md:
  - **Models (≤ 3), all on the same 1,334 spike tasks:** q (free), the best prompt-only arm (with recalibration and/or k-sample averaging if they won on validation), and at most one trained model.
  - **Decoding:** T = 0.6, top-p 0.95, top-k 20, the P3 token cap.
  - **Primary test (D3 = match):** non-inferiority. Paired bootstrap of Brier(model) − Brier(q), resampling by market (1,334 tasks on 1,020 markets); pass if the 95% upper bound is ≤ +0.015. **In practice the observed gap must be about ≤ +0.006 to +0.009 to pass.** BD's no-tool validation gap (+0.014) would fail; recalibration or averaging must close it. Sensitivity: a calendar-week block bootstrap.
  - **Secondary:** direction corr > 0 with its CI excluding 0; per-direction mean p within 0.07 of the holdout's own hold rate; within-direction corr(p, headroom) < 0 both ways; sd(p) ≥ 0.12; ECE ≤ ECE(q) + 0.01 (an absolute 0.05 bar would fail q itself); malformed ≤ 5%; copy-q share; launch-placeholder vs other split.
  - **What must beat q's own result** (since q passes all of the above): a fixed blend 0.25·p + 0.75·q against q, or an encompassing test (does p add significant information in y ~ logit(q) + logit(p)?) fitted on validation; and the placeholder subset. Report-only unless pre-registered as primary.
  - **Labels:** one seed, so the result answers "is *this checkpoint* better?", not "does this recipe work?". Sonnet's 0.213 is an eval-set reference under framing A, not a paired holdout comparison.
- **Cost (8k cap):** ≈ $11 per model thinking on, single sample; ≈ $33 at k = 3; ≈ $8 thinking off.

---

## 6. The three decisions

### D1: may the task inputs change?

| Option | Evidence | What you lose |
|---|---|---|
| **A. Yes: mirror drops in the tool layer and state q with the tested D sentence (recommended)** | No-tool probe: Brier 0.311 → 0.226, direction corr −0.64 → +0.47, down corr(p, q) −0.59 → +0.67. Input work before weights is the lab ladder. Showing the market number at inference is standard and reported as its own condition ([ForecastBench](https://arxiv.org/html/2409.19839v5), [AIA Forecaster](https://arxiv.org/abs/2511.07678), [Schoenegger et al.](https://arxiv.org/abs/2402.19379)) | Untested with tools until P3. No published *fine-tuning* run shows the price in the prompt, so we are an early data point. Earlier results become "framing A" results. Mirroring may weaken sibling comparisons (mitigate by labelling raw sibling prices) |
| B. Mirror only, no q sentence | B alone: Brier 0.278, direction corr +0.19; the D sentence is what pulls answers towards q (mean \|p − q\| 0.23 → 0.15) | Most of the gain |
| C. No change; teach the symmetry by training on both forms | Doubles data or episodes; half the RL budget goes to the form the model is bad at | Budget, and the evidence that it works is about pretraining knowledge, not in-context presentation |

**Recommendation: A.** Mirroring is lossless and exactly invertible: the label and the "half the move still there" event do not change. After mirroring, q equals the shown price (150/150 tasks), so the D sentence restates it with a meaning attached. Conditions: one `Frame` object drives prompt and tools; the frame goes in every receipt; a B-vs-B′ consistency check (B′ = the reverse mirror, rises restated as drops; ≈ $0.04) runs as a regression test per checkpoint; Brier − Brier(q) by direction becomes the primary metric. It needs a `REVIEW:` line in DECISIONS.md (rule 5).

### D2: the summary penalty

| Option | Effect on penalised share (base_eval / pilot step 6) | Cost |
|---|---|---|
| Keep as is | 65% / 52% | The penalty measures a spec mismatch (even Sonnet breaks "without numbers" 81% of the time) and adds noise to every advantage |
| Strip numbers before verification | Same as the next row under current code (`agent.py:212`) | Silently rewrites what the model wrote |
| **Stop verifying the summary as a claim** | ≈ 36% / 33% | $0 |
| **…and let claims cite the task facts as a receipt (recommended)** | ≤ 12% / ≤ 1% under the current matcher (upper bounds; expect higher under the tightened one) | $0, plus a slightly larger receipt set |

**Recommendation: stop verifying the summary as a claim, register the task facts as a citable receipt, and change the tool text to "summary: one or two sentences of reasoning (not checked)".** Specification gaming is "caused by misspecification of the intended task, rather than any flaw in the RL algorithm" ([Krakovna et al.](https://deepmind.google/discover/blog/specification-gaming-the-flip-side-of-ai-ingenuity/)). Rewarding the surface form of text written *after* the number trains it to look clean, not to be right. Two guards:
- Keep logging the summary drop rate as a monitor.
- **Tighten number matching for the benchmark, and use it for the re-score.** Today `_matches` accepts any value within 0.5 of any reference below 100 (`gridcase/explain/verifier.py:105`), so any probability "verifies" against a prompt price. The product verifier (hard rule 3) can stay as it is.

**What you lose with "keep":** RL keeps optimising a rule the model cannot meet, the penalty stays ≈ 23% of reward variance, and SFT filtering would reject almost every trace.

### D3: beat the market, or match it with correct structure?

| Goal | What the evidence says | What it costs |
|---|---|---|
| **D3-0. Ship q as the number; the LLM explains it with cited receipts** | q passes every "match" criterion by construction (non-inferior to itself; right direction; corr(q, headroom) = −1 both ways; ECE = ECE(q)). Hard rule 3's explainer already does the explaining | $0. You learn nothing about fine-tuning, and the LLM adds no forecast |
| **D3-1. Match q with correct structure (recommended)** | Plausible: BD alone is +0.014 from q on validation without tools or training, which would **fail** the non-inferiority bar (observed gap must be ≲ +0.006–0.009); recalibration or averaging must close it. Structure metrics (direction, headroom, spread, calibration) have far more power than raw Brier | Matching can be done by copying q, so it must be paired with something q lacks (a blend gain, placeholder handling) to justify training money |
| D3-2. Beat q outright | No published system we found, fine-tuned or not, beats a current liquid price alone. Fine-tuned: Turtel 0.190 vs 0.151; Foresight-32B 0.199 vs 0.170 ([vendor blog](https://blog.lightningrod.ai/p/foresight-32b-beats-frontier-llms-on-live-polymarket-predictions)). Not fine-tuned: AIA alone 0.126 vs 0.111; shown the price, GPT-5 ties it (0.184 vs 0.187, [Prophet Arena](https://arxiv.org/html/2510.17638v2)). Systems beat only *stale* shown prices. Our tools carry no news, and a facts-only fit ties q (eval difference −0.0002, CI ±0.009) | A model that departs from q wholesale cannot show a gain < ≈ 0.01 on the holdout |
| Stretch: add information beyond q (report only) | Published gains over the market come from **blends** ([Halawi](https://arxiv.org/html/2402.18563): 0.146 vs crowd 0.149; AIA blend 0.106 vs 0.111). A 0.25·BD + 0.75·q blend already scores 0.003 below q on validation (SE 0.0035, not significant) | Free to compute; small departures from q *can* be shown significant (holdout half-width ≈ 0.0023 for the blend) |

**Recommendation: D3-1, with D3-0 as the honest baseline every trained model is compared against.** Before spending anything, run one free check: a small logistic with path features the facts-only fit never tried (realised volatility, move speed, time to close, flow), fitted on train and tested on eval. If it does not beat q, the extra information is probably not in the tools.

---

## 7. Recommended approach

**Is the current plan right?** Right in shape, wrong in budget and in three specifics:
- It assumes ≈ $80; ≈ $70 is left, and the full plan with RL leaves nothing for contingency.
- P4's data source is (on our reading) not permitted and must become self-training.
- It skips three free or cheap rungs (recalibration, sample averaging, few-shot) that attack the same remaining error SFT would.

### The ladder (≈ $70 available; choose a rung)

| # | Step | Cost | Gate to continue | Stop rule / fallback |
|---|---|---|---|---|
| 0 | **Free prep:** seed the spend ledger with $64.41; record D1/D2/D3 (`REVIEW:`); build the `Frame` wrapper and exit test; implement D2 and re-score old evals with the **tightened** matcher; retry policy + fallback parser; recalibration, blend and path-feature checks; confirm Qwen3-8B is still served on Tinker (listed, not deprecated, as of 2026-10-14); draft the P6 pre-registration | $0 | Exit tests green; ledger shows ≈ $80 spent | If the path-feature logistic beats q on eval, revisit D3 |
| 1 | **P3:** BD with tools, thinking off and on, averaging, few-shot and spread arms | ≈ $6 | Brier ≤ 0.25, dir corr > 0, down corr(p, q) ≥ 0.4, malformed ≤ 10% / 15%, no frame mismatches | If BD fails with tools: stop, analyse transcripts (free) |
| A | **Gate A** | $0 | Best prompt-only arm (+ recalibration) within ≈ +0.006 of q on validation → **rung 1 is enough; go to P6** | Thinking-on malformed > 15% after the parser fix → thinking off; only P4b (scripted q) is possible, and no RL |
| 2 | **P4-self** (rung 2) | ≈ $13–17 | P4 gates (§5) | Too few kept traces → stop and report |
| B | **Gate B** | $0 | SFT model meets the D3 bar on validation → P6 | Spread probe fails → no RL |
| 3 | **P5** (rung 3) | ≈ $27–35 | Checkpoint-keep rule (§5) | Any stop rule → carry the SFT model to P6 |
| 4 | **P6:** pre-registered, one-shot holdout | $11 per model single-sample; $33 at k = 3 | Non-inferiority (95% upper bound ≤ +0.015) plus structure metrics | Any re-run after looking is labelled exploratory |

**Totals (thinking-on estimates ± 30%; re-check after P3).**
- **Rung 1, prompt + recalibration + averaging:** 0 + 6 + one model on the holdout ($11 single-sample, $33 at k = 3) ≈ **$17–39**, leaving $31–53.
- **Rung 2, + self-training SFT:** 6 + 15 + 11 (SFT model) ≈ **$32**; grading the prompt-only arm too ≈ **$43**.
- **Rung 3, + RL:** 6 + 15 + 27–35 + 11 (one model only) ≈ **$59–67**, leaving $3–11. Fits only if P6 grades a single model and every cost lands low.

**Recommendation:** do step 0 and P3 (≈ $6), then decide. Take rung 2 only if the owner values the fine-tuning experience or P3 shows a gap recalibration cannot close. Do not plan on rung 3. Banking the rest is a legitimate outcome.

**What success looks like.** A model that, on the untouched holdout, is non-inferior to q (95% bound within 0.015 Brier), reads direction correctly (direction corr > 0, down answers near the 0.7–0.77 hold rate rather than near the price), is at least as well calibrated as q, keeps answer spread (sd ≥ 0.12), rarely malforms, **and** adds something q cannot (a significant blend gain or better launch-placeholder handling). Without that last part, D3-0 (ship q) is the better product.

---

## 8. Where to look (ranked for you)

1. **Thinking Machines, [On-Policy Distillation](https://thinkingmachines.ai/blog/on-policy-distillation/)**: SFT, RL and distillation as one family, and why RL learns so little per episode. Written for Tinker users.
2. **Thinking Machines, [LoRA Without Regret](https://thinkingmachines.ai/blog/lora/)**: why rank isn't our problem, the 10× LR rule, and the "O(1) bits per episode" argument.
3. **[Turtel et al., Outcome-based RL to Predict the Future](https://arxiv.org/html/2505.17989v4)**: the closest precedent (Polymarket, Brier reward, real hyperparameters), and a market the model does not beat.
4. **[Qwen3 Technical Report](https://arxiv.org/html/2505.09388)**, §4 and Table 21: how *our* base model was made; distillation vs RL at 8B.
5. **[DeepSeek-R1, v1](https://arxiv.org/html/2501.12948v1)**: the canonical cold-start-then-RL pipeline, format rewards, distillation beating RL on small models.
6. **[Halawi et al., Approaching Human-Level Forecasting](https://arxiv.org/html/2402.18563)**: forecasting fine-tuning on filtered samples, the overconfidence warning, and blending with the crowd.
7. **[Paleka et al., Pitfalls in Evaluating LM Forecasters](https://arxiv.org/html/2506.00723v1)**: why "matches the market" is circular when the price is shown, and correlated-period caveats.
8. **[OpenAI RFT guide](https://developers.openai.com/api/docs/guides/reinforcement-fine-tuning)**: a short checklist for when RL can and cannot work ("guess-proof" tasks, "some success" required).
9. **Tinker Cookbook `recipes/forecasting/`** (installed in `.venv/lib/python3.12/site-packages/tinker_cookbook/`) and the [RL hyperparameters page](https://tinker-docs.thinkingmachines.ai/tutorials/advanced/rl-hyperparams/): a working Brier-RL config on our platform.
10. **[OpenForecaster](https://arxiv.org/html/2512.25070v2)**: Qwen3-8B, thinking on, GRPO for forecasting, with an SFT-vs-no-SFT ablation.
11. **[Prophet Arena](https://arxiv.org/html/2510.17638v2)**: what frontier LLMs do when the market price is in the prompt.
12. **[Anthropic Usage Policy](https://www.anthropic.com/legal/aup)**: read before using any Claude output as training data.
13. **[Tülu 3](https://arxiv.org/html/2411.15124v3)**: the most transparent open account of eval-first development, decontamination and RLVR.
14. **[Yue et al.](https://arxiv.org/abs/2504.13837)** with **[ProRL](https://arxiv.org/abs/2505.24864)** as the counterpoint: does RL sharpen or expand?
15. **Nathan Lambert, [RLHF book](https://rlhfbook.com/)** (policy-gradients and rejection-sampling chapters) and his [elicitation post](https://www.interconnects.ai/p/elicitation-theory-of-post-training): the textbook when you need depth.

---

## 9. Glossary

| Term | Meaning |
|---|---|
| Advantage | A sample's reward minus a baseline (here the group mean), optionally divided by a spread; the weight on its log-likelihood in the update |
| AIME'24 | A 2024 maths-competition benchmark used in reasoning-model reports |
| Anchoring | Answering close to a number shown in the prompt |
| Arm / framing A, B, B′, D, E | Prompt variants from the Phase 4 probe. A: current wording. B: drops restated as rises of the other outcome. B′: the reverse (rises restated as drops). D: q stated in a sentence. E: the "prices are probabilities of the first outcome" sentence deleted |
| `batch_std` advantages | Reward minus the group mean, divided by the standard deviation over the whole batch |
| BD | B plus D; our recommended framing |
| Brier score | Mean (p − y)²; lower is better; a constant 0.5 scores 0.25. The repo's reward is 1 − Brier − penalty |
| Calibration / ECE | Events forecast at p happen a fraction p of the time; ECE is the binned average gap |
| Checkpoint | Saved model weights at a training step |
| Clip-higher, DAPO, GSPO, Dr. GRPO | GRPO variants. The first three limit or loosen how far one batch may move the policy when it is reused for several updates; Dr. GRPO removes per-sequence length division |
| Cold start / warm start | A short SFT stage before RL |
| Decontamination | Making sure no training item overlaps evaluation items (here, by market id) |
| Degenerate group | A group whose samples all get the same reward, so it contributes no gradient |
| Direction corr | Correlation of p with the move being a drop; positive is right for spike tasks |
| Distillation (off-/on-policy) | Training a student on a stronger teacher's outputs; on-policy needs the teacher's logprobs on the student's samples |
| DPO | Direct Preference Optimization: training on better/worse answer pairs |
| Encompassing test | Regress y on both forecasts (here logit q and logit p); if p's coefficient is significant, p adds information q lacks |
| Entropy (bonus) | Spread of the model's next-token choices; a bonus rewards keeping it high |
| Entropy / answer collapse | Samples becoming nearly identical (run 1: 0.65 everywhere), after which RL has nothing to compare |
| Frame-mismatch flag | An episode where the price in the prompt and the price the tools report are in different frames |
| GRPO | Group Relative Policy Optimization: sample a group per prompt, use the group mean as the baseline |
| Headroom | How far the price can still move in the move's direction (1 − price_now after a rise, price_now after a drop). More headroom should mean a lower hold probability |
| Holdout | Data scored once at the end; here 1,872 frozen tasks (1,334 spike) |
| Hybrid model | One set of weights that can run with thinking on or off (Qwen3) |
| KL penalty | A cost for drifting from a reference model's distribution |
| Launch placeholder | A market's 0.50 opening price before real trading; a "move" away from it is not news, and such moves hold more often |
| Logprobs | The model's log-probability of each token |
| LoRA | Low-rank adapters: train ΔW = BA instead of all weights |
| Malformed episode | An episode with no parseable answer (bad tool call, prose answer, truncation, budget overrun) |
| Masking | Giving an episode or tokens zero weight in the loss |
| MDE | Minimum detectable effect at 80% power, two-sided α = 0.05 |
| Metaculus baseline score | Metaculus's log-score-based points relative to a uniform forecast; higher is better |
| Non-inferiority test | Testing that a model is no worse than a reference by more than a pre-set margin |
| On-policy | Training samples come from the current weights |
| pass@1 / pass@k | Chance one sample is right / that at least one of k samples is right |
| Per-turn splitting | Turning one multi-turn episode into one training example per assistant turn, which repeats the shared context and multiplies training tokens |
| Proper scoring rule | A score whose expectation is best when you report your true belief (Brier, log score) |
| q | Market-implied rule: price_now after an up-move, 1 − price_now after a down-move |
| Recalibration (Platt / isotonic) | A fitted monotone map from raw p to calibrated p: logistic on logit p, or a step function |
| Rejection sampling / RFT / STaR | Sample from the model itself, keep what passes a filter, SFT on the kept samples |
| Renderer | The code that turns a conversation into the model's token format (e.g. with or without thinking) |
| RLVR | RL with verifiable rewards: a program, not a learned model, scores answers |
| SFT | Supervised fine-tuning: cross-entropy on example outputs |
| `strip_thinking_from_history` | Renderer setting: whether earlier turns' `<think>` text is removed from the context of later turns |
| Temperature (T) | Sampling sharpness; higher T gives more varied samples |
| Thinking on/off | Whether Qwen3 writes a `<think>` reasoning block before answering |
| Tinker | Thinking Machines' fine-tuning API; we train LoRA adapters on it |
| Tokenizer | The code that splits text into the model's tokens; teacher and student must share it for on-policy distillation |
| Top-p / top-k | Sample only from the smallest token set whose probability sums to p / from the k most likely tokens |
| Usage Policy (AUP) | Anthropic's rules for using its models; our reading is that it bars training models on Claude outputs without authorisation |

---

## 10. Review notes

All 32 critic items were checked against the repo and applied, with these qualifications:
- **A1–A3** confirmed: ledger sum $15.61 since the pilot, $64.41 before (`DECISIONS.md:696,737`), run 1 measured $41.97 (`RESULTS_M15.md:20`).
- **A6:** episode count recomputed as "under ≈ 3,500" (sampling cost alone at 1.36 × $1.85 / 128 per episode; training tokens lower it further) rather than the critic's 3,500–4,000. The OpenForecaster comparison was deleted.
- **B13:** chose masking from the baseline as the default (it matches the original plan and the 10-step horizon), with the critic's −0.10 floor as the alternative.
- **F30:** applied, with the caveat that the 0.035 variance was measured at T = 1.0; at the eval temperature 0.6 it will be smaller, so the arm measures the gain rather than assuming it.
- **F32:** the ladder's rung-1 cost is $17–39, not the critic's ≈ $10, because grading even one model on the holdout costs ≈ $11 single-sample and k = 3 averaging triples it.
- Rejected: none outright. "Reverse KL" (E27) does not appear in this guide, so it was not added to the glossary.
