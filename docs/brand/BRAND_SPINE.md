# SmartLoad — Brand Spine v1

The canonical reference for everything visible about SmartLoad: wordmark, tagline, palette, type, logomark, and voice. Every asset (landing page, deck, video, README hero, social card, one-pager) builds from this sheet. If something on this page is wrong, fix it here first — then update the derived assets.

---

## 1. Positioning

**Category.** Middleware for adaptive traffic management. Self-hosted today, managed control plane on the roadmap.

**Wedge.** Every "smart infrastructure" product asks you to trust a model. SmartLoad is the one that doesn't — it ships with a deterministic fallback and an operator override on every decision.

**One-line description.**
> SmartLoad is adaptive load management that routes ahead of the spike — and gives operators a kill switch on every decision it makes.

---

## 2. Wordmark

**Primary wordmark.** `smartload/`

- Lowercase, monospace, with a trailing forward slash. The slash reads as routing — the wordmark literally points downstream.
- Set in **JetBrains Mono Bold** (free, open-source, ships with most dev tooling). Berkeley Mono is the paid alternate if budget allows.
- The slash is part of the mark, not punctuation. Never drop it. Never replace it with a different glyph.

**Alternate wordmarks.**
- `SmartLoad` — title case, geometric sans. Use in long-form prose where the mono wordmark would clash (e.g. inside a sentence in a research paper).
- `SMARTLOAD` — all caps mono. Reserve for monogrammed merch / sticker / 1-color print where the slash would not survive reproduction.

**Clearspace.** Minimum clearspace around the wordmark = the height of the lowercase `s`. No other element enters that zone.

**Minimum size.** 64px wide on screen. Below that, switch to the logomark alone.

---

## 3. Tagline

**Lead line.**
> **Routing learns. Safety doesn't.**

Always set on its own line, directly under the wordmark or as the hero subhead on a page. Never paraphrase. Never translate without re-approving the cadence.

**Secondary lines** (use only on dedicated feature pages or video subheads, never as the lead):

| Context | Line |
|---|---|
| Forecast / autoscaling page | Route ahead of the spike. |
| Operator UI / audit page | See every decision. Override any of them. |
| Self-host / install page | Run it locally in 90 seconds. |
| Trust / safety page | The kill switch is built in. |

---

## 4. Logomark

**Concept.** A predictive curve drawn slightly ahead of an actual curve. Two strokes: the **actual** line (in muted graphite, behind) and the **forecast** line (in mint, in front and one step ahead). The mint line is always leading.

**Construction.**
- Two sine-derived curves on a 24×24 grid.
- The forecast curve is offset by 3 grid units to the right of the actual curve.
- Stroke width = 2 units. Round caps. Both strokes share the same baseline.
- The forecast curve terminates with a small filled circle (the "next decision point").

**Color rules.**
- On dark surfaces: actual = `#5B6472`, forecast = `#4ADE80`, decision point = `#4ADE80`.
- On light surfaces: actual = `#5B6472`, forecast = `#16A34A`, decision point = `#16A34A`.
- 1-color reproduction (sticker, embossing): both curves in the surface accent color, no decision point.

**When to use the logomark alone.** Favicons, app icons, social avatars, footer marks, sub-64px placements. Anywhere with full breathing room, use the wordmark.

---

## 5. Palette

Two-color brand. Resist adding a third.

| Role | Hex | Use |
|---|---|---|
| **Base** | `#0E1116` | Page background, hero canvas, deck slides |
| **Text** | `#E6E8EC` | Body type on base |
| **Accent (Mint)** | `#4ADE80` | The forecast curve, CTA buttons, live-status pulses, every "this is alive" signal |
| **Muted** | `#5B6472` | The actual curve, secondary text, dividers, inactive states |

**Light-mode flip** (for printed docs, papers, light landing variants):
| Role | Hex |
|---|---|
| Base | `#F7F8FA` |
| Text | `#0E1116` |
| Accent (Mint) | `#16A34A` |
| Muted | `#6B7280` |

**Functional colors** for the product UI (status, alerts) are *not* brand colors and do not appear in marketing assets unless we are screenshotting the operator UI itself.

---

## 6. Typography

| Tier | Face | Use |
|---|---|---|
| Wordmark + numerics + code | **JetBrains Mono** Bold / Regular | The wordmark, metric callouts, code blocks, timestamps, decision labels |
| Headlines + body | **Inter** SemiBold / Regular | Everything else. Headings, paragraphs, captions |

**Why two faces.** The mono is the product's DNA — it shows up wherever a real decision or measurement is on screen. The sans carries the marketing voice. Never reverse them. Mono is not the body face.

**Tracking & leading.** Headlines `-0.015em`, body `0`. Body leading 1.55. Mono leading 1.45.

---

## 7. Voice

**Voice qualities.** Precise. Operator-respectful. Quietly confident. Numbers when possible.

**Five rules.**
1. **Lead with the verb.** "Routes ahead of the spike." not "SmartLoad is designed to route…"
2. **Numbers beat adjectives.** "Excluded the bad backend in 3 seconds" beats "fast anomaly response."
3. **Never sell trust — show the override.** The proof is the kill switch, not the promise.
4. **No hype vocabulary.** Avoid: revolutionary, intelligent, cutting-edge, seamless, magical, next-gen.
5. **Mention the fallback near the model.** Anywhere we say "learns," "predicts," or "decides," the next sentence references override / audit / safe_mode.

**Say this, not that.**
| Don't | Do |
|---|---|
| Revolutionary AI-powered load balancing | Routing that learns. Safety that doesn't. |
| Intelligent traffic optimization | Routes ahead of the spike. Excludes sick backends before users feel them. |
| Seamlessly scales your infrastructure | Grows the pool ahead of the forecast, not in response to the alarm. |
| Trust our model | Override any decision. Every change is audit-logged. |

---

## 8. Motion

Motion is part of the brand, not decoration. Every hero placement has one moving element.

**The signature motion.** The predictive curve animates: the muted actual line draws first, the mint forecast line catches up and overtakes it, the decision point pulses once. ~1.6 seconds. This loop is the SmartLoad equivalent of a heartbeat — it appears in the logomark animation, the landing page hero, and the video idents.

**Rules.**
- One signature motion per surface, never two competing.
- Easing: `cubic-bezier(0.22, 1, 0.36, 1)` — slow start, decisive finish. Never linear.
- Looping motion pauses 800ms between cycles. Things that are alive breathe; things that don't breathe look broken.

---

## 9. Asset inventory (built from this spine)

Each derived asset references this file. When the spine changes, these regenerate.

- Landing page (hero, three feature blocks, CTA)
- One-pager PDF
- Pitch deck (10 slides)
- 30s teaser video
- 90s elevator video
- 3-minute product tour
- Vertical clip series (4 shorts)
- README hero GIFs
- LinkedIn carousel template
- "Before / After" latency chart
- Social avatar + favicon

---

## 10. Do / Don't

| Don't | Why |
|---|---|
| Use a gradient on the wordmark or logomark | Reads SaaS, dilutes the infrastructure tone |
| Add a third brand color | Two-color is the discipline; a third dilutes the mint |
| Set the tagline on a separate page from the wordmark | They are a unit |
| Translate the tagline without cadence review | The rhythm carries the meaning |
| Show the model without showing the override | Breaks the wedge |
| Use stock photography of server rooms | Cliché; the product is the visual |

---

*This is v1. Every revision bumps the version at the top and lands a dated entry in the asset inventory.*
