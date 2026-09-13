# Offline Slack quality evaluation

Run without a provider, Slack connection, or live Hermes configuration:

```sh
python3 evals/slack_quality/evaluate.py evals/slack_quality/baseline.json
scripts/run_tests.sh tests/gateway/test_slack_quality_evaluation.py
```

The sanitized fixture retains measured timestamps and separately attributed human review findings, not raw message bodies, private keys, tokens, or internal deliberation. Four request boundaries cover six review cases: initial response, wrong workdir, historical retrieval, Delta capability inference, account identity, and the later exhausted-continuation failure. The initial request was expanded; it must not be scored as a simple host query.

Accepted **offline targets**: visible receipt within 5s, simple local outcome within 60s, bounded fleet outcome within 180s, factual progress at intervals no greater than 60s. These are not live SLAs, forced termination timers, or new retry limits. An explicit failure can meet an outcome timing target without being a successful answer; `outcome_kind` preserves that distinction.

Evidence rules:

- Timestamps are finite decimal strings. Events from another request never contribute to the requested result.
- Only successful Slack delivery observations count as visible output. Database assistant text and unsuccessful sends do not.
- `visibility_complete: true` asserts a reviewed complete visible-event trace. Without it, progress cadence and single-outcome success cannot be established. Selected observations can establish first-receipt/outcome timing only with separately reviewed `first_visible_verified`/`outcome_verified` flags. Duplicate observed outcomes still prove duplication.
- Provider durations require one start/end pair with the same request, call ID, provider and model. Matched pairs never imply every call was captured. Session-level model metadata supplies no attribution.
- These provenance/coverage assertions are supplied by the capture reviewer; this tool does not authenticate evidence or judge natural-language truth. `semantic_quality` remains `REQUIRES_REVIEW`, and `live_verified` remains false.

The CLI prints a report and exits successfully when evaluation executes; individual target failures/unknowns are in the report. It does not claim the fixture passed as a whole. Invalid evidence shape/timestamps fail execution.

Current baseline proves a 346.558640s first visible response and a 893.524990s observed incomplete outcome. Its progress cadence and per-call provider attribution remain unknown. Native conversation-loop source already emits per-call model/provider/latency log lines; those alone do not prove request correlation or explain the first-tool delay. No new runtime telemetry or behavior is installed by this evaluator.
