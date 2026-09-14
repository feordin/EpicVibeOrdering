# Running downtime extraction on a fully local model

The downtime subsystem exists for the hour when Epic is down. If that hour is a
network event rather than an application event, a downtime tool that calls a
hosted API is a downtime tool that does not work. This page covers the third
provider - `ollama` - which runs the extraction model on the downtime box
itself, and the eval harness that tells you whether a given local model is good
enough to put in front of a clinician.

There are three providers, and they trade off differently:

| Provider | Needs | Transcript leaves the box | Typical latency |
|---|---|---|---|
| `fake` | nothing | no | milliseconds |
| `ollama` | Ollama + a pulled model | no | seconds to minutes |
| `anthropic` | API key + internet + the PHI gate | **yes** | ~seconds |

`fake` is a deterministic keyword/regex extractor. It is the floor, not the
goal: it gets all six fixture transcripts to the right template and captures
every stated demographic, but it cannot read a sentence it was not written for.
`ollama` is the interesting middle: no network, no PHI leaving the building, and
real language understanding - at the cost of latency and of whatever accuracy
the model you can fit on the box happens to have.

## PHI: why Ollama is not behind the `allow_phi_to_model` gate

`EPICVIBE_DOWNTIME_ALLOW_PHI_TO_MODEL` gates **`provider=anthropic` only**. A
downtime transcript is ambient clinical audio: patient name, DOB, chief
complaint. Sending it to a hosted model is a per-deployment decision that a
human has to make on the record, so `build_provider()` refuses to construct the
hosted provider until someone sets that flag.

Ollama is different in kind, not in degree. The weights run on the machine, the
request goes to `localhost`, and nothing crosses the trust boundary - so there
is nothing for the flag to authorise, and `provider=ollama` starts with the gate
still `false`.

The privacy question for Ollama is a different one: **where is
`EPICVIBE_DOWNTIME_OLLAMA_BASE_URL` pointing?** The default is
`http://localhost:11434`. If you point it at another host you have re-opened the
question yourself, and you should point it only at a machine inside the same
trust boundary as the downtime box. Nothing in the code can tell the difference
between a loopback address and a hostname that resolves to a vendor's cloud.

## Setup

Do all of this **before** the downtime, while the network is up. A model that is
not already pulled is a model you do not have.

1. **Install Ollama** - <https://ollama.com/download>. It installs a daemon that
   listens on `127.0.0.1:11434` and starts with the machine.

2. **Pull a model, once.** The tag you pull is the tag you configure.

   ```powershell
   ollama pull qwen2.5:7b
   ollama list          # confirm the tag and the on-disk size
   ```

3. **Point the downtime subsystem at it.** In `.env`:

   ```ini
   EPICVIBE_DOWNTIME_PROVIDER=ollama
   EPICVIBE_DOWNTIME_OLLAMA_MODEL=qwen2.5:7b
   EPICVIBE_DOWNTIME_OLLAMA_BASE_URL=http://localhost:11434
   EPICVIBE_DOWNTIME_OLLAMA_TIMEOUT_SECONDS=300
   EPICVIBE_DOWNTIME_OLLAMA_NUM_CTX=16384
   EPICVIBE_DOWNTIME_OLLAMA_KEEP_ALIVE=10m
   EPICVIBE_DOWNTIME_OLLAMA_THINK=false
   ```

4. **Verify with the network unplugged.** Physically, not by trusting a config
   file. Then run the eval below; it exercises the same two-step engine the
   capture UI uses.

### How the provider talks to Ollama

`POST {base_url}/api/chat` with `stream: false`, a system and a user message,
and `format` set to the JSON schema for the step's pydantic model. Ollama
compiles that schema into a llama.cpp GBNF grammar and constrains decoding to
it, which is the local equivalent of the hosted provider's forced `emit` tool.

One wrinkle worth knowing: the schema the engine hands the provider is the
*strict* Anthropic dialect (`schema.strict_json_schema()`), and Ollama cannot
lower two of its constructs - `additionalProperties: false`, which has no
grammar meaning, and a nullable union written as a list of types
(`"type": ["string", "null"]`). `providers.relax_json_schema()` walks the schema
back to plain JSON Schema before it goes out: list-typed nodes become `anyOf`,
`additionalProperties` is dropped. `required` is deliberately left as
strictification set it - every property - because a grammar that forces every
key out of a small model yields far more filled fields than one that lets it
answer `{}`.

If the model still returns something unparseable (prose around the object, a
markdown fence), the provider unwraps what it can and otherwise retries once
with a "return only JSON" nudge before raising `OllamaProviderError`. Connection
failures and timeouts are mapped to the same error type with a message that
names the URL and the env var to change.

## The eval harness

```powershell
# the deterministic floor - fast, no model needed
.venv\Scripts\python -m epicvibe.downtime.eval --provider fake

# a local model, full report to JSON
.venv\Scripts\python -m epicvibe.downtime.eval --provider ollama --model qwen2.5:7b --json eval.json

# one transcript, for iterating
.venv\Scripts\python -m epicvibe.downtime.eval --provider ollama --only sepsis-bundle
```

It runs the real two-step engine - template selection, then template fill - over
all six transcripts in `fixtures/downtime/transcripts/` and scores each against
a hand-written expectation in `fixtures/downtime/expected/`. The expectations
were derived by reading each transcript against its template and recording only
what is unambiguous; where the transcript is genuinely equivocal the item is
left out of both lists rather than guessed at. (Example: in `sepsis-bundle` the
clinician asks for norepinephrine at the bedside but conditions starting it on
the post-bolus MAP, so norepinephrine is in neither `selected_orders` nor
`deselected_orders`.)

| Column | What it means |
|---|---|
| `tmpl` | Did we pick the right order set? Binary, and it gates everything downstream. |
| `fields` | Of the demographics the clinician said out loud, how many did we capture? There is no ADT feed during a downtime, so a miss here is re-keyed by hand. |
| `evid` | Of the values we filled, how many carry a quote that really appears in the transcript. Values that came from the template's own `defaults` have nothing to quote, so this is a floor, not a target. |
| `halluc` | Filled values whose quote is **not** in the transcript. This is the patient-safety number. An invented quote is worse than a blank field because it looks checkable and is not. |
| `ord R/P` | Recall and precision of selected orders against the expectation. Precision is scored against what the clinician asked for, so an unmentioned template default counts against it; the harness separately reports `invented` - orders selected that are neither expected nor a template default. |
| `desel` | Of the orders the clinician explicitly deferred ("hold the carvedilol", "no troponin for now"), how many we left off. The hardest thing for a small model and the most dangerous to get wrong. |

The command exits non-zero when selection accuracy drops below
`--min-selection-accuracy` (default `1.0`) or mean field recall below
`--min-field-recall` (default `0.7`), which is what makes it usable as a gate
when someone swaps the model.

## Measured results

### Hardware these numbers came from

| | |
|---|---|
| CPU/GPU | AMD Radeon 8060S integrated graphics (Strix Halo class, unified memory) |
| Discrete GPU | none - `nvidia-smi` is not installed and there is no NVIDIA device |
| Offload | Ollama reported `100% GPU` for every model below, on the integrated adapter |
| Ollama | 0.33.3, Windows 11 |
| Context | `num_ctx=16384`, `temperature=0`, `think=false` |

Worth being precise about, because it changes how you read the timings: this is
an integrated GPU with unified memory, not a datacentre card and not pure CPU.
A box with only a CPU will be slower than this; a box with a discrete GPU will
be faster. What transfers between machines is the *ranking* of the models and
the shape of their failures, not the seconds.

### Summary

Six transcripts, run end to end through the same two-step engine the capture UI
uses. `sec/tx` is the wall time for both model calls of one transcript.

| Model | Params | Selection | Field recall | Evidence | Invented quotes | Order R / P | Deselection | sec/tx |
|---|---|---|---|---|---|---|---|---|
| `fake` (keyword) | - | **6/6** | 100% | 52% | 0 | 94% / 92% | 4/9 | <0.1 |
| `gemma4:26b` (Q4_K_M) | 25.2B | **6/6** | **95%** | 61% | 1 | **100% / 100%** | **9/9** | 58 |
| `qwen3.5:9b` | 9B | **6/6** | 10% | 1% | 4 | 8% / 33% | 9/9 † | 167 |
| `qwen2.5:7b` | 7B | 1/4 ‡ | 0% | 0% | 0 | 0% / 0% | 2/2 † | 33-950 |

† A model that selects almost no orders scores 100% on deselection for free -
it "correctly" left off the deferred order by leaving off everything. Read that
column only next to order recall.

‡ The 7B run was stopped after four transcripts: two failed with unparseable
output, one timed out at 900s, and the one that completed scored zero. Enough to
settle the question.

### Per-transcript, `gemma4:26b`

```
transcript                    tmpl   fields       evid    halluc   ord R/P      desel   secs
ambulatory-new-t2dm           ok     100% 7/7     68%     1        100%/100%    1/1     48.8
chf-exacerbation-admission    ok     86%  6/7     52%     0        100%/100%    2/2     60.3
dka-management                ok     83%  5/6     73%     0        100%/100%    2/2     66.0
ed-cap-admission              ok     100% 7/7     58%     0        100%/100%    2/2     54.5
ed-chest-pain-acs             ok     100% 5/5     50%     0        100%/100%    1/1     51.3
sepsis-bundle                 ok     100% 6/6     67%     0        100%/100%    1/1     67.3
MEAN (n=6)                    100%   95%          61%     1        100%/100%    100%    58.0
```

It got every order the clinician asked for, selected nothing they did not ask
for, and left off all nine deferred orders - including the two hard ones, "hold
the carvedilol until he's diuresed" and "hold the piperacillin-tazobactam given
the allergy". Its two field misses were `weight_kg` in the CHF transcript and
`encounter_location` in DKA.

The single flagged quote is not really an invention: for `chief_complaint` it
stitched four separate patient lines together with ellipses. Every fragment is
in the transcript; the concatenation is not, so the scorer counts it. Worth
knowing before you read a `halluc` of 1 as a safety event.

### What the smaller models actually did

`qwen3.5:9b` picked the right template all six times and then filled almost
nothing - blank values, no orders selected, occasional quotes of the form
`Transcript: 'weighs 58 kg'.` (a paraphrase wrapped in a label, which is not a
verbatim quote and is scored as invented). It is not that it was wrong; it is
that it declined to answer. It was also three times slower than the 26B model,
because it ran to the grammar's token limit instead of finishing.

`qwen2.5:7b` broke down under the grammar. Its output degenerated into
repetition loops inside the constrained decode - `"template_id":
"sepsis-basketball-basketball-basketball-..."`, `"Sepsis/SEPSISISISEISEISE..."` -
running until the context or the timeout ended it. This is the characteristic
failure of a small model under a strict grammar: the grammar keeps the output
syntactically legal while the model has already lost the thread.

### Reproducing

```powershell
.venv\Scripts\python -m epicvibe.downtime.eval --provider ollama --model gemma4:26b --timeout 900 --json eval.json
```

Expect roughly six minutes for the 26B model on hardware like the above, and
considerably longer for a model that is failing - a degenerate run is *slower*
than a good one, not faster.

## Choosing a model size

The honest summary of the table above is that on this workload, model size
mattered more than anything else we varied, and the useful floor was higher than
expected.

**Size.** The 25B model was the only one that did the job. The 9B and 7B models
both routed correctly - picking a template from six summaries is easy - and both
failed at the step that actually matters, reading a transcript and filling
thirty-odd fields with quotes. Do not infer from "it picked the right template"
that a small model is working; the fill step is where the difficulty is, and the
eval reports them separately for exactly that reason.

If you are sizing a downtime box, size it for a ~25B model at 4-bit
(`gemma4:26b` is 18 GB on disk). If that does not fit, the answer is not a
smaller model - it is the `fake` provider, which captures every stated
demographic deterministically and routes all six transcripts correctly. A
keyword extractor that fills less but never bluffs is a better downtime tool
than a 7B model that emits `"sepsis-basketball-basketball-..."`.

**Turn thinking off.** Under a grammar-constrained `format`, a reasoning model
will happily spend its entire context in `message.thinking` and hand back an
empty `content` with `done_reason: length`. Measured on `gemma4:26b`: 25,946
tokens of reasoning and an empty answer, versus a complete fill in ~60s with
`think: false`. The provider defaults to off and raises a specific error if it
sees the failure anyway.

**Give it context.** The largest fixture template plus a transcript is ~6,800
prompt tokens and the fill it produces is ~3,100. At `num_ctx=8192` that gets
truncated and Ollama returns an empty string, which reads like a model failure
and is not. 16k is the floor; raise it if you add larger templates.

**Latency is a workflow question, not just a number.** A minute per transcript
is fine when the clinician dictates, walks away, and signs the captured orders
afterwards. It is not fine if they are standing at the screen waiting. The
capture UI should be driven from the transcript the clinician already finished,
not blocked on the model - and during a real downtime, a minute of compute
against fifteen minutes of writing on paper is a good trade.

**Re-run the eval when you change the model.** That is what the non-zero exit
code is for. `--min-selection-accuracy` and `--min-field-recall` turn this page
into a gate rather than a memory of one afternoon's measurements.
