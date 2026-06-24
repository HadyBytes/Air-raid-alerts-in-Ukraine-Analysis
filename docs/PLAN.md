# Project Plan — Time-Series Analysis of Air Raid Alerts in Ukraine

A living document. We work through it stage by stage; nothing is final until reviewed together.

## Goal

A single, self-contained Python script that downloads fresh public data on Ukrainian air raid
alerts, analyzes it, and produces one **self-contained HTML report** answering: how have the
**number**, **timing** (time of day and day of week), and **duration** of alerts changed over
time — for the country as a whole and for each region (oblast). The report is built for a reader
who wants the general picture at a glance, not a long read.

## Confirmed decisions

| Decision | Choice | Notes |
|---|---|---|
| Output format | Self-contained HTML report | One `.html` file, charts embedded, opens in any browser, prints to PDF if wanted. |
| Primary data source | Official dataset | Vadimkin `ukrainian-air-raid-sirens-dataset`, `official_data_en.csv`. Authoritative, updated daily. |
| Regional depth | All oblasts | National trends first, then every oblast. |
| Timezone | Kyiv local time | Convert from UTC (source) to Europe/Kyiv with DST handling. |
| Start of analysis window | 2022 | Official records begin 15 Mar 2022. |

## Data source

**Repository:** `https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset`
**File:** `datasets/official_data_en.csv` (download the raw URL fresh on each run).
**Origin:** the official national Telegram channel `@air_alert_ua` ("Повітряна тривога").
See [`DATA_SOURCE.md`](DATA_SOURCE.md) for the full note on what "official" means — this is folded into
the README at the end.

Columns: `oblast, raion, hromada, level, started_at, finished_at, source`. Each row is one alert with
UTC timestamps. Verified shape (24 Jun 2026 pull): ~273k rows, from 15 Mar 2022 onward, 25 oblast
labels (incl. Kyiv City). Known characteristics we handle in data prep:

- **Timezone:** all source times are UTC → convert to Europe/Kyiv before any time-of-day / weekday logic.
- **Granularity / mapping:** rows come at `oblast`, `raion`, or `hromada` level, but **every row already
  carries its parent `oblast`** (the source attaches it from Ukraine's official administrative hierarchy).
  So aggregating to oblast is a group-by on the existing column — we do **not** invent a mapping,
  and the analysis relies on no external raion→oblast crosswalk file.
- **Oblast-alert definition (oblast-precedence + quorum of active raions):** because the data mixes
  oblast / raion / hromada granularity over time, we do **not** use a naive "any subdivision ⇒ whole
  oblast" union (a single stuck frontline siren would otherwise keep an entire oblast under permanent
  alert). Instead, two regimes, with **oblast-level alerts taking precedence**:
    - **Oblast mode (precedence).** When the oblast itself is still issuing oblast-wide alerts around
      _t_ — i.e. an oblast-level alert falls within **±15 days** of _t_, on **both** the before and
      after side — we trust the oblast-level rows directly and **suppress the raion quorum**. This
      carries the pre-switch period: e.g. Dnipropetrovsk kept issuing oblast-wide alerts *and* a
      permanent Nikopolskyi (frontline) siren well into 2025; without precedence the quorum's
      denominator collapsed to that one reporting raion and glued its siren into a fake 31-day
      oblast alert (15 Feb–18 Mar 2025). Diagnosed directly from the data; see README.
    - **Raion mode (quorum).** Everywhere else, the **oblast is under alert at _t_ when at least half
      of its _active_ raions are under alert at _t_**:
        - **Unit = raion.** A raion is "under alert" if it, any of its hromadas, or an oblast-wide
          alert is active. (Hromada alerts roll up to their parent raion; raions per oblast range 3–8.)
        - **Active raions (the denominator) = raions that appeared in the data within ±30 days of _t_**,
          so the measure tracks the changing reporting structure and — crucially — **excludes occupied
          raions** (which stop reporting) and **re-includes de-occupied ones** (which resume), instead
          of being diluted by permanently silent raions.
        - **Threshold = 50%** (tunable). **At least 2 active raions** must exist for the quorum to fire,
          so a lone reporter can never carry the oblast (a cheap guard against denominator collapse).
    - **Direct oblast-level alerts** always count as full oblast coverage, in both regimes — this also
      carries the early 2022–2023 period when alerts were issued oblast-wide.
  Implemented as a sweep-line over raion alert/active intervals per oblast, plus interval dilation for
  the ±15-day oblast-precedence gate. **Validation:** with this rule exactly **one** oblast-alert runs
  ≥3 days across the whole dataset — the genuine Donetsk offensive of 20–24 Feb 2026 (all 8 raions
  contributing, none missing) — versus 22 (21 single-frontline-raion artifacts) under the quorum
  alone. This logic **must be explained in the README** (see Stage 6).
- **Excluded regions:** **Luhansk** and **Crimea** are dropped (permanent sirens; the source omits them —
  Luhansk has 2 stray rows, Crimea none). The output states this explicitly.
- **Occupied territories:** the data has **no "occupied" label**; occupied areas surface as absent/sporadic
  records. Rule: where a territory's data is non-systemic (and/or its name contains "occupied"), exclude it
  and say so in the output note — we do not read missing data as "zero alerts."
- **Open/ongoing alerts:** rows with a missing `finished_at` are handled explicitly — excluded from
  duration stats or capped, never silently treated as zero.
- **Reliability:** verify row counts and date range on download; fail loudly if the file is empty,
  malformed, or the schema changed.

## Stages

### Stage 1 — Scaffolding and environment
Set up the repo skeleton, `requirements.txt` (pinned versions), and a virtual-environment-friendly
layout. Pick the minimal dependency set: `pandas` for data, a plotting library (`plotly` preferred —
it produces interactive charts that embed cleanly into one self-contained HTML), `requests` for the
download. Confirm the script runs end-to-end on a clean machine with only Python installed.

### Stage 2 — Data acquisition
Download `official_data_en.csv` from the raw GitHub URL at runtime. Cache a local copy so the script
still works offline / if the source is briefly unavailable, with a clear message about which copy is
in use. Validate schema and date coverage immediately after download.

### Stage 3 — Data cleaning and preparation
Parse timestamps, convert UTC → Europe/Kyiv. Compute each alert's **duration**. Map raion → oblast for
the post-2025 records and merge overlapping intervals per oblast. Drop/cap open alerts. Filter to the
agreed window (from 15 Mar 2022). Produce one tidy alert-level table plus pre-aggregated tables
(per month, per oblast, per hour-of-day, per weekday) that the charts read from.

### Stage 4 — Analysis and visualizations
Build the chart set, optimized for glance-readability (clear titles, short captions, consistent colors):

0. **Summary banner** — a headline strip at the top with a few at-a-glance numbers (e.g. total alerts,
   date range, busiest oblast, busiest month, typical alert length), so the reader gets the gist before
   scrolling.
1. **Number over time** — alerts per month, nationally; trend direction obvious at a glance.
2. **Duration over time** — typical alert length per month, shown as **two separate lines, mean and
   median**, so no single statistic looks like a chosen narrative. If a lightweight toggle between
   them is feasible with the base libraries (Plotly's built-in trace toggle / buttons — no extra deps),
   add it; otherwise both lines just stay visible.
3. **Time of day** — distribution of alert starts across the 24-hour clock (Kyiv time).
4. **Day of week** — alerts by weekday.
5. **Regional picture** — all oblasts as a **heatmap** (oblast × time, e.g. month), encoding alert
   count and/or total time-under-alert, so the most-affected regions and periods stand out instantly.
   Luhansk and Crimea are omitted with a visible note.
6. Each section gets a one-line plain-language takeaway above the chart.

### Stage 5 — Report assembly
Compose the charts and takeaways into a single self-contained `.html` file: a short header
(what this is, data source, date range, generation date), the national sections, then the regional
section. Everything embedded so the file works with no internet and no extra files. Include a small
footnotes/caveats section stating: data source (`@air_alert_ua`), **Luhansk and Crimea excluded**,
**occupied territories not included** (absent/non-systemic data), granularity handling, and open alerts.

### Stage 6 — Repository packaging
Package for a clean, runnable GitHub repo:

- `README.md` — what it does, one-command setup (`pip install -r requirements.txt`), how to run, sample
  output. **Folds in the `docs/DATA_SOURCE.md` note** (official source explanation), documents how raions
  are grouped under their parent `oblast` (directly from the source column, no external crosswalk), **and
  explains the oblast-alert quorum logic** (quorum of active raions, ±30-day active window, 50% threshold).
- `requirements.txt` — pinned dependencies.
- `examples/` — a committed **example output** (`report_example.html`) and **input data example**
  (a small slice of the CSV).
- `.gitignore` — excludes the venv, caches, freshly downloaded data, generated reports.
- `LICENSE` and data attribution to the source dataset and the `@air_alert_ua` channel.
- Verify a fresh clone runs successfully assuming the user has *no* libraries pre-installed.

### Stage 7 — Verification (before we call it done)
- Re-run on a clean environment; confirm the HTML opens and renders in a browser.
- Spot-check numbers against the source (e.g. total alert count, a known busy month).
- Sanity-check edge cases: DST boundaries, open alerts, permanent-siren regions, the raion→oblast period.
- Review charts for at-a-glance clarity and confirm the report reads well to a non-technical viewer.

## Resolved (from review)
- Summary banner with headline numbers: **yes, include it.**
- Duration statistic: **show both mean and median** as separate lines; add a base-library toggle if free.
- Regional layout: **heatmap.**

## Open questions to revisit
- Heatmap metric: alert *count* vs *total time-under-alert* per oblast-month (maybe offer both via toggle).
- Exact summary-banner numbers once we see how they read at a glance.

## Working principles
Don't rush; ask rather than guess; propose best practices; optimize for the reader's quick understanding
and for reliability.
