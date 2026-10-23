# Prompt customization

`abc-mine` ships prompts that know nothing about any particular codebase.
There are two ways to adapt them, with deliberately different ergonomics.

| | Slots | Overrides |
|---|---|---|
| **What** | Add context to shipped prompts | Replace a shipped prompt entirely |
| **Where** | `<pack>/slots/` | `<pack>/overrides/` |
| **Opt-in needed** | No | **Yes** — `allow_template_overrides: true` |
| **Survives upgrades** | Yes | No — you own the text |
| **Can break a stage** | No | Yes (guarded) |
| **Use when** | Almost always | You must rewrite a stage's brief |

Both are optional. With no pack at all, the generic prompts are used unchanged.

---

## Slots — the normal path

A slot is a markdown file whose content is inserted into the shipped templates
under a fixed heading.

```bash
abc-mine prompts scaffold ./prompts   # create a commented pack
abc-mine prompts slots                # describe every slot
```

```text
prompts/
  slots/
    codebase_overview.md
    build_test_guidance.md
    verification_guidance.md
    screening_notes.md
    ranking_notes.md
    construction_notes.md
    exclusions.md
```

Point your config at it:

```yaml
prompts:
  pack: ./prompts
```

### The vocabulary

| Slot | Used by | For |
|---|---|---|
| `codebase_overview` | screen, rank, build | What the project is: language, layout, domain. The orientation a new contributor would want. |
| `build_test_guidance` | screen, rank, build | Codebase-specific build/test advice: useful targets, how to select one test, pitfalls. |
| `verification_guidance` | screen, build | What a good deterministic check looks like here; which check styles to avoid. |
| `screening_notes` | screen | Commit conventions, subsystems to favour, historical quirks affecting scoring. |
| `ranking_notes` | rank | Which candidates to prefer, desired suite coverage, project tie-breakers. |
| `construction_notes` | build | House style for task statements, verifier conventions. |
| `exclusions` | screen, rank | Paths or change kinds that must never become benchmarks. |

### All-or-nothing rendering

Every slot renders as a **complete titled section that vanishes entirely when
unset** — no heading, no lead-in, no stray whitespace.

This matters more than it sounds. The obvious alternative — interpolating a
sentence fragment mid-paragraph — inevitably reads broken in one state or the
other: awkward when filled, or leaving a dangling clause when empty. A
whole-section slot reads correctly both ways, which means you can fill in two slots
and ignore five without degrading the prompt.

Verify with:

```bash
abc-mine prompts render --config mine.yaml screen
```

### Practicalities

- **Empty means absent.** A whitespace-only or comment-only file is treated as
  unfilled, so scaffolded stubs you never fill in are harmless. Delete them or
  leave them.
- **HTML comments are stripped**, so `<!-- notes to yourself -->` never reaches an
  agent.
- **Unknown filenames are rejected**, not ignored. A typo like
  `codebase_overiew.md` is an error, because silently dropping prompt content you
  believed was active is a worse outcome than a failed load.
- **Editing a slot mid-run is critical drift.** Content digests are recorded at
  `init`; changing them requires `--allow-config-drift`, since commits screened
  before and after would have been judged by different instructions.

### Writing good slot content

- **Be concrete.** "Fortran 2008, CMake + Ninja, sources under `src/truchas/`"
  beats "a modern scientific code."
- **Do not restate the rubric or the toolchain.** Both are injected automatically
  from your config; repeating them risks contradicting them.
- **Put costs in the config, not prose.** `full_build_seconds` (ideally written by
  `preflight`) is rendered consistently into every stage. A number written by hand
  into a slot can drift out of agreement with the machinery.
- **Use `exclusions` for hard rules.** Vendored code, generated files, and
  licence-encumbered areas belong there rather than buried in an overview.

---

## Overrides — advanced, and deliberately gated

Occasionally a slot is not enough and you need to rewrite a stage's brief outright.
That is supported, but it is intentionally hard to do by accident, because a
silently-overridden system prompt is a miserable thing to debug.

**Three independent conditions must hold:**

1. The file lives in `overrides/` — not `slots/`.
2. It is named *exactly* after a base template.
3. The config sets `prompts.allow_template_overrides: true`.

```bash
mkdir -p prompts/overrides
abc-mine prompts show screen_system > prompts/overrides/screen_system.md
# edit it, keeping every @@placeholder@@
```

```yaml
prompts:
  pack: ./prompts
  allow_template_overrides: true
```

Valid filenames — anything else is an error:

```text
screen_system.md   screen_task.md
rank_system.md     rank_task.md
build_system.md    build_task.md
```

### Refused, never ignored

If `overrides/` contains a template and the flag is **not** set, loading **fails**
with an explanation. It does not quietly proceed with stock prompts.

That asymmetry is intentional. Of the three possible behaviours — apply silently,
ignore silently, refuse loudly — the worst is ignoring: you would believe your
prompt was in force, see results that contradict it, and have nothing pointing at
the cause. Refusing costs one config line and removes the ambiguity.

### Placeholders, and what breaks

Overrides receive normal substitution, including slots. Two severities:

- **Required placeholders** carry paths the stage substitutes at run time — where
  the agent must write its output. Dropping one is a **hard error**: the stage
  could not function, and the agent would be told to write nowhere.
- **Advisory placeholders** carry single-sourced content such as the rubric tables.
  Dropping one is a **warning**: you may have deliberately inlined your own, which
  is your call.

The distinction is mechanical breakage versus prompt-quality regression. The first
is never acceptable; the second is your judgement.

### Overrides are loud and recorded

When an override is active, every stage start prints:

```text
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
PROMPT TEMPLATE OVERRIDES ACTIVE — this run does NOT use stock prompts.

  screen_system    overridden  (sha256 32036a855e50e6f5)

Recorded in the run manifest. Re-run without prompts.allow_template_overrides
to restore the shipped templates.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

The SHA-256 of each override is stored in `run.json`, and `status` lists them. A
run using non-stock prompts can never be mistaken for a stock run — which matters
when comparing results across runs months apart.

### Before you override

Ask whether a slot would do. Overrides mean you own that prompt's engineering
forever: improvements to the shipped templates will not reach you, and the
fairness, gate, and output-contract language in the shipped briefs is load-bearing.
The build-stage brief in particular encodes the golden rule on verifier fairness
and the invariance requirements — rewriting it without carrying those forward will
produce unfair benchmarks that still pass every automated check.

If you only need to *add* to a brief, use a slot.

---

## Placeholder syntax

Substitution uses `@@name@@`, and slots `@@slot:name@@`.

Not `{name}`: these prompts are full of JSON braces, and a brace-based syntax
requires escaping gymnastics to avoid mangling injected JSON. (The predecessor
pipeline needed a bespoke single-pass regex substituter for exactly that reason.)

Substitution is a single simultaneous pass, so an injected value that itself
contains `@@something@@` or JSON braces is never re-scanned.

---

## Inspecting the result

```bash
abc-mine prompts slots                            # the vocabulary
abc-mine prompts show build_system                 # a shipped template
abc-mine prompts render --config mine.yaml screen  # the composed prompt
abc-mine prompts render --config mine.yaml build -o /tmp/build.md
```

`render` composes everything — slots, overrides, the rubric tables, the measured
toolchain block — exactly as an agent receives it, and reports the size. Reading
the output is a far better check than reasoning about the templates, and it costs
nothing.
