# ADR-014: NWS alert history comes from the Iowa Environmental Mesonet archive

- Status: proposed (review by Punith on the PR)
- Date: 2026-10-24
- Owner: piyush1420
- Phase: data

## Context

The feature table (#16) joins NWS alerts (watches, warnings and advisories) to each neighborhood and day. ADR-003 (PR #67) allows a day-level feature only if its source covers every training year, 2015 to 2023, and asks #12 to find an alert-history source or drop the alert feature. ADR-002 (PR #67) takes live alerts from the NWS API for the event DAG (#7).

Every NWS watch, warning and advisory carries a VTEC code: the alert type (`phenomena`, for example `WS` for winter storm), its level (`significance`: `W` warning, `A` watch, `Y` advisory, `S` statement), the issuing office and an event number. Alerts go to NWS forecast zones and counties (UGC codes). The NWS point lookup puts Boston City Hall in forecast zone MAZ015 (Suffolk) and county MAC025 (Suffolk County), office BOX; all of Boston is in Suffolk County.

The Iowa Environmental Mesonet (IEM) at Iowa State University archives these VTEC events: "All VTEC watch/warn" products from 12 Nov 2005, and tornado, severe thunderstorm and flash flood warnings by county from 1986 (https://mesonet.agron.iastate.edu/request/gis/watchwarn.phtml). Each record is the last state of an alert: its start and end can change after it is first sent (extended, upgraded or cancelled), but the time of its first bulletin does not (https://mesonet.agron.iastate.edu/info/datasets/vtec.html).

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

  The training years 2015 to 2023 hold 684 zone alerts and 235 county alerts, with no year missing. Counted by the year of each alert's first bulletin instead, 1 or 2 alerts fall in the neighboring year in 2017 to 2019 and 2024 to 2025; the totals are the same.
- All alerts from 2015-01-01 to 2026-10-24: zone MAZ015, 866 alerts ([JSON](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z), [CSV](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAZ015&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv)); county MAC025, 278 alerts ([CSV](https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py?ugc=MAC025&sts=2015-01-01T00:00:00Z&ets=2026-10-24T00:00:00Z&fmt=csv)).
- Known storms, alerts active on the UTC day in zone MAZ015, from the full-range files:

| day | storm | zone alerts |
|---|---|---|
| 2015-01-27 | blizzard | blizzard watch and warning, winter storm warning, coastal flood watch and warning |
| 2018-01-04 | winter storm | blizzard warning, winter storm watch and warning, coastal flood watch and warning, high wind watch |
| 2018-03-02 | nor'easter | coastal flood watch and warning, high wind watch and warning, flood watch |
| 2022-01-29 | blizzard | blizzard warning, winter storm watch, coastal flood warning and advisory |

- First bulletin: the first 12 digits of `product_id` (UTC). For the 2015 blizzard they equal `utc_prodissue` in IEM's bulk download ([CSV](https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py?accept=csv&sts=2015-01-20T00:00Z&ets=2015-02-01T00:00Z&location_group=wfo&wfo=BOX&limitps=1&phenomena=BZ,BZ,WS,WS&significance=W,A,W,A)). The blizzard warning was first sent 2015-01-25 20:58 for a start at 2015-01-27 00:00, and was cancelled at 2015-01-27 23:37 when a winter storm warning took over.
- Lead time from first bulletin to start, zone MAZ015, 2015 to 2026 (median): winter storm watch 39.5 h, winter storm warning 14.9 h, blizzard warning 20.4 h, winter weather advisory 15.1 h, coastal flood warning 21.4 h, coastal flood advisory 17.7 h, wind advisory 13.2 h, high wind warning 17.9 h, heat advisory 19.3 h, flood watch 22.2 h. County severe thunderstorm, flash flood and tornado warnings start when first sent (0 h).
- The NWS API keeps no history: https://api.weather.gov/alerts?zone=MAZ015&start=2015-01-25T00:00:00Z&end=2015-01-28T00:00:00Z returns 0 alerts for the 2015 blizzard, and https://api.weather.gov/alerts?zone=MAZ015&start=2026-09-20T00:00:00Z&end=2026-09-30T00:00:00Z returns 0 as well, while IEM has alerts in both windows. Its manual (https://api.weather.gov/openapi.json) describes `/alerts` only as "Returns all alerts".

## Decision

- **Source:** the IEM archive of NWS VTEC events, read with its per-area service (`https://mesonet.agron.iastate.edu/json/vtec_events_byugc.py`), for Boston's forecast zone MAZ015 and county MAC025.
- **Coverage:** every year from 2015 to 2026 has Boston alerts, and the archive starts in 2005 (1986 by county for tornado, severe thunderstorm and flash flood warnings). The alert feature meets ADR-003 and is kept. The same archive covers validation (2024), test (2025 to mid-2026) and live, so history and live describe alerts with the same VTEC codes.
- **Known in advance:** an alert counts as known from its first bulletin (the first 12 digits of `product_id`, UTC). Its start and end are its last state and can change after any cutoff.
- **Scope:** this ADR picks the source. Which alert types become features, how they map to storm groups, and how they become daily values belong to #16.

## Alternatives considered
| Option | Cost | Effort | Failure behavior |
|---|---|---|---|
| **IEM VTEC archive, Boston zone and county (chosen)** | $0 | Low: one query per area and date range, no key | A university service, not the NWS: if it is down or changes its interface, history pulls fail until it is back; raw pulls kept under DVC protect what is already downloaded |
| NWS API `/alerts` | $0 | Low | No history: 0 alerts for the 2015 blizzard and for late September 2026; stays the live source (#7) |
| NOAA NCEI Storm Events Database | $0 | Medium | Records what happened, compiled after the event, not what was announced in advance; as a feature it would give away the outcome |
| Raw NWS text products, parsed ourselves | $0 | High: parse VTEC out of the text | Same information as the IEM archive, with more code to maintain and test |
| Drop the alert feature, live only | $0 | None | Loses a signal that is out 15 to 40 hours ahead for winter storms, coastal floods and wind |

## Consequences

- #16 may use alerts as a feature. Its leakage test (ADR-003) should count an alert for day D only if its first bulletin is before the forecast cutoff. Start and end alone are not safe: 138 zone alerts in the archive, mostly watches that were upgraded or cancelled early, end before they start.
- County severe thunderstorm, flash flood and tornado warnings start when first sent, so none is known at an evening cutoff for the next day; their watches can be.
- Alert types change over time, as the 311 categories did. In zone MAZ015, freezing rain advisories appear only in 2015, blizzard watches only up to 2017, flash flood watches up to 2021 (flood watches after), excessive heat up to 2024 (extreme heat from 2025), and wind chill up to 2023 (a cold weather advisory in 2026); county snow squall warnings start in 2019. #16 needs a reviewed, versioned map from VTEC type to storm group, like `docs/data/category-map.csv`.
- Boston is one forecast zone, so zone alerts are the same for all 24 neighborhoods on a day. The alert feature varies by day, not by neighborhood.
- **No open issue downloads the 2015-onward alert history.** #7 lands live NWS alerts (ADR-002), and #16 uses alerts but has no download step. The team needs to assign the download, with its raw pulls versioned by DVC (#18, #19), before #16 builds the alert feature. The archive is public and needs no key, so #42's fresh-clone test can rerun it.
- **Licence and credit.** NWS alerts are U.S. government work: "The information on National Weather Service (NWS) Web pages are in the public domain, unless specifically noted otherwise, and may be used without charge for any lawful purpose so long as you do not: 1) claim it is your own ..., 2) use it in a manner that implies an endorsement or affiliation with NOAA/NWS, or 3) modify its content and then present it as official government material." (https://www.weather.gov/disclaimer). The IEM says: "The materials found on this website are in the public domain and may be used freely by anyone for any lawful purpose. Attributing the Iowa Environmental Mesonet of Iowa State University would be appreciated." The same page gives the data "without any warranty of accuracy" (https://mesonet.agron.iastate.edu/disclaimer.php). The data card (#43) credits the NWS and the Iowa Environmental Mesonet of Iowa State University. Briefs and dashboards must not present alert-based values as official NWS warnings, in line with the spec: official warnings stay with the City and the NWS.
- Once accepted, this answers ADR-003's "Revisit when #12 finds alert history" and the spec's risk 4 (alert history needs a source). The spec is read-only here; its owner updates risk 4.

## Revisit when

- IEM changes or retires the per-area service, or Boston's count drops in a year with known storms.
- The NWS changes Boston's zone or county codes, or renames alert types again.
- #16 finds that the alert feature adds nothing beyond the weather forecasts, or cannot pass the leakage test.
