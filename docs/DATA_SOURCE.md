# Data source note

## What "official" means here

The analysis uses the **official** CSV from the open repository
[`Vadimkin/ukrainian-air-raid-sirens-dataset`](https://github.com/Vadimkin/ukrainian-air-raid-sirens-dataset)
(`datasets/official_data_en.csv`).

That repository is a *collector*, not the origin of the data. Its generation script
(`processors/official_channel_processor.py`) reads messages from a single Telegram channel:

> **`@air_alert_ua`** — "Повітряна тривога" / *Air Alert Ukraine*, the official national
> air-raid notification channel.

This is the same official alert feed that the public siren/notification system broadcasts, so each
row corresponds to a real activation ("Повітряна тривога") and deactivation ("Відбій тривоги")
message published by the state alert system. That is the sense in which we call the data
**official / authoritative**: it mirrors the government alert channel's own messages, rather than being
a third party's reconstruction or estimate.

### Official / government references

A note on precision: I did **not** find a `.gov.ua` page that links the exact `t.me/air_alert_ua`
handle. The channel is the public Telegram arm — operated by Ajax Systems and launched 15 March 2022 —
of Ukraine's official "Повітряна тривога" (Air Alert) alert *system*. What government sources do
document is that system and how its signals originate, which is what gives the channel its authority:

- **Cabinet of Ministers of Ukraine (kmu.gov.ua)** — official announcement of the move to
  **district-level (raion) air-raid alerts**, which explains the granularity shift visible in the data:
  <https://www.kmu.gov.ua/en/news/yuliia-svyrydenko-zaprovadzhuiemo-novyi-pidkhid-do-oholoshennia-povitrianykh-tryvoh-poraionne-opovishchennia>
- **Lviv Regional Military Administration (loda.gov.ua)** — describes the official "Air Alarm" app and
  confirms signals come **first-hand from regional military administrations**, developed with support of
  the **Ministry of Digital Transformation**: <https://loda.gov.ua/en/services/text/18131>
- **UNITED24 (u24.gov.ua)** — the government's official platform, on the official **Air Alert app**:
  <https://u24.gov.ua/news/air_raid_alert>

In short: the alert *signals* are issued by Ukrainian state bodies (regional military administrations /
Air Force / ДСНС) through the official Air Alert system; `@air_alert_ua` is the Telegram broadcast of
that system, and Vadimkin's CSV is a faithful scrape of it.

What the collector adds on top of the raw messages is light and transparent: it parses each message
into a structured row, attaches the administrative location (oblast / raion / hromada) from Ukraine's
official administrative hierarchy, and — when an alert's "all-clear" message never arrives — caps that
alert at +1 hour rather than leaving it open. All times are stored in **UTC**.

## Coverage and known limits

- **Window:** records begin **15 March 2022** (first siren message in the channel) and update daily.
- **Granularity:** alerts were issued at **oblast** level early on, with **raion** (district) and
  **hromada** (community) level messages appearing increasingly over time. Every row still carries its
  parent `oblast`, so oblast-level aggregation is consistent across the whole window.
- **Permanent sirens are excluded by the source:** **Luhansk** (continuous since Apr 2022) and
  **Crimea** (continuous since Dec 2022) are not represented as ongoing alerts. In this dataset Luhansk
  appears in only 2 stray records and Crimea/Sevastopol not at all.
- **Occupied territories:** the data contains **no explicit "occupied" label**. Occupied areas simply
  produce **absent or sporadic** records (the alert channel cannot reliably cover them). Our analysis
  therefore excludes Luhansk and Crimea outright, and treats sparse/non-systemic data from occupied
  parts of other oblasts as unreliable rather than as "zero alerts."

## Columns

`oblast, raion, hromada, level, started_at, finished_at, source`

## Attribution

Data collected and published by Vadym Klymenko (`Vadimkin`) under the linked repository; underlying
alert messages are from the official `@air_alert_ua` channel. This note will be folded into the
project README.
