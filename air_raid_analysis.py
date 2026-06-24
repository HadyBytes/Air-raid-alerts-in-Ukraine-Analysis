#!/usr/bin/env python3
"""
Time-series analysis of air raid alerts in Ukraine.

Downloads the official air-raid alert dataset (sourced from the @air_alert_ua channel,
collected by github.com/Vadimkin/ukrainian-air-raid-sirens-dataset), analyses how the
number, timing (time of day / day of week) and duration of alerts changed over time —
nationally and per oblast — and produces a single self-contained HTML report.

See docs/PLAN.md for the full design and docs/DATA_SOURCE.md for the data provenance.

Usage:
    python air_raid_analysis.py            # run the full pipeline -> HTML report
    python air_raid_analysis.py --check    # verify environment/dependencies only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------

DATA_URL = (
    "https://raw.githubusercontent.com/Vadimkin/"
    "ukrainian-air-raid-sirens-dataset/main/datasets/official_data_en.csv"
)

# Source timestamps are UTC; we report time-of-day / weekday in Kyiv local time.
KYIV_TZ = ZoneInfo("Europe/Kyiv")

# Official records begin on this date (first siren message in the channel).
ANALYSIS_START = "2022-03-15"

# Excluded from analysis (permanent sirens / no systemic data). See docs/DATA_SOURCE.md.
# Occupied territories are excluded implicitly: they produce absent/sporadic records,
# which we never read as "zero alerts".
EXCLUDED_OBLASTS = ("Luhanska oblast", "Crimea", "Avtonomna Respublika Krym", "Sevastopol")

# Oblast-alert logic (see README / docs/PLAN.md). Two regimes, with oblast-level alerts taking
# precedence:
#   1. Oblast-precedence ("oblast mode"): when the oblast itself is issuing oblast-wide
#      alerts around time t (an oblast-level alert within OBLAST_PRECEDENCE_DAYS both before
#      AND after t), we trust those oblast-level rows directly and suppress the raion quorum.
#      This is the period before an oblast switched to per-raion reporting; using the quorum
#      there would let a single permanent frontline raion (e.g. Nikopolskyi in Dnipropetrovsk)
#      stand in for the whole oblast.
#   2. Raion quorum ("raion mode"): otherwise, the oblast is "under alert" at t when at least
#      QUORUM_THRESHOLD of its *active* raions are under alert. A raion is "active" at t if it
#      appeared in the data within ACTIVE_WINDOW_DAYS of t (this adaptive denominator excludes
#      occupied raions, which stop reporting, and re-includes de-occupied ones). At least
#      MIN_ACTIVE_RAIONS must be active for the quorum to fire, so a lone reporter can never
#      carry the oblast.
# Direct oblast-level alerts always count as full oblast coverage in both regimes.
QUORUM_THRESHOLD = 0.5
ACTIVE_WINDOW_DAYS = 30
OBLAST_PRECEDENCE_DAYS = 15   # oblast-level rows win within +/- this many days, both sides
MIN_ACTIVE_RAIONS = 2         # quorum needs at least this many active raions to fire

# Expected CSV schema — validated on download; we fail loudly if it changes.
EXPECTED_COLUMNS = ["oblast", "raion", "hromada", "level", "started_at", "finished_at", "source"]

# Paths (repo-relative).
REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"


# --------------------------------------------------------------------------------------
# Stage 2 — data acquisition
# --------------------------------------------------------------------------------------

def _validate_schema_and_coverage(path: Path) -> dict:
    """Fail loudly if the CSV is empty, malformed, or no longer matches the expected
    schema / date coverage. Returns a small summary dict on success."""
    import pandas as pd

    # 1) Schema: compare the header to what we expect.
    with open(path, "r", encoding="utf-8") as fh:
        header = fh.readline().strip()
    found_cols = [c.strip() for c in header.split(",")]
    if found_cols != EXPECTED_COLUMNS:
        raise ValueError(
            "Unexpected CSV schema — the source format may have changed.\n"
            f"  expected: {EXPECTED_COLUMNS}\n"
            f"  found:    {found_cols}"
        )

    # 2) Coverage: non-empty, parseable timestamps, history reaching back to 2022.
    started = pd.read_csv(path, usecols=["started_at"])["started_at"]
    if started.empty:
        raise ValueError("Dataset contains no rows.")
    parsed = pd.to_datetime(started, utc=True, errors="coerce")
    n_bad = int(parsed.isna().sum())
    if n_bad:
        raise ValueError(f"{n_bad:,} rows have unparseable `started_at` timestamps.")
    start, end = parsed.min(), parsed.max()
    if start.year != 2022:
        raise ValueError(
            f"Earliest record is {start.date()} (expected 2022); source may have changed."
        )

    return {"rows": int(len(started)), "start": start, "end": end}


def download_data(url: str = DATA_URL, dest_dir: Path = DATA_DIR, timeout: int = 60) -> Path:
    """Download the fresh CSV, falling back to a cached copy if the source is unavailable.
    Validates schema and date coverage. Returns the local path.

    A partial/corrupt download never overwrites a good cached copy: we download to a
    temporary file, validate it, and only then atomically replace the cache.
    """
    import requests

    dest_dir.mkdir(parents=True, exist_ok=True)
    cache_path = dest_dir / "official_data_en.csv"
    tmp_path = dest_dir / "official_data_en.csv.part"
    headers = {"User-Agent": "ukraine-air-raid-analysis/1.0"}

    try:
        print(f"Downloading fresh data:\n  {url}")
        with requests.get(url, headers=headers, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
        _validate_schema_and_coverage(tmp_path)   # don't commit a bad download
        tmp_path.replace(cache_path)
        print(f"  saved fresh copy -> {cache_path}")
    except Exception as exc:  # network error, HTTP error, or failed validation
        if tmp_path.exists():
            tmp_path.unlink()
        if cache_path.exists():
            print(f"[WARN] Fresh download failed ({exc!r}).\n       Falling back to cached copy: {cache_path}")
        else:
            raise RuntimeError(
                f"Could not download data and no cached copy exists at {cache_path}.\n"
                f"Original error: {exc!r}"
            ) from exc

    info = _validate_schema_and_coverage(cache_path)
    print(f"Data OK: {info['rows']:,} alerts, {info['start'].date()} -> {info['end'].date()} (UTC)")
    return cache_path


# --------------------------------------------------------------------------------------
# Stage 3 — cleaning & preparation
# --------------------------------------------------------------------------------------

# --- interval algebra (all times are int64 nanoseconds since the Unix epoch, UTC) ---

def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union a list of [start, end) intervals into disjoint, sorted intervals.
    Touching intervals (end == next start) are merged."""
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:                      # overlap or touch
            if e > le:
                merged[-1] = (ls, e)
        else:
            merged.append((s, e))
    return merged


def _intersect(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Intersection of two lists of disjoint, sorted [start, end) intervals."""
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def _dilate(intervals: list[tuple[int, int]], left: int, right: int) -> list[tuple[int, int]]:
    """Extend every interval by `left` ns at the start and `right` ns at the end, then merge."""
    return _merge([(s - left, e + right) for s, e in intervals])


def _complement(intervals: list[tuple[int, int]], lo: int, hi: int) -> list[tuple[int, int]]:
    """Complement of disjoint, sorted [start, end) intervals within the bound [lo, hi)."""
    out, cur = [], lo
    for s, e in intervals:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        out.append((cur, hi))
    return out


def _quorum_segments(num: list[tuple[int, int]], den: list[tuple[int, int]],
                     threshold: float, min_den: int = 1) -> list[tuple[int, int]]:
    """Given per-raion numerator intervals (raion under alert AND active) and denominator
    intervals (raion active), pooled across raions, return the time segments where
    #under-alert / #active >= threshold AND #active >= min_den. Sweep-line over endpoints."""
    if not den:
        return []
    # event stream: (+1/-1) to running counts of numerator (N) and denominator (D)
    events: dict[int, list[int]] = {}
    for s, e in num:
        events.setdefault(s, [0, 0]); events[s][0] += 1
        events.setdefault(e, [0, 0]); events[e][0] -= 1
    for s, e in den:
        events.setdefault(s, [0, 0]); events[s][1] += 1
        events.setdefault(e, [0, 0]); events[e][1] -= 1
    times = sorted(events)
    qualifying, N, D = [], 0, 0
    for k, t in enumerate(times[:-1]):
        N += events[t][0]
        D += events[t][1]
        if D >= min_den and N >= threshold * D:
            qualifying.append((t, times[k + 1]))
    return _merge(qualifying)


def load_and_clean(csv_path: Path):
    """Read the CSV; parse timestamps as UTC; drop excluded regions and invalid rows;
    filter to the analysis window. Returns a tidy per-row frame with int64-ns UTC columns
    `start`/`end`, plus `oblast`, `level`, and `raion` (parent raion; empty for oblast rows)."""
    import pandas as pd

    df = pd.read_csv(csv_path, usecols=EXPECTED_COLUMNS)
    df = df[~df["oblast"].isin(EXCLUDED_OBLASTS)].copy()
    # also drop any explicitly "occupied"-labelled territory, should the source add such labels
    occ = df[["oblast", "raion", "hromada"]].apply(
        lambda c: c.astype(str).str.contains("occup", case=False, na=False)).any(axis=1)
    df = df[~occ]

    # Force nanosecond resolution: pandas >=3.0 may default to microseconds, and the
    # interval math below assumes int64 nanoseconds throughout.
    start = pd.to_datetime(df["started_at"], utc=True, errors="coerce").dt.as_unit("ns")
    end = pd.to_datetime(df["finished_at"], utc=True, errors="coerce").dt.as_unit("ns")
    keep = start.notna() & end.notna() & (end > start) & (start >= pd.Timestamp(ANALYSIS_START, tz="UTC"))
    df, start, end = df[keep], start[keep], end[keep]

    out = pd.DataFrame({
        "oblast": df["oblast"].values,
        "level": df["level"].values,
        "raion": df["raion"].fillna("").values,
        "start": start.astype("int64").values,
        "end": end.astype("int64").values,
    })
    return out.reset_index(drop=True)


def compute_oblast_alerts(clean, threshold: float = QUORUM_THRESHOLD,
                          active_window_days: int = ACTIVE_WINDOW_DAYS,
                          oblast_precedence_days: int = OBLAST_PRECEDENCE_DAYS,
                          min_active_raions: int = MIN_ACTIVE_RAIONS):
    """Collapse per-row alerts into oblast-level alert intervals.

    Two regimes (see config block above), with oblast-level alerts taking precedence:
      * "Oblast mode" — times within `oblast_precedence_days` (both sides) of an oblast-level
        alert. Here we trust the oblast-level rows directly and suppress the raion quorum.
      * "Raion mode" — everywhere else. The oblast is under alert when >= `threshold` of its
        active raions (>= `min_active_raions` of them) are simultaneously under alert.
    Direct oblast-level alerts always count as full coverage. Returns a frame with `oblast`,
    UTC + Kyiv start/end, and `duration_min`."""
    import pandas as pd

    DAY_NS = 24 * 3600 * 1_000_000_000
    window_ns = active_window_days * DAY_NS
    gate_ns = oblast_precedence_days * DAY_NS
    records = []
    for oblast, g in clean.groupby("oblast", sort=True):
        # direct oblast-level alerts => full coverage
        O = _merge([(s, e) for s, e in g.loc[g["level"] == "oblast", ["start", "end"]].itertuples(index=False)])

        sub = g[g["level"].isin(["raion", "hromada"])]
        num_all, den_all = [], []
        for raion, gr in sub.groupby("raion", sort=False):
            pairs = list(gr[["start", "end"]].itertuples(index=False))
            alerts_r = _merge([(s, e) for s, e in pairs])
            active_r = _merge([(s - window_ns, s + window_ns) for s, _ in pairs])
            num_all.extend(_intersect(alerts_r, active_r))   # under alert AND active
            den_all.extend(active_r)

        quorum = _quorum_segments(num_all, den_all, threshold, min_active_raions)

        # Oblast precedence: suppress the quorum wherever the oblast is in "oblast mode"
        # (an oblast-level alert within gate_ns both before AND after t).
        if O and quorum:
            before = _dilate(O, 0, gate_ns)     # an oblast alert in the last gate_ns
            after = _dilate(O, gate_ns, 0)      # an oblast alert in the next gate_ns
            oblast_mode = _intersect(before, after)
            lo = min(s for s, _ in quorum + O)
            hi = max(e for _, e in quorum + O)
            raion_mode = _complement(oblast_mode, lo, hi)
            quorum = _intersect(quorum, raion_mode)

        for s, e in _merge(quorum + O):
            records.append((oblast, s, e))

    res = pd.DataFrame(records, columns=["oblast", "start", "end"])
    res["start_utc"] = pd.to_datetime(res["start"], utc=True, unit="ns")
    res["end_utc"] = pd.to_datetime(res["end"], utc=True, unit="ns")
    res["start_kyiv"] = res["start_utc"].dt.tz_convert(KYIV_TZ)
    res["end_kyiv"] = res["end_utc"].dt.tz_convert(KYIV_TZ)
    res["duration_min"] = (res["end"] - res["start"]) / 6e10  # 6e10 ns = 1 minute
    return res.drop(columns=["start", "end"]).sort_values(["oblast", "start_utc"]).reset_index(drop=True)


# Long-alert audit threshold (days). Any oblast-alert at least this long is listed, with the
# raions that did and did not contribute, in a separate diagnostic file — so the oblast-alert
# method can be checked by hand.
AUDIT_MIN_DAYS = 3


def write_long_alert_audit(res, clean, out_dir: Path = OUTPUT_DIR, min_days: float = AUDIT_MIN_DAYS):
    """Write a standalone diagnostic listing every oblast-alert >= `min_days` long, with the
    contributing and non-contributing raions for each. Returns the CSV path (a sibling .md is
    also written). Keeps the main HTML report focused on trends while staying auditable."""
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    long = res[res["duration_min"] >= min_days * 1440].sort_values("duration_min", ascending=False)

    sub = clean[clean["level"].isin(["raion", "hromada"])]
    known = sub.groupby("oblast")["raion"].apply(lambda s: sorted(set(s) - {""})).to_dict()

    rows = []
    for r in long.itertuples(index=False):
        s_ns = int(pd.Timestamp(r.start_utc).value)
        e_ns = int(pd.Timestamp(r.end_utc).value)
        g = sub[sub["oblast"] == r.oblast]
        overlap = g[(g["start"] < e_ns) & (g["end"] > s_ns)]
        contributing = sorted(set(overlap["raion"]) - {""})
        oblast_rows = clean[(clean["oblast"] == r.oblast) & (clean["level"] == "oblast")
                            & (clean["start"] < e_ns) & (clean["end"] > s_ns)]
        non_contrib = [x for x in known.get(r.oblast, []) if x not in contributing]
        rows.append({
            "oblast": r.oblast,
            "start_kyiv": r.start_kyiv.strftime("%Y-%m-%d %H:%M"),
            "end_kyiv": r.end_kyiv.strftime("%Y-%m-%d %H:%M"),
            "days": round(r.duration_min / 1440, 2),
            "oblast_level_rows_in_window": int(len(oblast_rows)),
            "contributing_raions": "; ".join(contributing),
            "non_contributing_raions": "; ".join(non_contrib),
        })

    audit = pd.DataFrame(rows)
    csv_path = out_dir / "long_alerts_audit.csv"
    audit.to_csv(csv_path, index=False)

    md = [f"# Long oblast-alerts audit (>= {min_days:g} days)", "",
          f"Generated from the current data pull. {len(audit)} oblast-alert(s) meet the threshold.",
          "Each row lists the raions that did and did not contribute, so the oblast-alert logic",
          "(oblast precedence + 50%-of-active-raions quorum) can be checked by hand.", ""]
    if audit.empty:
        md.append("_No oblast-alert reached the threshold._")
    else:
        for _, a in audit.iterrows():
            md += [f"## {a['oblast']} — {a['days']} days",
                   f"- **Window (Kyiv):** {a['start_kyiv']} → {a['end_kyiv']}",
                   f"- **Oblast-level rows in window:** {a['oblast_level_rows_in_window']}",
                   f"- **Contributing raions:** {a['contributing_raions'] or '(none)'}",
                   f"- **Non-contributing raions:** {a['non_contributing_raions'] or '(none)'}", ""]
    (out_dir / "long_alerts_audit.md").write_text("\n".join(md), encoding="utf-8")
    return csv_path


# --------------------------------------------------------------------------------------
# Stage 4 — analysis & visualisations
# --------------------------------------------------------------------------------------

# Visual theme — sober, glance-readable palette. Steel-blue accent (no alarming red, since
# the subject is sensitive). These constants seed the JS theme used by the in-browser charts.
ACCENT = "#33608f"     # steel blue — primary series
ACCENT2 = "#6aa9a6"    # muted teal — median line
HEAT_SCALE = "Blues"   # heatmap colourscale
INK = "#1f2328"
GRID = "#e8e8ec"
FONT = "Inter, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Period windows for the time-of-day / day-of-week charts (days back from the latest record;
# None = whole history). The labels are shown as buttons in the report.
PERIODS = [("all", "All time", None), ("year", "Last year", 365),
           ("3m", "Last 3 months", 90), ("month", "Last month", 30)]


def _ns(series):
    """A datetime64 Series -> int64 nanoseconds since the epoch (ns resolution assumed)."""
    return series.astype("int64")


def _spread(starts_ns, ends_ns, lo):
    """Spread each interval's duration across the local hour-of-day (0–23) and weekday
    (Mon–Sun) it covers. Walks UTC-hour boundaries (DST-safe) and labels each one-hour
    segment by its local (Europe/Kyiv) hour/weekday. `lo` clips interval starts to a trailing
    window (None = no clip). Returns two numpy arrays of minutes: (hour[24], weekday[7])."""
    import numpy as np
    import pandas as pd

    hour = np.zeros(24)
    wday = np.zeros(7)
    HOUR_NS = 3600 * 1_000_000_000
    for cur, end in zip(starts_ns, ends_ns):
        cur = int(cur); end = int(end)
        if lo is not None and cur < lo:
            cur = lo
        while cur < end:
            nxt = ((cur // HOUR_NS) + 1) * HOUR_NS
            seg_end = end if end < nxt else nxt
            ky = pd.Timestamp(cur, unit="ns", tz="UTC").tz_convert(KYIV_TZ)
            mins = (seg_end - cur) / 6e10
            hour[ky.hour] += mins
            wday[ky.weekday()] += mins
            cur = seg_end
    return hour, wday


def _monthly(res):
    """National per-month aggregates: number of oblast-alerts and their mean/median duration."""
    import pandas as pd

    month = res["start_kyiv"].dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
    g = res.assign(month=month).groupby("month")
    out = g["duration_min"].agg(n="size", mean_min="mean", median_min="median").reset_index()
    return out


def summarize(res) -> dict:
    """Headline numbers for the summary banner."""
    monthly = _monthly(res)
    busiest_month = monthly.loc[monthly["n"].idxmax()]
    by_oblast_hours = (res.assign(h=res["duration_min"] / 60)
                       .groupby("oblast")["h"].sum().sort_values(ascending=False))
    busiest_oblast = by_oblast_hours.index[0].replace(" oblast", "")
    return {
        "total_alerts": int(len(res)),
        "n_oblasts": int(res["oblast"].nunique()),
        "date_start": res["start_kyiv"].min().strftime("%d %b %Y"),
        "date_end": res["end_kyiv"].max().strftime("%d %b %Y"),
        "median_min": float(res["duration_min"].median()),
        "mean_min": float(res["duration_min"].mean()),
        "busiest_oblast": busiest_oblast,
        "busiest_oblast_hours": float(by_oblast_hours.iloc[0]),
        "busiest_month": busiest_month["month"].strftime("%b %Y"),
        "busiest_month_n": int(busiest_month["n"]),
    }


def build_payload(res, meta: dict) -> dict:
    """Turn the oblast-alert table into a compact JSON payload that the report renders
    entirely in the browser. All filtering (by oblast) and re-aggregation (count granularity,
    duration mean/median, heatmap metric) happen client-side from this payload; the expensive,
    DST-sensitive time-of-day / day-of-week spreading is precomputed here, per oblast and per
    period, so the browser only has to sum small arrays."""
    import numpy as np
    import pandas as pd

    r = res.copy()
    r["oblast_s"] = r["oblast"].str.replace(" oblast", "", regex=False)

    # oblasts ordered by total hours under alert (most-affected first)
    hours_by = (r.assign(h=r["duration_min"] / 60).groupby("oblast_s")["h"].sum()
                .sort_values(ascending=False))
    oblasts = list(hours_by.index)
    ob_index = {o: i for i, o in enumerate(oblasts)}
    nO = len(oblasts)

    # day index (local calendar day) for each interval, relative to the earliest start
    start_local = r["start_kyiv"].dt.tz_localize(None).dt.normalize()
    day0 = start_local.min()
    day_off = ((start_local - day0).dt.days).astype(int)
    intervals = {
        "ob": [int(ob_index[o]) for o in r["oblast_s"]],
        "day": [int(d) for d in day_off],
        "dur": [round(float(x), 2) for x in r["duration_min"]],
    }

    # month axis (YYYY-MM) covering the full range; heatmap matrices oblast x month
    mper = r["start_kyiv"].dt.tz_localize(None).dt.to_period("M")
    all_months = pd.period_range(mper.min(), mper.max(), freq="M")
    months = [str(m) for m in all_months]
    m_index = {str(m): i for i, m in enumerate(all_months)}
    nM = len(months)
    heat_count = [[0] * nM for _ in range(nO)]
    heat_hours = [[0.0] * nM for _ in range(nO)]
    for o, m, d in zip(r["oblast_s"], mper.astype(str).values, r["duration_min"]):
        i, j = ob_index[o], m_index[m]
        heat_count[i][j] += 1
        heat_hours[i][j] += d / 60.0
    heat_hours = [[round(x, 1) for x in row] for row in heat_hours]

    # time-of-day / day-of-week per oblast per period (precomputed, DST-safe)
    DAY_NS = 24 * 3600 * 1_000_000_000
    end_ns = int(_ns(r["end_utc"]).max())
    su = _ns(r["start_utc"]).values
    eu = _ns(r["end_utc"]).values
    obi = np.array([ob_index[o] for o in r["oblast_s"]])
    hod = {p[0]: [] for p in PERIODS}
    dow = {p[0]: [] for p in PERIODS}
    for oi in range(nO):
        idx = np.where(obi == oi)[0]
        s_o, e_o = su[idx], eu[idx]
        for key, _label, nd in PERIODS:
            lo = None if nd is None else end_ns - nd * DAY_NS
            h, w = _spread(s_o, e_o, lo)
            hod[key].append([round(float(x), 1) for x in h])
            dow[key].append([round(float(x), 1) for x in w])

    banner = summarize(res)

    # static, plain-language takeaways describing the default (all-oblast) view
    monthly = _monthly(res)
    first_year = monthly[monthly["month"].dt.year == monthly["month"].min().year]["n"].mean()
    last_full = monthly.iloc[-4:-1]["n"].mean()
    trend = "risen" if last_full > first_year else "fallen"
    hour_all, _ = _spread(su, eu, None)
    peak_hour = int(hour_all.argmax())
    takeaways = {
        "count": (f"Across all oblasts, alerts have broadly {trend} since 2022; the busiest "
                  f"month was {banner['busiest_month']} with {banner['busiest_month_n']:,} "
                  f"oblast-alerts. Choose oblasts and a granularity to explore."),
        "duration": (f"A typical alert lasts about {banner['median_min']:.0f} minutes (median); "
                     f"the mean is higher ({banner['mean_min']:.0f} min) because long frontline "
                     f"alerts pull it up."),
        "hour": (f"Alert-time concentrates overnight, peaking around {peak_hour:02d}:00 local "
                 f"time. Pick a period to see how this shifted."),
        "weekday": "Alert-time is spread fairly evenly across the week, dipping slightly at weekends.",
        "region": (f"Front-line and border oblasts dominate — {banner['busiest_oblast']} has the "
                   f"most time under alert. Luhansk and Crimea are omitted (see note)."),
    }

    return {
        "oblasts": oblasts,
        "months": months,
        "day0": str(day0.date()),
        "intervals": intervals,
        "heat_count": heat_count,
        "heat_hours": heat_hours,
        "hod": hod,
        "dow": dow,
        "periods": [[k, lab] for k, lab, _ in PERIODS],
        "weekdays": WEEKDAY_NAMES,
        "banner": banner,
        "takeaways": takeaways,
        "meta": meta,
        "theme": {"accent": ACCENT, "accent2": ACCENT2, "heat": HEAT_SCALE,
                  "ink": INK, "grid": GRID, "font": FONT},
    }


# --------------------------------------------------------------------------------------
# Stage 5 — report assembly
# --------------------------------------------------------------------------------------

REPORT_CSS = """
:root { --ink:#1f2328; --muted:#6b7280; --line:#e8e8ec; --accent:#33608f; --bg:#ffffff; }
* { box-sizing: border-box; }
body { margin:0; background:#f6f6f8; color:var(--ink);
  font-family:Inter,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; line-height:1.5; }
.wrap { max-width:980px; margin:0 auto; padding:32px 20px 64px; }
header.rep { border-bottom:3px solid var(--accent); padding-bottom:16px; margin-bottom:8px; }
header.rep h1 { font-size:26px; margin:0 0 6px; }
header.rep p { color:var(--muted); margin:2px 0; font-size:14px; }
.banner { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px;
  margin:24px 0 8px; }
.card { background:var(--bg); border:1px solid var(--line); border-radius:10px; padding:14px 16px; }
.card .num { font-size:21px; font-weight:700; color:var(--accent); }
.card .lbl { font-size:12px; color:var(--muted); margin-top:2px; }
.filter { background:var(--bg); border:1px solid var(--line); border-radius:10px;
  padding:14px 16px; margin:20px 0; }
.filter-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; }
.filter-title { font-weight:600; font-size:14px; }
.lnk { background:none; border:none; color:var(--accent); font-size:13px; cursor:pointer;
  padding:2px 6px; }
.lnk:hover { text-decoration:underline; }
.ob-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:4px 10px; }
.ob-grid label { font-size:13px; color:var(--ink); display:flex; align-items:center; gap:6px;
  cursor:pointer; }
section.chart { background:var(--bg); border:1px solid var(--line); border-radius:10px;
  padding:18px 18px 12px; margin:20px 0; }
.chead { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;
  gap:8px; margin-bottom:2px; }
.chead h2 { font-size:17px; margin:0; }
.ctrls { display:flex; gap:4px; flex-wrap:wrap; }
.ctrls button { font:inherit; font-size:12px; padding:4px 10px; border:1px solid var(--line);
  background:#fff; color:var(--muted); border-radius:6px; cursor:pointer; }
.ctrls button:hover { border-color:var(--accent); color:var(--accent); }
.ctrls button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
section.chart .takeaway { font-size:14px; color:var(--ink); margin:6px 0 4px; font-weight:600; }
.notes { background:var(--bg); border:1px solid var(--line); border-radius:10px;
  padding:18px 22px; margin-top:28px; font-size:13px; color:var(--muted); }
.notes h2 { font-size:15px; color:var(--ink); margin:0 0 8px; }
.notes ul { margin:0; padding-left:18px; } .notes li { margin:5px 0; }
.notes a { color:var(--accent); }
footer.rep { color:var(--muted); font-size:12px; text-align:center; margin-top:28px; }
"""

REPORT_NOTES_HTML = """
<div class="notes">
  <h2>Notes &amp; caveats</h2>
  <ul>
    <li><b>Source.</b> Official Ukrainian air-raid alert dataset compiled from the national
        <i>@air_alert_ua</i> ("Повітряна тривога") Telegram channel, via the open
        <a href="https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset">Vadimkin dataset</a>.
        Times are converted from UTC to local Ukrainian time (UTC+2 in winter, UTC+3 in summer;
        daylight-saving-aware).</li>
    <li><b>What "an alert" means here.</b> Raw records arrive at oblast, raion or hromada level.
        We collapse them to one timeline per oblast: an oblast counts as under alert when it
        issues an oblast-wide alert, or — once it has switched to per-raion reporting — when at
        least half of its active raions are under alert at once. (Full method in the README.)</li>
    <li><b>Filtering.</b> The headline figures above cover all included oblasts. The oblast
        selector re-computes every chart below it; the heatmap shows only the chosen oblasts.</li>
    <li><b>Luhansk and Crimea are excluded</b> — sirens there are effectively permanent and the
        source does not track them systematically.</li>
    <li><b>Occupied territories are not included.</b> They surface as absent or sporadic records;
        we never read missing data as "zero alerts".</li>
    <li><b>Open alerts</b> (no recorded end) are excluded from duration statistics rather than
        treated as zero-length.</li>
    <li><b>Made with AI.</b> This report and the script that generates it were produced with the
        help of an AI assistant (Anthropic's Claude). All figures come from the cited public
        dataset, and the analysis code is open in the project repository for inspection.</li>
  </ul>
</div>
"""

# Static body markup. The oblast checkboxes and the control buttons are populated by the JS
# from the embedded payload, so the chart sections themselves are just empty containers.
BODY_HTML = """
<div class="filter">
  <div class="filter-head">
    <span class="filter-title">Oblasts in view</span>
    <span class="filter-actions">
      <button class="lnk" id="ob-all">Select all</button>
      <button class="lnk" id="ob-none">Clear</button>
    </span>
  </div>
  <div class="ob-grid" id="ob-grid"></div>
</div>

<section class="chart">
  <div class="chead"><h2>Number of alerts over time</h2><div class="ctrls" id="ctrl-gran"></div></div>
  <p class="takeaway" id="tk-count"></p>
  <div id="chart-count"></div>
</section>

<section class="chart">
  <div class="chead"><h2>How long alerts last</h2></div>
  <p class="takeaway" id="tk-duration"></p>
  <div id="chart-duration"></div>
</section>

<section class="chart">
  <div class="chead"><h2>When alerts happen</h2><div class="ctrls" id="ctrl-period"></div></div>
  <p class="takeaway" id="tk-hour"></p>
  <div id="chart-hour"></div>
  <p class="takeaway" id="tk-weekday" style="margin-top:14px;"></p>
  <div id="chart-weekday"></div>
</section>

<section class="chart">
  <div class="chead"><h2>Regional picture</h2><div class="ctrls" id="ctrl-heat"></div></div>
  <p class="takeaway" id="tk-region"></p>
  <div id="chart-region"></div>
</section>
"""

# Client-side rendering. One global oblast filter drives all five charts; each chart has its
# own small HTML control group (granularity / period / heatmap metric) in its header, so no
# control floats over a chart axis. All aggregation is done in the browser from window.DATA.
REPORT_JS = """
(function(){
  var D = window.DATA, T = D.theme;
  var FONT=T.font, INK=T.ink, GRID=T.grid, ACCENT=T.accent, ACCENT2=T.accent2, HEAT=T.heat;
  var CFG={displayModeBar:false, responsive:true};
  var DAY_MS=86400000, day0ms=Date.parse(D.day0+'T00:00:00Z');
  var gran='month', period='all', heatMetric='hours';

  function lay(yTitle){
    return {
      font:{family:FONT,color:INK,size:13},
      paper_bgcolor:'#fff', plot_bgcolor:'#fff',
      margin:{l:60,r:24,t:10,b:46}, height:340,
      showlegend:false, hovermode:'x unified',
      xaxis:{showgrid:false, linecolor:GRID, ticks:'outside', tickcolor:GRID},
      yaxis:{showgrid:true, gridcolor:GRID, zeroline:false, title:{text:yTitle}}
    };
  }

  // ---- oblast filter ----
  var grid=document.getElementById('ob-grid');
  D.oblasts.forEach(function(name,i){
    var lab=document.createElement('label');
    var cb=document.createElement('input');
    cb.type='checkbox'; cb.checked=true; cb.id='ob'+i;
    cb.addEventListener('change', renderAll);
    lab.appendChild(cb); lab.appendChild(document.createTextNode(name));
    grid.appendChild(lab);
  });
  function selected(){
    var out=[];
    for(var i=0;i<D.oblasts.length;i++){ if(document.getElementById('ob'+i).checked) out.push(i); }
    return out;
  }
  document.getElementById('ob-all').addEventListener('click', function(){
    D.oblasts.forEach(function(_,i){ document.getElementById('ob'+i).checked=true; }); renderAll();
  });
  document.getElementById('ob-none').addEventListener('click', function(){
    D.oblasts.forEach(function(_,i){ document.getElementById('ob'+i).checked=false; }); renderAll();
  });

  // ---- button groups ----
  function buttonGroup(container, items, current, onPick){
    container.innerHTML='';
    items.forEach(function(it){
      var btn=document.createElement('button');
      btn.textContent=it.label;
      if(it.v===current) btn.classList.add('on');
      btn.addEventListener('click', function(){
        var bs=container.querySelectorAll('button');
        for(var j=0;j<bs.length;j++) bs[j].classList.remove('on');
        btn.classList.add('on'); onPick(it.v);
      });
      container.appendChild(btn);
    });
  }
  buttonGroup(document.getElementById('ctrl-gran'),
    [{v:'day',label:'Day'},{v:'week',label:'Week'},{v:'month',label:'Month'},{v:'quarter',label:'Quarter'}],
    gran, function(v){ gran=v; renderCount(); });
  buttonGroup(document.getElementById('ctrl-period'),
    D.periods.map(function(p){ return {v:p[0],label:p[1]}; }),
    period, function(v){ period=v; renderHour(); renderWeekday(); });
  buttonGroup(document.getElementById('ctrl-heat'),
    [{v:'hours',label:'Hours under alert'},{v:'count',label:'Number of alerts'}],
    heatMetric, function(v){ heatMetric=v; renderHeatmap(); });

  // ---- bucketing helpers ----
  function bucketKey(dt){
    var y=dt.getUTCFullYear(), mo=dt.getUTCMonth(), da=dt.getUTCDate();
    if(gran==='day') return Date.UTC(y,mo,da);
    if(gran==='week'){ var off=(dt.getUTCDay()+6)%7; return Date.UTC(y,mo,da)-off*DAY_MS; }
    if(gran==='quarter') return Date.UTC(y, Math.floor(mo/3)*3, 1);
    return Date.UTC(y,mo,1);
  }
  function isoDay(ms){ return new Date(ms).toISOString().slice(0,10); }
  function range(n){ var a=[]; for(var i=0;i<n;i++) a.push(i); return a; }

  // ---- renderers ----
  function renderCount(){
    var sel={}, s=selected(); for(var q=0;q<s.length;q++) sel[s[q]]=1;
    var I=D.intervals, buckets={};
    for(var k=0;k<I.ob.length;k++){
      if(!sel[I.ob[k]]) continue;
      var key=bucketKey(new Date(day0ms+I.day[k]*DAY_MS));
      buckets[key]=(buckets[key]||0)+1;
    }
    var keys=Object.keys(buckets).map(Number).sort(function(a,b){return a-b;});
    var x=keys.map(isoDay), y=keys.map(function(k){return buckets[k];});
    Plotly.react('chart-count',[{type:'bar',x:x,y:y,marker:{color:ACCENT},
      hovertemplate:'%{x}<br>%{y:,} oblast-alerts<extra></extra>'}], lay('oblast-alerts'), CFG);
  }

  function renderDuration(){
    var sel={}, s=selected(); for(var q=0;q<s.length;q++) sel[s[q]]=1;
    var I=D.intervals, g={};
    for(var k=0;k<I.ob.length;k++){
      if(!sel[I.ob[k]]) continue;
      var dt=new Date(day0ms+I.day[k]*DAY_MS);
      var key=Date.UTC(dt.getUTCFullYear(),dt.getUTCMonth(),1);
      (g[key]=g[key]||[]).push(I.dur[k]);
    }
    var keys=Object.keys(g).map(Number).sort(function(a,b){return a-b;});
    var x=keys.map(isoDay);
    var mean=keys.map(function(k){var a=g[k],t=0;for(var j=0;j<a.length;j++)t+=a[j];return t/a.length;});
    var median=keys.map(function(k){
      var a=g[k].slice().sort(function(p,q){return p-q;}), n=a.length;
      return n%2 ? a[(n-1)/2] : (a[n/2-1]+a[n/2])/2;
    });
    var L=lay('minutes'); L.showlegend=true;
    L.legend={orientation:'h',yanchor:'bottom',y:1.0,xanchor:'right',x:1.0};
    Plotly.react('chart-duration',[
      {type:'scatter',mode:'lines',name:'Mean',x:x,y:mean,line:{color:ACCENT,width:2.5},
        hovertemplate:'%{x}<br>mean %{y:.0f} min<extra></extra>'},
      {type:'scatter',mode:'lines',name:'Median',x:x,y:median,line:{color:ACCENT2,width:2.5},
        hovertemplate:'%{x}<br>median %{y:.0f} min<extra></extra>'}
    ], L, CFG);
  }

  function renderHour(){
    var s=selected(), src=D.hod[period], arr=new Array(24); for(var h=0;h<24;h++) arr[h]=0;
    for(var i=0;i<s.length;i++){ var a=src[s[i]]; for(var h2=0;h2<24;h2++) arr[h2]+=a[h2]; }
    var tot=0; for(var h3=0;h3<24;h3++) tot+=arr[h3]; if(!tot) tot=1;
    var y=arr.map(function(v){return 100*v/tot;});
    var L=lay('% of alert-time');
    L.xaxis={showgrid:false, linecolor:GRID, ticks:'outside', tickcolor:GRID,
      title:{text:'hour of day (local time, UTC+2/+3)'}, tickmode:'array',
      tickvals:[0,2,4,6,8,10,12,14,16,18,20,22]};
    Plotly.react('chart-hour',[{type:'bar',x:range(24),y:y,marker:{color:ACCENT},
      hovertemplate:'%{x}:00<br>%{y:.1f}% of alert-time<extra></extra>'}], L, CFG);
  }

  function renderWeekday(){
    var s=selected(), src=D.dow[period], arr=new Array(7); for(var d=0;d<7;d++) arr[d]=0;
    for(var i=0;i<s.length;i++){ var a=src[s[i]]; for(var d2=0;d2<7;d2++) arr[d2]+=a[d2]; }
    var tot=0; for(var d3=0;d3<7;d3++) tot+=arr[d3]; if(!tot) tot=1;
    var y=arr.map(function(v){return 100*v/tot;});
    var L=lay('% of alert-time'); L.height=300;
    Plotly.react('chart-weekday',[{type:'bar',x:D.weekdays,y:y,marker:{color:ACCENT},
      hovertemplate:'%{x}<br>%{y:.1f}% of alert-time<extra></extra>'}], L, CFG);
  }

  function renderHeatmap(){
    var s=selected();
    var src = heatMetric==='hours' ? D.heat_hours : D.heat_count;
    var z=s.map(function(i){return src[i];});
    var yl=s.map(function(i){return D.oblasts[i];});
    var ht = heatMetric==='hours'
      ? '%{y}<br>%{x}<br>%{z:.0f} h under alert<extra></extra>'
      : '%{y}<br>%{x}<br>%{z:.0f} alerts<extra></extra>';
    var L=lay(''); L.height=Math.max(360, 20*s.length+130);
    L.margin={l:120,r:24,t:10,b:62}; L.hovermode='closest';
    L.xaxis={showgrid:false, tickangle:-45}; L.yaxis={autorange:'reversed', showgrid:false};
    Plotly.react('chart-region',[{type:'heatmap',z:z,x:D.months,y:yl,colorscale:HEAT,
      reversescale:true,
      colorbar:{title:{text: heatMetric==='hours'?'hours':'alerts'}}, hovertemplate:ht}], L, CFG);
  }

  function renderAll(){ renderCount(); renderDuration(); renderHour(); renderWeekday(); renderHeatmap(); }

  document.getElementById('tk-count').textContent=D.takeaways.count;
  document.getElementById('tk-duration').textContent=D.takeaways.duration;
  document.getElementById('tk-hour').textContent=D.takeaways.hour;
  document.getElementById('tk-weekday').textContent=D.takeaways.weekday;
  document.getElementById('tk-region').textContent=D.takeaways.region;

  renderAll();
})();
"""


def build_report(payload: dict, out_dir: Path = OUTPUT_DIR) -> Path:
    """Assemble the interactive report into one self-contained HTML file. Plotly.js and the
    JSON data payload are embedded (no internet needed); every chart is rendered and
    re-aggregated in the browser from that payload, driven by the on-page controls."""
    import json
    from plotly.offline import get_plotlyjs

    out_dir.mkdir(parents=True, exist_ok=True)
    b = payload["banner"]
    meta = payload["meta"]

    cards = [
        (f"{b['total_alerts']:,}", "oblast-alerts recorded"),
        (f"{b['date_start']} – {b['date_end']}", "period covered"),
        (f"{b['median_min']:.0f} min", "typical alert (median)"),
        (b["busiest_month"], f"busiest month ({b['busiest_month_n']:,} alerts)"),
        (f"{b['n_oblasts']}", "oblasts analysed"),
    ]
    cards_html = "\n".join(
        f'<div class="card"><div class="num">{v}</div><div class="lbl">{l}</div></div>'
        for v, l in cards)

    header_html = (
        '<header class="rep">'
        '<h1>Air-raid alerts in Ukraine</h1>'
        f'<p>How the number, timing and duration of alerts changed, '
        f'{b["date_start"]} – {b["date_end"]} (local time).</p>'
        f'<p>Generated {meta.get("generated", "")} · Source: official @air_alert_ua dataset · '
        f'{b["total_alerts"]:,} oblast-alerts across {b["n_oblasts"]} oblasts.</p>'
        '</header>')

    # json.dumps is HTML-safe here (numeric/text payload); guard against a stray "</script>"
    data_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = (
        '<!DOCTYPE html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<title>Air-raid alerts in Ukraine — time-series analysis</title>\n'
        '<style>' + REPORT_CSS + '</style>\n'
        '<script type="text/javascript">' + get_plotlyjs() + '</script>\n'
        '</head><body><div class="wrap">\n'
        + header_html + '\n'
        + '<div class="banner">' + cards_html + '</div>\n'
        + BODY_HTML + '\n'
        + REPORT_NOTES_HTML + '\n'
        + '<footer class="rep">Built by air_raid_analysis.py · data &amp; method documented in '
          'the project README.</footer>\n'
        + '</div>\n'
        + '<script type="text/javascript">window.DATA=' + data_json + ';</script>\n'
        + '<script type="text/javascript">' + REPORT_JS + '</script>\n'
        + '</body></html>')

    out_path = out_dir / "air_raid_report.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def check_environment() -> int:
    """Verify required dependencies import correctly. Returns a process exit code."""
    missing = []
    for mod in ("pandas", "plotly", "requests"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"[FAIL] Missing dependencies: {', '.join(missing)}")
        print("       Install them with:  pip install -r requirements.txt")
        return 1
    import pandas, plotly, requests  # noqa: E401  (already verified above)
    print("[OK] Environment looks good.")
    print(f"     Python   {sys.version.split()[0]}")
    print(f"     pandas   {pandas.__version__}")
    print(f"     plotly   {plotly.__version__}")
    print(f"     requests {requests.__version__}")
    print(f"     Kyiv tz  {KYIV_TZ}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify environment/dependencies and exit")
    args = parser.parse_args(argv)

    if args.check:
        return check_environment()

    import datetime as _dt

    csv_path = download_data()
    clean = load_and_clean(csv_path)
    print(f"Cleaned: {len(clean):,} alert rows after exclusions/filtering.")

    res = compute_oblast_alerts(clean)
    print(f"Oblast-alerts: {len(res):,} intervals across {res['oblast'].nunique()} oblasts.")

    audit_path = write_long_alert_audit(res, clean)
    print(f"Long-alert audit -> {audit_path}")

    meta = {"generated": _dt.datetime.now().strftime("%d %b %Y")}
    payload = build_payload(res, meta)
    report_path = build_report(payload)
    print(f"Report written to {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
