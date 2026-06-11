# Academic assessment — SmartLoad

This file collects the SmartLoad material that exists for **university-assessment purposes**: thesis writing, poster preparation, in-class presentation, and project provenance. None of it is needed to use SmartLoad as a middleware product — the [README](../README.md) covers that. The content here was originally embedded in the README and was moved out as part of the open-source product reframe so the front page stays developer-facing.

The three docs that back every assessment artefact are:

- [`docs/SOURCE_OF_TRUTH.html`](SOURCE_OF_TRUTH.html) — canonical product spec, every architectural decision with a hash-anchored section
- [`docs/PROJECT_WALKTHROUGH.md`](PROJECT_WALKTHROUGH.md) — narrative file-by-file tour with code excerpts
- [`docs/PROJECT_STATE.md`](PROJECT_STATE.md) — point-in-time audit of where the project actually stands

Together they're designed to be the only source you need for a thesis, poster, or presentation — no repo reading required.

---

## Reading order for thesis / research

Start at the top, follow the arrow:

[§2 Overview](SOURCE_OF_TRUTH.html#sec-2-overview) → [§14 ML Foundations](SOURCE_OF_TRUTH.html#sec-9-data) → [§15 Routing Authority](SOURCE_OF_TRUTH.html#sec-10-routing) → [§22 Changelog](SOURCE_OF_TRUTH.html#sec-15-changelog) → **[§31 Related Work](SOURCE_OF_TRUTH.html#sec-31-related-work) · [§32 Algorithm Foundations](SOURCE_OF_TRUTH.html#sec-32-algorithms) · [§33 Methodology](SOURCE_OF_TRUTH.html#sec-33-methodology) · [§34 Results & Discussion](SOURCE_OF_TRUTH.html#sec-34-results) · [§35 Limitations & Future Work](SOURCE_OF_TRUTH.html#sec-35-limitations)**

---

## Writing about SmartLoad — lift table

Every assessment artefact (thesis chapter, poster panel, presentation slide) has a canonical source section in the SOT. Lift the prose directly; the cross-references inside each section are already wired.

| Artefact | Lift from |
|---|---|
| **Thesis — Introduction** | [SOT §2 Executive Overview](SOURCE_OF_TRUTH.html#sec-2-overview) + [§3 Canonical Project Definition](SOURCE_OF_TRUTH.html#sec-3-definition) |
| **Thesis — Background / Related Work** | [SOT §31 Background & Related Work](SOURCE_OF_TRUTH.html#sec-31-related-work) (inline citations are lift-ready) |
| **Thesis — System Design** | [SOT §4 Principles](SOURCE_OF_TRUTH.html#sec-design-principles) + [§5 Big Picture](SOURCE_OF_TRUTH.html#sec-4-architecture) + [§8 Service Deep Dives](SOURCE_OF_TRUTH.html#sec-6-deepdives) + [§12 Diagrams](SOURCE_OF_TRUTH.html#sec-architecture-map) + [§15 Routing Authority](SOURCE_OF_TRUTH.html#sec-10-routing) |
| **Thesis — Algorithms / Methodology** | [SOT §32 Algorithm Foundations](SOURCE_OF_TRUTH.html#sec-32-algorithms) + [§33 Evaluation Methodology](SOURCE_OF_TRUTH.html#sec-33-methodology) + [Walkthrough §8 Algorithms & training procedure](PROJECT_WALKTHROUGH.md#8-algorithms--training-procedure) |
| **Thesis — Implementation** | [Walkthrough](PROJECT_WALKTHROUGH.md) (file-by-file tour, code excerpts, §1–§8) |
| **Thesis — Results / Discussion** | [SOT §34 Results & Discussion](SOURCE_OF_TRUTH.html#sec-34-results) — synthesised honest finding; raw run-by-run numbers in [§22 Changelog](SOURCE_OF_TRUTH.html#sec-15-changelog) v1.0.7r/s/t |
| **Thesis — Limitations / Future Work** | [SOT §35 Limitations & Future Work](SOURCE_OF_TRUTH.html#sec-35-limitations) |
| **Thesis — Conclusion** | [SOT §31.7 Positioning in one paragraph](SOURCE_OF_TRUTH.html#sec-31-related-work) + [§34.5 What this confirms](SOURCE_OF_TRUTH.html#sec-34-results) |
| **Poster — Problem statement** | [SOT §2 Problem statement](SOURCE_OF_TRUTH.html#sec-2-overview) (one paragraph, lift-ready) |
| **Poster — System diagram** | [SOT §5 Figure 5.1 Context](SOURCE_OF_TRUTH.html#sec-4-architecture) + [Figure 5.2 Layer](SOURCE_OF_TRUTH.html#sec-4-architecture) + [Figure 5.3 MAPE Loop](SOURCE_OF_TRUTH.html#sec-4-architecture) (Mermaid sources) |
| **Poster — Contribution** | [SOT §31.7 Positioning paragraph](SOURCE_OF_TRUTH.html#sec-31-related-work) |
| **Poster — Headline numbers** | [SOT §34.3 Per-phase p95 table](SOURCE_OF_TRUTH.html#sec-34-results) (honest, including the +3 s max-latency cost) |
| **Presentation — Story arc** | §2 (what / why) → §31 (where the field is) → §5 + §15 (the architecture) → §32 (how the engines work) → §33 (how we evaluate) → §34 (what we found) → §35 (what's next) |
| **Presentation — Demo flow** | [SOT §28 Operator UI Guide](SOURCE_OF_TRUTH.html#sec-28-operator-ui) (policy → audit → manual actions → status) + the demo-ui benchmark page from [Walkthrough §5.5](PROJECT_WALKTHROUGH.md#55-toolsdemo-ui--developer-demo-harness) |
| **Presentation — Honest read** | [SOT §34.3–§34.6](SOURCE_OF_TRUTH.html#sec-34-results) — the harness works, the mechanism works, the trained policy needs retraining on heterogeneous traces; the binding constraint is named. |

> Every cross-section reference inside the SOT is a working hash anchor — Ctrl-F by section number to jump.

---

## Project provenance

Originally developed as a graduation project at Zewail City of Science, Technology, and Innovation (CIE 2025/2026, Team 09: Tasneem Muhammed, Nada Nabil, Rghda Salah; supervisors Dr. Tamer Ashour, Dr. Doaa Shawky). The canonical design history is in [`SOURCE_OF_TRUTH.html`](SOURCE_OF_TRUTH.html).
