# ADR-014: NWS alert history comes from the Iowa Environmental Mesonet archive

- Status: proposed
- Date: 2026-10-25
- Owner: piyush1420
- Phase: data

## Context

The feature table (#16) joins NWS alerts (watches, warnings and advisories) to each neighborhood and day. ADR-003 allows a day-level feature only if its source covers every training year, 2015 to 2023, and asks #12 to find an alert-history source or drop the alert feature. ADR-002 takes live alerts from the NWS API for the event DAG (#7).

Every NWS watch, warning and advisory carries a VTEC code: the alert type (`phenomena`, for example `WS` for winter storm), its level (`significance`: `W` warning, `A` watch, `Y` advisory, `S` statement), the issuing office and an event number. Alerts go to NWS forecast zones and counties (UGC codes). The NWS point lookup puts Boston City Hall in forecast zone MAZ015 (Suffolk) and county MAC025 (Suffolk County), office BOX; all of Boston is in Suffolk County.

The Iowa Environmental Mesonet (IEM) at Iowa State University archives these VTEC events: "All VTEC watch/warn" products from 12 Nov 2005, and tornado, severe thunderstorm and flash flood warnings by county from 1986 (https://mesonet.agron.iastate.edu/request/gis/watchwarn.phtml). An alert's start and end can change after it is first sent (extended, upgraded or cancelled); the time of its first bulletin does not. IEM serves the archive in two ways, and only one keeps the first bulletin:

- The per-area service (`https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py`) answers for one zone or county and gives each alert's start and end in their last state only.
- The bulk download (`https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py`, CSV with `accept=csv`) answers for a forecast office or a state, not a single zone, and also keeps the first bulletin. IEM defines its time columns (https://mesonet.agron.iastate.edu/info/datasets/vtec.html): `utc_issue` and `utc_expire` are the start and end in the last state; `utc_prodissue` (IEM's `INIT_ISS`) is the "timestamp of the NWS Text Product that started the event"; `utc_init_expire` (`INIT_EXP`) is "the expiration of the event denoted with the first issuance of the event"; `utc_updated` is when the NWS last changed the event. All are UTC. There is no column for the start as first announced.
- For one event at a time, IEM also lists every bulletin with its time and full text, including the VTEC line that gives that bulletin's action and times (`https://mesonet.agron.iastate.edu/json/vtec_event.py`).

### Evidence (queries of 2026-10-24)

Anyone can rerun these by opening the links. The NWS links need a browser or a client that sends a User-Agent.

- Boston's zone and county: https://api.weather.gov/points/42.3603,-71.058 returns `forecastZone` MAZ015, `county` MAC025 and `cwa` BOX.
- Alerts per year, from IEM's per-area service. Each count links to a CSV whose data rows are that year's alerts; the service selects alerts by their issuance time in UTC (`sts` and `ets`).

| year | split | zone MAZ015 | county MAC025 |
|---|---|---|---|
| 2015 | train | [70](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2015-01-01T00:00:00Z&ets=2016-01-01T00:00:00Z&fmt=csv) | [13](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2015-01-01T00:00:00Z&ets=2016-01-01T00:00:00Z&fmt=csv) |
| 2016 | train | [76](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2016-01-01T00:00:00Z&ets=2017-01-01T00:00:00Z&fmt=csv) | [20](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2016-01-01T00:00:00Z&ets=2017-01-01T00:00:00Z&fmt=csv) |
| 2017 | train | [88](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2017-01-01T00:00:00Z&ets=2018-01-01T00:00:00Z&fmt=csv) | [29](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2017-01-01T00:00:00Z&ets=2018-01-01T00:00:00Z&fmt=csv) |
| 2018 | train | [115](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2018-01-01T00:00:00Z&ets=2019-01-01T00:00:00Z&fmt=csv) | [39](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2018-01-01T00:00:00Z&ets=2019-01-01T00:00:00Z&fmt=csv) |
| 2019 | train | [69](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2019-01-01T00:00:00Z&ets=2020-01-01T00:00:00Z&fmt=csv) | [27](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2019-01-01T00:00:00Z&ets=2020-01-01T00:00:00Z&fmt=csv) |
| 2020 | train | [67](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2020-01-01T00:00:00Z&ets=2021-01-01T00:00:00Z&fmt=csv) | [39](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2020-01-01T00:00:00Z&ets=2021-01-01T00:00:00Z&fmt=csv) |
| 2021 | train | [72](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2021-01-01T00:00:00Z&ets=2022-01-01T00:00:00Z&fmt=csv) | [36](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2021-01-01T00:00:00Z&ets=2022-01-01T00:00:00Z&fmt=csv) |
| 2022 | train | [63](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2022-01-01T00:00:00Z&ets=2023-01-01T00:00:00Z&fmt=csv) | [11](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2022-01-01T00:00:00Z&ets=2023-01-01T00:00:00Z&fmt=csv) |
| 2023 | train | [64](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2023-01-01T00:00:00Z&ets=2024-01-01T00:00:00Z&fmt=csv) | [21](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2023-01-01T00:00:00Z&ets=2024-01-01T00:00:00Z&fmt=csv) |
| 2024 | validation | [85](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2024-01-01T00:00:00Z&ets=2025-01-01T00:00:00Z&fmt=csv) | [23](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2024-01-01T00:00:00Z&ets=2025-01-01T00:00:00Z&fmt=csv) |
| 2025 | test | [51](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2025-01-01T00:00:00Z&ets=2026-01-01T00:00:00Z&fmt=csv) | [13](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2025-01-01T00:00:00Z&ets=2026-01-01T00:00:00Z&fmt=csv) |
| 2026, to 2026-10-23 (partial year) | test to mid-2026, then live | [46](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2026-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv) | [7](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2026-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv) |

  The training years 2015 to 2023 hold 684 zone alerts and 235 county alerts, with no year missing. Counted by the year of each alert's first bulletin instead, 1 or 2 alerts fall in the neighboring year in 2017 to 2019 and 2024 to 2025; the totals are the same.
- All alerts from 2015-01-01 to 2026-10-24: zone MAZ015, 866 alerts ([JSON](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z), [CSV](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv)); county MAC025, 278 alerts ([CSV](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv)).
- Known storms, alerts active on the UTC day in zone MAZ015, from the full-range files:

| day | storm | zone alerts |
|---|---|---|
| 2015-01-27 | blizzard | blizzard watch and warning, winter storm warning, coastal flood watch and warning |
| 2018-01-04 | winter storm | blizzard warning, winter storm watch and warning, coastal flood watch and warning, high wind watch |
| 2018-03-02 | nor'easter | coastal flood watch and warning, high wind watch and warning, flood watch |
| 2022-01-29 | blizzard | blizzard warning, winter storm watch, coastal flood warning and advisory |

- The bulk download lists the same Boston alerts. Requested for office BOX one year at a time (for example [2015](https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py?accept=csv&wfo=BOX&sts=2015-01-01T00:00Z&ets=2016-01-01T00:00Z)) and kept to zone MAZ015 and county MAC025, one row per first bulletin, it matches the table above in every year (queries of 2026-10-25). An event issued for Boston a second time has a second row with its own first bulletin: 9 zone alerts in 2015 to 2026, for example winter weather advisory 18 of 2017.
- First bulletin versus last state, the 2015 blizzard warning in zone MAZ015 ([CSV](https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py?accept=csv&sts=2015-01-20T00:00Z&ets=2015-02-01T00:00Z&wfo=BOX&limitps=1&phenomena=BZ&significance=W), [all bulletins](https://mesonet.agron.iastate.edu/json/vtec_event.py?wfo=BOX&year=2015&phenomena=BZ&significance=W&etn=1)). It was first sent 2015-01-25 20:58 (`utc_prodissue`, also the first 12 digits of `product_id`) for a start at 2015-01-27 00:00 and an end at 2015-01-28 06:00 (`utc_init_expire`). Every bulletin through the evening of 26 January kept that end. The archive's last state ends it at 2015-01-27 23:37 (`utc_expire`), when it was cancelled for Boston as a winter storm warning took over. A feature built from `utc_expire` would know on 26 January about a cancellation made the next evening.
- How often the end changed after the first bulletin, from the bulk download (`utc_expire` against `utc_init_expire`): in zone MAZ015, 429 of the 684 alerts of 2015 to 2023 (62.7%), 360 ending earlier and 69 later; in county MAC025, 97 of 235 (41.3%), 89 earlier and 8 later. Over 2015 to 2026, 138 zone alerts end before they start.
- Lead time from first bulletin to start, zone MAZ015, 2015 to 2026 (median): winter storm watch 39.5 h, winter storm warning 14.9 h, blizzard warning 20.4 h, winter weather advisory 15.1 h, coastal flood warning 21.4 h, coastal flood advisory 17.7 h, wind advisory 13.2 h, high wind warning 17.9 h, heat advisory 19.3 h, flood watch 22.2 h. County severe thunderstorm, flash flood and tornado warnings start when first sent (0 h).
- The NWS API keeps no history: https://api.weather.gov/alerts?zone=MAZ015&start=2015-01-25T00:00:00Z&end=2015-01-28T00:00:00Z returns 0 alerts for the 2015 blizzard, and https://api.weather.gov/alerts?zone=MAZ015&start=2026-09-20T00:00:00Z&end=2026-09-30T00:00:00Z returns 0 as well, while IEM has alerts in both windows. Its manual (https://api.weather.gov/openapi.json) describes `/alerts` only as "Returns all alerts".

## Decision

- **Source:** the IEM archive of NWS VTEC events, read with its bulk download (`https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py`, `accept=csv`) for office BOX, keeping the rows of Boston's forecast zone MAZ015 and county MAC025. Of IEM's two services it is the one that keeps each alert's first bulletin and first-announced end. The per-area service stays as coverage evidence only: it lists the same Boston alerts, but only in their last state.
- **Coverage:** every year from 2015 to 2026 has Boston alerts, and the archive starts in 2005 (1986 by county for tornado, severe thunderstorm and flash flood warnings). The alert feature meets ADR-003 and is kept. The same archive covers validation (2024), test (2025 to mid-2026) and live, so history and live describe alerts with the same VTEC codes.
- **Known in advance:** an alert is known from its first bulletin, `utc_prodissue` (also the first 12 digits of `product_id`, UTC), and as known then it ends at `utc_init_expire`. `utc_expire`, `utc_updated` and `status` describe the last state and can hold what happened after the cutoff, so features do not use them.
- **Download and preparation:** #16 downloads the alert history and prepares it for the feature table (see Consequences).
- **Scope:** this ADR picks the source and the fields that are safe to use. Which alert types become features, how they map to storm groups, and how they become daily values belong to #16.

### Limits on preventing future-data leakage

- **No first-announced start.** `utc_issue` is the start in the last state. It did not move for the 2015 blizzard, but starts can move after the first bulletin; #16 checks how often from the bulletins before it relies on `utc_issue` for a day close to the cutoff.
- **Updates before the cutoff are missed.** An extension, early cancellation or upgrade sent after the first bulletin but before the cutoff is in neither the first bulletin nor the last state. The first-bulletin fields never use information from after the cutoff, but they can be out of date at it: an alert cancelled in the afternoon still counts with its first-announced end.
- **History and live differ slightly.** The live NWS feed (#7) shows each alert as last updated before the cutoff, while the history shows it as first sent. Rebuilding the exact state at the cutoff, the same as live, needs every bulletin: IEM's event service lists them with their VTEC lines, at one request per alert, about 920 for Boston's training years (684 zone and 235 county). #16 decides whether that accuracy is worth the extra download.
- **IEM's display advice is the leaky choice.** For display, IEM suggests an event's last end (`EXPIRE`); for features that is exactly the field that leaks.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **IEM bulk download, office BOX, Boston zone and county rows (chosen)** | $0 | Low: one request per year for office BOX, no key, then keep Boston's two areas | A university service, not the NWS: if it is down or changes its interface, history pulls fail until it is back; raw pulls kept under DVC protect what is already downloaded. First-bulletin fields can be out of date at the cutoff (see Limits) |
| IEM per-area service only | $0 | Low: one query per area and date range | Last state only: 62.7% of training-year zone alerts end at a different time than first announced, so features would know cancellations and extensions made after the cutoff. Kept as coverage evidence |
| IEM event service, every bulletin | $0 | Medium: one request per alert (about 920 for the training years) and VTEC parsing | Exact state at the cutoff, the same as live; #16 may add it on top of the bulk download if the first-bulletin fields prove too stale |
| NWS API `/alerts` | $0 | Low | No history: 0 alerts for the 2015 blizzard and for late September 2026; stays the live source (#7) |
| NOAA NCEI Storm Events Database | $0 | Medium | Records what happened, compiled after the event, not what was announced in advance; as a feature it would give away the outcome |
| Raw NWS text products, parsed ourselves | $0 | High: parse VTEC out of the text | Same information as the IEM archive, with more code to maintain and test |
| Drop the alert feature, live only | $0 | None | Loses a signal that is out 15 to 40 hours ahead for winter storms, coastal floods and wind |

## Consequences

- #16 may use alerts as a feature. Its leakage test (ADR-003) should count an alert for day D only if its first bulletin is before the forecast cutoff, and end it at its first-announced end (`utc_init_expire`). The last state is not safe: 62.7% of training-year zone alerts end at a different time than first announced, and 138 zone alerts, mostly watches upgraded or cancelled early, end before they start.
- County severe thunderstorm, flash flood and tornado warnings start when first sent, so none is known at an evening cutoff for the next day; their watches can be.
- Alert types change over time, as the 311 categories did. In zone MAZ015, freezing rain advisories appear only in 2015, blizzard watches only up to 2017, flash flood watches up to 2021 (flood watches after), excessive heat up to 2024 (extreme heat from 2025), and wind chill up to 2023 (a cold weather advisory in 2026); county snow squall warnings start in 2019. #16 needs a reviewed, versioned map from VTEC type to storm group, like `docs/data/category-map.csv`.
- Boston is one forecast zone, so zone alerts are the same for all 24 neighborhoods on a day. The alert feature varies by day, not by neighborhood.
- **#16 downloads and prepares the alert history.** It requests the bulk download for office BOX one year at a time, keeps each pull as published under DVC (#18, #19), and prepares one row per first bulletin for zone MAZ015 and county MAC025 with the fields above. #7 still lands live NWS alerts (ADR-002). The archive is public and needs no key, so #42's fresh-clone test can rerun the download.
- **Licence and credit.** NWS alerts are U.S. government work: "The information on National Weather Service (NWS) Web pages are in the public domain, unless specifically noted otherwise, and may be used without charge for any lawful purpose so long as you do not: 1) claim it is your own ..., 2) use it in a manner that implies an endorsement or affiliation with NOAA/NWS, or 3) modify its content and then present it as official government material." (https://www.weather.gov/disclaimer). The IEM says: "The materials found on this website are in the public domain and may be used freely by anyone for any lawful purpose. Attributing the Iowa Environmental Mesonet of Iowa State University would be appreciated." The same page gives the data "without any warranty of accuracy" (https://mesonet.agron.iastate.edu/disclaimer.php). The data card (#43) credits the NWS and the Iowa Environmental Mesonet of Iowa State University. Briefs and dashboards must not present alert-based values as official NWS warnings, in line with the spec: official warnings stay with the City and the NWS.
- Once accepted, this answers ADR-003's "Revisit when #12 finds alert history" and the spec's risk 4 (alert history needs a source). The spec is read-only here; its owner updates risk 4.

## Revisit when

- IEM changes or retires the bulk download or its first-bulletin columns (`utc_prodissue`, `utc_init_expire`), or Boston's count drops in a year with known storms.
- #16 finds that starts often move after the first bulletin, or switches to every bulletin (IEM's event service) for the exact state at the cutoff.
- The NWS changes Boston's zone or county codes, or renames alert types again.
- #16 finds that the alert feature adds nothing beyond the weather forecasts, or cannot pass the leakage test.
