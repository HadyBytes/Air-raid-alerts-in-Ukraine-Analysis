# Air-raid alerts in Ukraine — time-series analysis

A single Python script that downloads the official air-raid alert dataset for Ukraine and
produces one self-contained, interactive HTML report answering how the **number**, **timing**
(time of day and day of the week) and **duration** of alerts have changed over time — both
nationally and per oblast.

The report is a single `.html` file. It opens in any modern browser, needs no server, and
embeds its own copy of Plotly.js plus the aggregated data, so it works offline and can be
emailed or archived as-is. A committed sample is in
[`examples/report_example.html`](examples/report_example.html).

## What the report shows

The report opens with five summary cards (total alerts, oblasts covered, date range, and the
median / mean alert duration) followed by four interactive sections:

1. **How many alerts over time** — alert counts with a day / week / month / quarter granularity
   switch.
2. **How long alerts lasted** — mean and median duration per period, shown together.
3. **When alerts happen** — distribution by hour of day and by day of the week, with a period
   selector (last month / 3 months / year / all time).
4. **Where alerts concentrate** — an oblast × month heatmap of alert counts and hours under alert.

A global oblast multi-select filter at the top drives every chart at once, so you can read the
national picture or focus on a few regions. All time-of-day and weekday figures are in **Kyiv
local time** (UTC+2 in winter, UTC+3 in summer); all charts are computed in the browser from an
embedded data payload.

## Quick start

You need **Python 3.10 or newer**. You do **not** need to install anything else by hand — the
steps below install the three required libraries into an isolated environment.

```bash
# 1. clone
git clone https://github.com/HadyBytes/Air-raid-alerts-in-Ukraine-Analysis.git
cd Air-raid-alerts-in-Ukraine-Analysis

# 2. create an isolated environment and install dependencies
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. run
python air_raid_analysis.py
```

The script downloads the latest data, runs the analysis, and writes the report to
`output/air_raid_report.html`. Open that file in your browser.

To check your environment without running the full pipeline:

```bash
python air_raid_analysis.py --check
```

If the download source is ever unreachable, the script falls back to the most recent cached
copy in `data/` and tells you it did so. A partial or corrupt download never overwrites a good
cached copy.

## Dependencies

Pinned in [`requirements.txt`](requirements.txt):

- `pandas==3.0.3` — data wrangling
- `plotly==6.8.0` — charts and the self-contained HTML
- `requests==2.34.2` — data download

## Data source and provenance

The analysis uses the **official** CSV from the open dataset
[`Vadimkin/ukrainian-air-raid-sirens-dataset`](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset)
(`datasets/official_data_en.csv`). That repository is a *collector*: its processor reads
messages from a single Telegram channel, **`@air_alert_ua`** ("Повітряна тривога" / *Air Alert
Ukraine*), the public broadcast arm of Ukraine's official state air-raid alert system, operated
by Ajax Systems and launched 15 March 2022.

Each row corresponds to a real activation ("Повітряна тривога") and deactivation ("Відбій
тривоги") message from that system. The alert *signals* themselves originate with Ukrainian
state bodies (regional military administrations / Air Force / ДСНС) through the official Air
Alert system; the Telegram channel broadcasts them, and Vadimkin's CSV is a faithful scrape.

What the collector adds is light and transparent: it parses each message into a structured row,
attaches the administrative location (oblast / raion / hromada) from Ukraine's official
administrative hierarchy, and — when an "all-clear" message never arrives — caps that alert at
+1 hour rather than leaving it open. All source timestamps are stored in **UTC**.

Government references that document the underlying alert system are listed in
[`docs/DATA_SOURCE.md`](docs/DATA_SOURCE.md).

### Columns

`oblast, raion, hromada, level, started_at, finished_at, source`

### Coverage and known limits

- **Window:** records begin **15 March 2022** and update daily.
- **Granularity shift:** alerts were issued at **oblast** level early on, with **raion**
  (district) and **hromada** (community) level messages appearing increasingly over time. Every
  row still carries its parent `oblast`, so oblast-level aggregation is consistent across the
  whole window. The methodology below handles this shift explicitly.
- **Permanent sirens are excluded by the source:** **Luhansk** (continuous since Apr 2022) and
  **Crimea** (continuous since Dec 2022) are not represented as ongoing alerts, so this analysis
  excludes them outright rather than reading their sparse records as "few alerts".
- **Occupied territories** carry no explicit label; they simply produce absent or sporadic
  records. The analysis treats sparse / non-systemic data as unreliable, never as "zero alerts".

## Methodology

### From messages to oblast-level alerts

Because reporting granularity changed over time (oblast-level early, raion-level later), a naive
count would be distorted: a single permanent frontline raion could otherwise stand in for an
entire oblast. The script reconstructs a consistent **oblast-level** alert timeline using two
regimes, with direct oblast-level messages always taking precedence:

1. **Oblast mode (precedence).** When the oblast itself is issuing oblast-wide alerts around a
   given time (an oblast-level alert within ±15 days on *both* sides), those oblast-level rows are
   trusted directly and the raion quorum is suppressed.
2. **Raion mode (quorum).** Otherwise the oblast counts as "under alert" when at least **50% of
   its active raions** are under alert. A raion is "active" at time *t* if it appeared in the data
   within ±30 days of *t* — an adaptive denominator that drops occupied raions (which stop
   reporting) and re-includes de-occupied ones. At least **2** raions must be active for the
   quorum to fire, so a lone reporter can never carry an oblast.

Direct oblast-level alerts always count as full oblast coverage in both regimes. Each raion
row in the source data already carries its parent `oblast`, so no external crosswalk is
needed — raions are grouped under their oblast directly from that column.

This produces a clean set of non-overlapping oblast-alert intervals, which all charts are built
from. The tunable constants (`QUORUM_THRESHOLD`, `ACTIVE_WINDOW_DAYS`, `OBLAST_PRECEDENCE_DAYS`,
`MIN_ACTIVE_RAIONS`) live at the top of `air_raid_analysis.py`.

### Time zones and DST

Time-of-day and weekday distributions are reported in **Kyiv local time**. Because Ukraine
observes daylight saving, each alert interval is split along UTC-hour boundaries and each segment
is converted to Kyiv time before its local hour and weekday are read, so the spread is correct
across DST transitions.

### A note on AI assistance

This project was built with AI assistance. Every figure in the report was independently
re-derived from the raw CSV using a second, deliberately different method (a per-minute time
grid rather than interval algebra) and matched the report's numbers within rounding /
grid-resolution tolerance. The transparency note is reproduced inside the report itself.

## Repository layout

```
air_raid_analysis.py        # the whole pipeline: download -> analyse -> report
requirements.txt            # pinned dependencies
README.md                   # this file
LICENSE                     # MIT (code) + data attribution
docs/                       # supporting documentation
  DATA_SOURCE.md            #   detailed data provenance and government references
  PLAN.md                   #   original design document
examples/                   # a committed sample report + a small input-data slice
output/                     # generated report (created on first run; git-ignored)
data/                       # downloaded / cached CSV (git-ignored)
```

## Reproducibility

The dataset updates daily, so re-running the script produces a report covering more recent dates.
For a fixed reference, [`examples/report_example.html`](examples/report_example.html) is a
committed snapshot, and [`examples/official_data_sample.csv`](examples/official_data_sample.csv)
shows the exact input schema.

## License

Code is released under the MIT License — see [`LICENSE`](LICENSE). The underlying alert data is
collected and published by Vadym Klymenko (`Vadimkin`); the alert messages originate from the
official `@air_alert_ua` channel. Please credit both if you reuse the data.
