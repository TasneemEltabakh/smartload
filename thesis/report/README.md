# SmartLoad — CIE 599 Final Report (LaTeX source)

This directory holds the LaTeX source for the graduation final report:
**“SmartLoad: An Intelligent Middleware for Telemetry-Driven Traffic Routing and
Predictive Resource Scaling in Distributed Backend Systems.”** It follows the CIE 599
2025/2026 template (cover → acknowledgement → declaration → abstract → ToC/LoT/LoF →
abbreviations → 6 chapters → references → Arabic abstract → Arabic cover → appendices).

## Build

The report uses **XeLaTeX** (for Times New Roman via `fontspec` and the Arabic
sections via `polyglossia`) and **biber** for the IEEE bibliography.

```bash
# from this directory (thesis/report)
latexmk -xelatex -interaction=nonstopmode main.tex
```

Or the manual sequence:

```bash
xelatex main.tex
biber main
xelatex main.tex
xelatex main.tex
```

### Prerequisites

- **TeX Live 2023+** or **MiKTeX** with: `fontspec`, `polyglossia`, `bidi`,
  `biblatex` + **`biblatex-ieee`** + **`biber`**, `geometry`, `setspace`, `titlesec`,
  `caption`, `booktabs`, `tabularx`, `longtable`, `multirow`, `enumitem`, `tikz`,
  `pgfgantt`, `listings`, `xcolor`, `hyperref`, `cleveref`, `amsmath`, `amssymb`.
  (On MiKTeX these auto-install on first build.)
- **Fonts:** *Times New Roman* (present by default on Windows). The Arabic abstract and
  cover reuse Times New Roman’s Arabic glyphs via `\newfontfamily\arabicfont[Script=Arabic]`,
  so **no extra Arabic font (e.g. Amiri) is required**. To switch to Amiri, change that one
  line in `preamble.tex`.

> No TeX toolchain was available in the authoring environment, so the tree was
> verified statically (inputs resolve, all figures present, all 35 cited keys exist in
> the bibliography, every `\begin`/`\end` balances, all `ch:*` cross-reference targets
> defined) but **not** compiled. The first local `latexmk` run is the place to confirm
> float placement and page counts.

## Layout

| Path | Contents |
|---|---|
| `main.tex` | Document root + `\input` order + page-numbering switches |
| `preamble.tex` | All formatting rules (Times New Roman, 1.5 spacing, 1″ margins, US Letter, 14 pt headings, captions, TikZ styles, `\TBD`/`\figtbd` macros) |
| `references.bib` | 35 IEEE-style references (stable cite keys) |
| `figures/ui,bench,anomaly` | 5 operator-UI screenshots, 4 adaptive-bench plots, 1 anomaly heatmap |
| `frontmatter/` | cover (EN), acknowledgement, declaration, abstract (EN), abbreviations |
| `chapters/` | 01 Introduction, 02 Literature Review, 03a/03b Proposed Design, 04a/04b/04c Implementation, 05 Discussion & Impact, 06 Conclusion |
| `backmatter/` | Arabic abstract, Arabic cover, appendices (A–E) |

## Placeholders — what to fill before submission

Every item left to fill is a visible marker. Find them all with:

```bash
grep -rn '\\TBD{\|\\figtbd{' chapters frontmatter backmatter
```

Current list (24 `\TBD`, 2 `\figtbd`):

- **Cover pages (EN + AR) + acknowledgement:** the three student IDs, both supervisors’
  programs/affiliations, and the Zewail City crest image (the two `\figtbd`).
- **Appendix B (Cost Sheet):** the monetary line items (deployment-specific by design),
  exact library-version pins, and one operator-runbook detail.
- **§5.2 (Economic impact):** an optional monetary figure for the autoscaling cost saving.
- **§4.4 (Evaluation):** a neutral note pointing to a planned broader head-to-head
  benchmark across additional workloads (a roadmap item).

## Style

The report is written in clear, accessible English with a natural voice, and is
results-forward: it leads with what the system does and frames remaining work as scope
choices and future directions. All reported numbers are real measurements taken from the
project’s committed benchmark runs and test suite — nothing is invented.
