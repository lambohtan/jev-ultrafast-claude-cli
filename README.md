<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!NOTE]
> **This is a fork of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast).**
> One change: the small LLM that writes text can be your local `claude` CLI instead of a second paid
> API key. Everything else is upstream. → [Why this fork](#why-this-fork)

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) picks an operation and an element. A small LLM writes text only when the operation is `TYPE_TEXT`.

**Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual text generation, and loading waits included.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## Why this fork

Upstream needs two credentials, and they are not equally easy to come by:

| Credential | What it buys | Substitute |
| --- | --- | --- |
| `TYPESAFE_API_KEY` | Jev picks the operation and the element — the entire policy | None. This fork does not touch it. |
| `TEXT_MODEL_API_KEY` | A small LLM writes the field value, and only when the operation is `TYPE_TEXT` | **This fork.** |

The second one does a narrow job. When the agent decides `TYPE_TEXT [3]`, something has to turn the
goal into the string `Zürich`. Upstream routes that through an OpenAI-compatible endpoint — an
OpenRouter key in the example config. So running the demo meant opening a second paid account to
generate a few short strings per run.

If you already have Claude Code, you already have a model that can write those strings.

So this fork adds **[`jev_ultrafast/claude_shim.py`](jev_ultrafast/claude_shim.py)**: an
OpenAI-compatible `/chat/completions` server on `127.0.0.1`, backed by the local `claude` CLI. It
speaks only the slice of the API that jev-ultrafast actually uses — `model`, one system message, one
user message, and a JSON-object reply — so the agent loop cannot tell the difference. Sampling,
reasoning, and streaming parameters are ignored.

The helper is a dependency of the agent, not a tool you operate beside it, so the package owns its
lifecycle. Against upstream that is one new module, one call in
[`agent.py`](jev_ultrafast/agent.py), and one console script in `pyproject.toml` — small enough that
pulling upstream stays clean.

### Run it

Install, put your keys in `.env`, and point the text helper at the shim:

```bash
uv sync
cp .env.example .env
```

```bash
TYPESAFE_API_KEY=...       # still required: this fork replaces the text helper, not the policy
TEXT_MODEL_BASE_URL=http://127.0.0.1:8899/v1
TEXT_MODEL=haiku           # a name the claude CLI accepts, not an OpenRouter slug
TEXT_MODEL_API_KEY=unused  # never sent anywhere, but it must be set
```

Both comments matter. `TEXT_MODEL` is forwarded straight to `claude --model`, so an OpenRouter-style
slug like `inception/mercury-2.5` will fail. And `TEXT_MODEL_API_KEY` has to be non-empty because
[`model.py`](jev_ultrafast/model.py) refuses `TYPE_TEXT` without it — that guard exists so no field
text is ever silently guessed — but the shim ignores the value.

That is the whole setup. Nothing needs starting by hand:

```bash
uv run --env-file .env python examples/run.py \
    --url https://duckduckgo.com/ --goal 'Search for "jev ultrafast"'
```

```text
text helper not running on 127.0.0.1:8899 — starting it (first boot takes ~20s)...
text helper ready (it exits on its own after idling)
```

Every `Agent` calls `claude_shim.ensure()` before it opens the browser. If `TEXT_MODEL_BASE_URL` is
a local port with nothing listening, the shim is started there and waited for — so the ~20s CLI boot
lands *before* the run's clock starts, instead of inside its first `TYPE_TEXT`. Later runs find it
already warm and skip the wait entirely. A remote `TEXT_MODEL_BASE_URL` — OpenRouter, DeepSeek — is
left completely alone, and `JEV_SHIM_AUTOSTART=0` turns the behaviour off.

### Drive it by hand

The server exits on its own after 15 idle minutes, so it never needs stopping. When you do want to
look at it, the package installs a `jev-shim` command:

```bash
uv run jev-shim status   # up or down
uv run jev-shim stop     # stop it now
uv run jev-shim start    # run it in the foreground, with request logs on stderr
```

(Drop the `uv run` if the venv is already activated.) A foreground `start` is the one to reach for
when a run reports the helper never came up: it prints the CLI's own errors instead of sending them
to `$TMPDIR/claude-cli-shim.log`, which is where the autostarted server logs.

Knobs: `CLAUDE_SHIM_PORT` (8899), `CLAUDE_SHIM_MODEL` (`haiku`), `CLAUDE_SHIM_MAX_TURNS` (8),
`CLAUDE_SHIM_TIMEOUT` (90s), `CLAUDE_SHIM_IDLE` (900s), `CLAUDE_SHIM_BOOT` (120s).

### What it costs

- **The CLI boots in roughly 20 seconds.** The shim pays that once at startup by pre-warming a
  session, so a run's `TYPE_TEXT` requests don't each wait for it — and because it binds its port
  only after the warm-up, an open port always means a ready helper.
- **It is a CLI, not a low-latency API model.** The 7.1-second Flights run above assumes the API
  text helper; expect a slower number here. The decision loop is unchanged — the added time is all
  in text generation.
- **Sessions are recycled.** One warm `claude` session serves requests and is replaced every
  `MAX_TURNS` turns, and every request is prefixed with an instruction to ignore earlier ones, so
  one run's text does not bleed into the next.
- **`TYPESAFE_API_KEY` is still required.** This fork replaces the text helper, not the policy.

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_UP`, `SCROLL_DOWN`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered.

```text
                      one TypeSafe request
                     ┌───────────────────────────┐
page → element table → operation                 │
                     │ click_target              │
                     │ type_text_target          │
                     │ select_target, if present │
                     └─────────────┬─────────────┘
                         use the matching target
                                   │
                    CLICK [7] ─────┤──→ browser
                TYPE_TEXT [3] ─────┘
                          ↓
                   small LLM → text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or prepared field strings in the policy. The Flights example supplies a goal and independently verifies the outcome. The screenshot renderer adds labels afterward; it does not drive the browser.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Use the library

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One request per decision cycle.** Operation and target heads share the same observed state.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read visible controls, their names, values, and text atomically. Keep references to the actual DOM nodes.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Reuse an interrupted text request.** A generated value survives a stale-page retry only if the entire text-helper input is unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Small enough to read

| File | Job |
| --- | --- |
| [agent.py](jev_ultrafast/agent.py) | The complete loop and text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

The current video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The same policy opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Shadow roots, frames, canvas, uploads, pop-up tabs, nested scrolling, and arbitrary keyboard widgets remain outside this MVP. Owned tabs share the existing Chrome profile.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

Tests are offline. `uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
