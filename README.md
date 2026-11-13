# kyb-review-agent

An AI agent that reviews business customers before they are onboarded to a payments platform
(KYB, "know your business"), and the platform around it that decides how much the agent is
trusted to do.

The agent reads the application and its documents, checks the company registry, screens every
director and owner against sanctions lists, and recommends **approve**, **request info** or
**escalate** with its evidence. The model is Claude Haiku 5.5. The interesting part is not the
agent. It is everything that keeps a wrong answer from becoming a wrong action:

- **Trust stages.** The agent starts in **shadow**, earns **suggest**, then **execute**, then
  **autonomous** ([`policy/stages.yaml`](policy/stages.yaml)). Each stage grants different tools,
  and the agent moves up only when its eval results pass that stage's gate.
- **Agent identity and scoped credentials.** For each case the platform issues the agent a signed,
  ten-minute token naming its identity, stage, tool scope and the one case it may touch. The tools
  sit behind an **MCP server** that holds only the public key and checks the token on every call.
- **Rules in code, not in the prompt.** After the model submits, the harness applies hard rules.
  A sanctions hit, a dissolved company or an altered document always goes to a human, whatever the
  model decided, and every override is logged.
- **Documents are untrusted data.** Everything an applicant supplies reaches the model labelled as
  untrusted, and any attempt to instruct the reviewer is itself a reason to escalate.
- **Evals as a release gate**, on a held-out set written before any tuning.
- **Audit, tracing and spend.** A hash-chained audit log, an OpenTelemetry trace per case that
  spans the harness and the tool server, a per-case budget and a hard cap per eval run.
- **An Operator dashboard** with the queue, the agent's call and evidence, the trace, and a place
  to record the Operator's own decision.

![Operator queue](docs/queue.png)

## How it fits together

```
                     ┌──────────────────────────────────────┐
                     │  policy/stages.yaml                  │
                     │  stages and their tools, gates,      │
                     │  flags, hard rules, budgets          │
                     └──────────────────┬───────────────────┘
                                        │
                                        ▼
 ┌──────────────┐  case   ┌──────────────────────────────────┐  signed token    ┌──────────────────────────────┐
 │ application  │────────▶│  HARNESS  (kyb/agent.py)         │────────────────▶ │  MCP TOOL SERVER             │
 │ + documents  │         │                                  │                  │  (kyb/mcp_server.py)         │
 │ (untrusted)  │         │  1. issues the agent's token     │   tool call      │  one process per session     │
 └──────────────┘         │  2. runs the model loop          │  + trace context │                              │
                          │  3. budget: calls and dollars    │ ───────────────▶ │  checks the token each call: │
                          │  4. wraps results as untrusted   │                  │  signature, expiry, scope,   │
                          │  5. enforce(): hard rules in     │ ◀─────────────── │  case binding                │
                          │     code, may overrule the model │  result or       │  runs registry, sanctions,   │
                          └──────┬────────────────┬──────────┘  refusal         │  documents                   │
                                 │                │                             │  execute-stage actions only  │
                                 ▼                │                             │  queue an Operator approval  │
                          ┌──────────────┐        │                             └──────────────┬───────────────┘
                          │ model        │        │ writes                                     │ writes
                          │ (OpenRouter) │        ▼                                            ▼
                          └──────────────┘  ┌───────────────────────────────────────────────────────────┐
                                            │  results/: audit.jsonl (hash chain), traces.jsonl (OTel), │
                                            │  eval-*.json, approvals.jsonl                             │
                                            └─────────────────────────────┬─────────────────────────────┘
                                                                          ▼
                                                          ┌──────────────────────────────┐
                                                          │  OPERATOR DASHBOARD          │
                                                          │  (kyb/dashboard.py)          │
                                                          └──────────────────────────────┘
```

## What happens to one application

1. The platform looks up the agent's stage, issues a token for that stage's tools and this case,
   and starts an MCP tool server for the session with that token. The model never sees the token.
2. The server lists only the tools in the token's scope. In shadow and suggest the agent can read
   and check, and the approve tool does not exist for it.
3. The agent lists and reads the documents, looks the company up in the registry (and searches by
   name if the number fails), compares directors and owners, and screens each person for sanctions.
   A name match counts only if date of birth and nationality also match.
4. Every tool call carries the trace context, and the server re-checks the token: signature,
   expiry, scope and case. Refusals come back to the model as errors and go to the audit log.
5. Results reach the model inside `<untrusted>` tags. A per-case budget caps tool calls and spend.
6. The agent submits a decision, flags from a fixed list, a risk level, reasons and evidence.
7. `enforce()` drops unknown flags and applies the hard rules: a blocking flag, or a brand-new company
   with implausible volume, always becomes escalate. A failed model call sends the case to a human.
8. What happens next depends on the stage:

| Stage | Tools | What the verdict does | Gate to move up |
|---|---|---|---|
| **shadow** | read, registry, sanctions | logged and scored, the Operator never sees it | held-out set: ≥ 80% accuracy, 0 unsafe approvals, every escalation caught |
| **suggest** | same | shown to the Operator as a recommendation | ≥ 100 live Operator decisions at ≥ 92% agreement, 0 unsafe |
| **execute** | + request documents, approve | actions queued for one Operator click, escalations straight to a human | ≥ 500 live at ≥ 97%, 0 unsafe |
| **autonomous** | same | approves alone only with no flags and volume under the cap | — |

## Tracing

Each case is one OpenTelemetry trace. The harness opens a `kyb.review` span. Every model call is an
`llm.chat` span with the standard `gen_ai.*` attributes (model, input and output tokens) plus its cost.
Every tool call is an `mcp.call` span, and the W3C `traceparent` travels inside the MCP request's
`_meta`, so the tool server, a separate process, continues the same trace. Its `mcp.server` span
records the authorization decision. The last span, `harness.enforce`, shows what the model said and
what the rules made of it.

Spans are written to `results/traces.jsonl`, which the dashboard draws as a timeline. Set
`OTEL_EXPORTER_OTLP_ENDPOINT` (with `opentelemetry-exporter-otlp` installed) and the same spans also
go to any OTLP backend, such as Grafana Tempo, Jaeger, Honeycomb or Langfuse.

![A case with its trace](docs/case.png)

## The cases

43 fictional applications ([`data/build_fixtures.py`](data/build_fixtures.py)), with registry records
shaped like the UK Companies House API and a small fictional sanctions list.

- **Dev set, 31 cases**, used while building: near misses (a director who shares a sanctioned
  person's name but not their birth date, "Ltd" vs "Limited"), hidden problems (an owner who appears
  only in the shareholder register, a director who resigned, crypto custody described without the
  word crypto, a certificate whose company number contradicts the registry, a sanctioned owner behind
  a holding company with his name in a different order) and three prompt injections.
- **Held-out set, 12 cases**, written and committed before any change made in response to the dev
  results, and never used for tuning. The gate to suggest is measured on these only.

## Results (Claude Haiku 5.5, temperature 0)

**Held-out set, 3 runs:** 12 / 12 in every run. 0 unsafe approvals, every escalation caught, no
harness overrides needed. About $0.023 per run. The held-out gate passes, so the agent earns
**suggest**: its recommendations may now be shown to an Operator, who still decides every case.

The held-out set scored 12 / 12 before the changes below as well, so it says the agent generalises
to new cases. It does not show that the changes helped. That evidence comes from the dev set.

**Dev set, 3 runs:** 31, 27 and 30 out of 31. In all three runs: 0 unsafe approvals and every case
that needed a human reached one. The model alone caught 10 of 11 escalations in each run; the code
rules caught the 11th. Every miss was in the cautious direction: asking for more information on a
clean company once, or escalating a case where the documents disagree for an innocent reason (a
trading name, a typo in a company number). About $0.06 per run.

## What building it taught

- **The gaps were in the policy, not the model.** The model noticed an altered certificate and asked
  for more information, because nothing said an altered document is a fraud signal. It also raised
  every right flag on a three-week-old company with a virtual office expecting EUR 5M a month, then
  asked for information, because no single flag was blocking. Both fixes went into code
  (`escalate_with`, `escalate_when_all`), not the prompt. One definition did go into the policy text:
  a gambling licence "in progress" is not a licence.
- **The trade is deliberate.** The rules turn a few cases the model would have handled more gently
  into escalations. In KYB a human spending two minutes is cheap; a forged document getting through
  is not.
- **The labels were wrong three times, and the model was right.** A payment facilitator, an
  "international payment solutions" company and an online casino all need a licence under this repo's
  own policy. The early runs flagged them and the labels were fixed.
- **One run is not enough.** At temperature 0 the dev score still moved between 27 and 31. The safety
  numbers did not move, which is what the gate is built on.
- **Fail closed.** An API error mid-case once crashed a whole run. Now that one case goes to a human,
  the error is logged and the run continues.

Everything run while building this cost about $0.80 in model calls.

## Run it

```bash
pip install -r requirements.txt
python data/build_fixtures.py                          # registry, sanctions list, dev and held-out cases
python -m pytest -q                                    # offline, no API key
export OPENROUTER_API_KEY=...
python -m kyb.evaluate --set heldout --label mine --max-usd 0.10
python -m kyb.dashboard                                # http://127.0.0.1:8090
```

`--max-usd` is a hard cap for the whole run. The offline tests start the real MCP server and check
that out-of-scope calls, expired tokens and forged tokens are refused and that execute-stage approvals
are only queued. They also pin the escalation rules, untrusted wrapping, sanctions matching and the
audit chain.

## Layout

```
policy/stages.yaml   trust stages, gates, flags, rules, budgets
kyb/agent.py         the harness: token, MCP session, model loop, budget, enforce, tracing
kyb/mcp_server.py    the MCP tool server: token checks, scoped tools, approval queue
kyb/identity.py      Ed25519-signed agent tokens
kyb/tools.py         registry, sanctions and document tools over the fixtures
kyb/telemetry.py     OpenTelemetry setup and trace-context propagation
kyb/audit.py         hash-chained audit log
kyb/evaluate.py      eval runner, scoring, stage gate
kyb/dashboard.py     Operator dashboard (stdlib only)
data/                fixtures, all fictional
results/             eval results, audit log and traces from the runs above
```

## Limits

Everything is fictional and offline: the registry, sanctions list and documents are fixtures, not
live data sources, and the sanctions matching is deliberately simple. Live Operator agreement, which
gates suggest → execute, is recorded by the dashboard but has no real Operators behind it here. The
signing key is generated locally; in production it would live in a secret manager or KMS.
