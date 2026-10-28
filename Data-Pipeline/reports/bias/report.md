# Bias slicing on data and labels (#32)

Neighborhood-days 2015-01-01 to 2026-10-25: 103,584 cells; 216,947 storm-related requests, 1,063 without a location, 154 of those with a recorded neighborhood name that covers a neighborhood.
Significant gap: ratio under 0.8 (four-fifths rule) and p under 0.01.

Two limits:
- The system slice compares two different years, so it cannot separate the 311 system change from that year's weather and other changes.
- The tests treat neighborhood-days and requests as independent, but one storm raises many of them at once, so the p values are too small. Read the four-fifths ratio as the main signal and p as a check against tiny groups.

No 2025 population or ACS values (left out of per capita and ACS slices): Harbor Islands.

- **poverty_share:** higher (11): Allston, Brighton, Chinatown, Dorchester, Downtown, Fenway, Longwood, Mattapan, Mission Hill, Roxbury, South End; lower (12): Back Bay, Beacon Hill, Charlestown, East Boston, Hyde Park, Jamaica Plain, North End, Roslindale, South Boston, South Boston Waterfront, West End, West Roxbury
- **poc_share:** higher (11): Allston, Chinatown, Dorchester, East Boston, Fenway, Hyde Park, Jamaica Plain, Mattapan, Mission Hill, Roslindale, Roxbury; lower (12): Back Bay, Beacon Hill, Brighton, Charlestown, Downtown, Longwood, North End, South Boston, South Boston Waterfront, South End, West End, West Roxbury
- **limited_english_share:** higher (11): Brighton, Chinatown, Dorchester, Downtown, East Boston, Hyde Park, Mattapan, Mission Hill, Roslindale, Roxbury, South End; lower (12): Allston, Back Bay, Beacon Hill, Charlestown, Fenway, Jamaica Plain, Longwood, North End, South Boston, South Boston Waterfront, West End, West Roxbury
- **flood_zone:** in flood zone (10): Charlestown, Dorchester, Downtown, East Boston, Fenway, Harbor Islands, North End, South Boston, South Boston Waterfront, West End; not in flood zone (14): Allston, Back Bay, Beacon Hill, Brighton, Chinatown, Hyde Park, Jamaica Plain, Longwood, Mattapan, Mission Hill, Roslindale, Roxbury, South End, West Roxbury
- **system:** legacy era 2024-10-27 to 2025-10-25; migration era 2025-10-27 to 2026-10-25

## Significant gaps

| slice | metric | period | group | group value | compared with | their value | ratio | p |
|---|---|---|---|---|---|---|---|---|
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Allston | 9.9 | rest of the city | 24.8 | 0.4 | 0.0e+00 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Beacon Hill | 33.9 | rest of the city | 24.0 | 0.71 | 1.4e-67 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Brighton | 19.1 | rest of the city | 24.5 | 0.78 | 8.3e-132 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Charlestown | 35.7 | rest of the city | 23.8 | 0.67 | 5.7e-190 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Chinatown | 7.9 | rest of the city | 24.3 | 0.33 | 9.5e-188 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Fenway | 10.0 | rest of the city | 25.0 | 0.4 | 0.0e+00 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Hyde Park | 30.4 | rest of the city | 23.8 | 0.78 | 1.4e-106 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Jamaica Plain | 31.2 | rest of the city | 23.7 | 0.76 | 1.5e-174 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Longwood | 6.6 | rest of the city | 24.2 | 0.27 | 1.5e-198 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Mission Hill | 10.6 | rest of the city | 24.5 | 0.43 | 0.0e+00 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | Roslindale | 33.0 | rest of the city | 23.7 | 0.72 | 3.9e-180 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | South Boston | 31.2 | rest of the city | 23.7 | 0.76 | 5.0e-165 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | South Boston Waterfront | 13.3 | rest of the city | 24.2 | 0.55 | 8.9e-97 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | South End | 32.4 | rest of the city | 23.7 | 0.73 | 4.7e-187 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | West End | 9.5 | rest of the city | 24.3 | 0.39 | 2.1e-212 |
| neighborhood | requests per 1,000 residents a year | train 2015 to 2023 | West Roxbury | 39.9 | rest of the city | 23.4 | 0.59 | 0.0e+00 |
| neighborhood | share of storm requests with no location | train 2015 to 2023 | East Boston | 0.14% | rest of the city | 0.01% | 0.06 | 1.3e-08 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Allston | 2.26% | rest of the city | 3.49% | 0.65 | 3.7e-04 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Beacon Hill | 2.23% | rest of the city | 3.49% | 0.64 | 2.5e-04 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Brighton | 5.76% | rest of the city | 3.34% | 0.58 | 2.1e-12 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Chinatown | 0.41% | rest of the city | 3.57% | 0.12 | 5.0e-20 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Dorchester | 6.82% | rest of the city | 3.29% | 0.48 | 1.2e-24 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Downtown | 1.75% | rest of the city | 3.51% | 0.5 | 3.1e-07 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | East Boston | 4.66% | rest of the city | 3.38% | 0.73 | 2.1e-04 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Harbor Islands | 0.00% | rest of the city | 3.59% | 0.0 | 2.2e-25 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Hyde Park | 5.28% | rest of the city | 3.36% | 0.64 | 2.5e-08 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Jamaica Plain | 5.49% | rest of the city | 3.35% | 0.61 | 5.7e-10 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Longwood | 0.27% | rest of the city | 3.58% | 0.08 | 9.7e-22 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Mission Hill | 1.34% | rest of the city | 3.53% | 0.38 | 2.0e-10 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | North End | 1.95% | rest of the city | 3.50% | 0.56 | 7.0e-06 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Roslindale | 4.70% | rest of the city | 3.38% | 0.72 | 1.4e-04 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | Roxbury | 5.52% | rest of the city | 3.35% | 0.61 | 2.9e-10 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | South Boston | 5.59% | rest of the city | 3.34% | 0.6 | 7.6e-11 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | South Boston Waterfront | 0.96% | rest of the city | 3.55% | 0.27 | 6.2e-14 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | South End | 5.97% | rest of the city | 3.33% | 0.56 | 2.0e-14 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | West End | 0.93% | rest of the city | 3.55% | 0.26 | 2.8e-14 |
| neighborhood | surge rate of labeled days | train 2015 to 2023 | West Roxbury | 6.34% | rest of the city | 3.31% | 0.52 | 1.5e-18 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Allston | 9.5 | rest of the city | 21.0 | 0.45 | 2.4e-54 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Beacon Hill | 31.3 | rest of the city | 20.4 | 0.65 | 6.8e-12 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Charlestown | 29.0 | rest of the city | 20.3 | 0.7 | 2.8e-15 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Chinatown | 4.1 | rest of the city | 20.6 | 0.2 | 7.3e-29 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Downtown | 11.7 | rest of the city | 20.7 | 0.56 | 2.1e-17 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Fenway | 8.9 | rest of the city | 21.2 | 0.42 | 8.2e-83 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Jamaica Plain | 25.8 | rest of the city | 20.2 | 0.78 | 1.4e-14 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Longwood | 8.4 | rest of the city | 20.6 | 0.41 | 1.8e-12 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Mission Hill | 13.1 | rest of the city | 20.7 | 0.63 | 1.8e-14 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | North End | 14.6 | rest of the city | 20.6 | 0.71 | 9.1e-06 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | Roslindale | 28.2 | rest of the city | 20.2 | 0.71 | 2.8e-19 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | South Boston | 29.4 | rest of the city | 20.0 | 0.68 | 2.3e-34 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | South Boston Waterfront | 10.2 | rest of the city | 20.6 | 0.5 | 5.8e-13 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | South End | 27.7 | rest of the city | 20.1 | 0.73 | 8.0e-20 |
| neighborhood | requests per 1,000 residents a year | validate 2024 | West Roxbury | 37.4 | rest of the city | 19.7 | 0.53 | 1.2e-82 |
| neighborhood | surge rate of labeled days | validate 2024 | Brighton | 9.02% | rest of the city | 4.63% | 0.51 | 1.3e-04 |
| neighborhood | surge rate of labeled days | validate 2024 | Chinatown | 0.00% | rest of the city | 5.02% | 0.0 | 1.1e-05 |
| neighborhood | surge rate of labeled days | validate 2024 | Dorchester | 10.38% | rest of the city | 4.57% | 0.44 | 3.7e-07 |
| neighborhood | surge rate of labeled days | validate 2024 | East Boston | 9.29% | rest of the city | 4.62% | 0.5 | 4.4e-05 |
| neighborhood | surge rate of labeled days | validate 2024 | Harbor Islands | 0.00% | rest of the city | 5.02% | 0.0 | 1.1e-05 |
| neighborhood | surge rate of labeled days | validate 2024 | Longwood | 0.55% | rest of the city | 5.00% | 0.11 | 9.7e-05 |
| neighborhood | surge rate of labeled days | validate 2024 | North End | 1.91% | rest of the city | 4.94% | 0.39 | 8.1e-03 |
| neighborhood | surge rate of labeled days | validate 2024 | Roxbury | 9.56% | rest of the city | 4.61% | 0.48 | 1.5e-05 |
| neighborhood | surge rate of labeled days | validate 2024 | South Boston Waterfront | 1.09% | rest of the city | 4.98% | 0.22 | 6.8e-04 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Allston | 26.4 | rest of the city | 35.6 | 0.74 | 3.7e-33 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Beacon Hill | 46.6 | rest of the city | 35.0 | 0.75 | 3.0e-14 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Charlestown | 55.5 | rest of the city | 34.6 | 0.62 | 2.1e-80 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Chinatown | 17.2 | rest of the city | 35.3 | 0.49 | 2.2e-30 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Downtown | 28.0 | rest of the city | 35.3 | 0.79 | 1.2e-11 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Fenway | 11.3 | rest of the city | 36.7 | 0.31 | 0.0e+00 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Jamaica Plain | 50.2 | rest of the city | 34.2 | 0.68 | 4.1e-105 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Longwood | 9.7 | rest of the city | 35.4 | 0.27 | 1.1e-58 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | Mission Hill | 15.6 | rest of the city | 35.7 | 0.44 | 2.0e-107 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | South Boston | 50.2 | rest of the city | 34.3 | 0.68 | 4.4e-100 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | South Boston Waterfront | 21.7 | rest of the city | 35.3 | 0.61 | 1.5e-21 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | West End | 17.6 | rest of the city | 35.4 | 0.5 | 1.5e-41 |
| neighborhood | requests per 1,000 residents a year | 2025 onward | West Roxbury | 45.2 | rest of the city | 34.7 | 0.77 | 1.2e-35 |
| neighborhood | share of storm requests with no location | 2025 onward | Downtown | 3.45% | rest of the city | 0.02% | 0.01 | 9.2e-05 |
| neighborhood | surge rate of labeled days | 2025 onward | Chinatown | 2.11% | rest of the city | 4.68% | 0.45 | 2.0e-03 |
| neighborhood | surge rate of labeled days | 2025 onward | Dorchester | 9.80% | rest of the city | 4.34% | 0.44 | 4.3e-11 |
| neighborhood | surge rate of labeled days | 2025 onward | Harbor Islands | 0.00% | rest of the city | 4.77% | 0.0 | 8.7e-09 |
| neighborhood | surge rate of labeled days | 2025 onward | Longwood | 0.75% | rest of the city | 4.73% | 0.16 | 1.5e-06 |
| neighborhood | surge rate of labeled days | 2025 onward | Roslindale | 6.79% | rest of the city | 4.47% | 0.66 | 5.2e-03 |
| neighborhood | surge rate of labeled days | 2025 onward | West End | 1.81% | rest of the city | 4.69% | 0.39 | 5.1e-04 |
| neighborhood | surge rate of labeled days | 2025 onward | West Roxbury | 6.64% | rest of the city | 4.48% | 0.67 | 9.2e-03 |
| poverty_share | requests per 1,000 residents a year | train 2015 to 2023 | higher | 20.5 | lower | 29.2 | 0.7 | 0.0e+00 |
| poverty_share | share of storm requests with no location | train 2015 to 2023 | lower | 0.03% | higher | 0.10% | 0.31 | 5.6e-07 |
| poverty_share | requests per 1,000 residents a year | validate 2024 | higher | 16.5 | lower | 26.1 | 0.63 | 1.3e-169 |
| poverty_share | requests per 1,000 residents a year | 2025 onward | higher | 30.9 | lower | 41.0 | 0.75 | 3.3e-199 |
| poverty_share | share of storm requests with no location | 2025 onward | lower | 0.02% | higher | 0.21% | 0.12 | 5.2e-08 |
| poc_share | requests per 1,000 residents a year | validate 2024 | higher | 18.5 | lower | 24.0 | 0.77 | 4.1e-55 |
| poc_share | surge rate of labeled days | validate 2024 | lower | 4.35% | higher | 5.76% | 0.75 | 3.0e-03 |
| poc_share | surge rate of labeled days | 2025 onward | lower | 4.24% | higher | 5.35% | 0.79 | 1.3e-03 |
| limited_english_share | surge rate of labeled days | train 2015 to 2023 | lower | 3.06% | higher | 4.16% | 0.73 | 1.6e-14 |
| limited_english_share | surge rate of labeled days | validate 2024 | lower | 4.01% | higher | 6.14% | 0.65 | 8.0e-06 |
| flood_zone | share of storm requests with no location | train 2015 to 2023 | in flood zone | 0.05% | not in flood zone | 0.09% | 0.51 | 3.9e-03 |
| flood_zone | share of storm requests with no location | 2025 onward | in flood zone | 0.02% | not in flood zone | 0.22% | 0.08 | 6.5e-08 |
| system | requests per 1,000 residents a year | 2025-10-27 to 2026-10-25 and a year earlier | legacy era | 24.0 | migration era | 45.8 | 0.52 | 0.0e+00 |
| system | surge rate of labeled days | 2025-10-27 to 2026-10-25 and a year earlier | legacy era | 4.13% | migration era | 5.79% | 0.71 | 4.4e-07 |
| tract poverty_share | requests per 1,000 residents a year | train 2015 to 2023 | higher | 17.8 | lower | 31.0 | 0.57 | 0.0e+00 |
| tract poc_share | requests per 1,000 residents a year | train 2015 to 2023 | higher | 21.2 | lower | 27.3 | 0.78 | 0.0e+00 |
| tract limited_english_share | requests per 1,000 residents a year | train 2015 to 2023 | higher | 21.2 | lower | 27.7 | 0.76 | 0.0e+00 |
| tract poverty_share | requests per 1,000 residents a year | validate 2024 | higher | 14.6 | lower | 27.0 | 0.54 | 4.4e-293 |
| tract poc_share | requests per 1,000 residents a year | validate 2024 | higher | 17.2 | lower | 24.1 | 0.71 | 9.4e-93 |
| tract limited_english_share | requests per 1,000 residents a year | validate 2024 | higher | 18.1 | lower | 23.5 | 0.77 | 3.9e-57 |
| tract poverty_share | requests per 1,000 residents a year | 2025 onward | higher | 27.3 | lower | 43.6 | 0.63 | 0.0e+00 |
| tract poc_share | requests per 1,000 residents a year | 2025 onward | higher | 30.3 | lower | 40.4 | 0.75 | 2.4e-209 |
| tract limited_english_share | requests per 1,000 residents a year | 2025 onward | higher | 31.4 | lower | 39.8 | 0.79 | 2.8e-143 |

## Requests per 1,000 residents a year

| slice | group | train 2015 to 2023 | validate 2024 | 2025 onward | 2025-10-27 to 2026-10-25 and a year earlier |
|---|---|---|---|---|---|
| neighborhood | Allston | 9.9 | 9.5 | 26.4 |  |
| neighborhood | Back Bay | 26.3 | 19.5 | 37.2 |  |
| neighborhood | Beacon Hill | 33.9 | 31.3 | 46.6 |  |
| neighborhood | Brighton | 19.1 | 19.1 | 38.3 |  |
| neighborhood | Charlestown | 35.7 | 29.0 | 55.5 |  |
| neighborhood | Chinatown | 7.9 | 4.1 | 17.2 |  |
| neighborhood | Dorchester | 24.2 | 17.1 | 38.1 |  |
| neighborhood | Downtown | 19.4 | 11.7 | 28.0 |  |
| neighborhood | East Boston | 20.5 | 24.0 | 30.2 |  |
| neighborhood | Fenway | 10.0 | 8.9 | 11.3 |  |
| neighborhood | Hyde Park | 30.4 | 23.4 | 32.9 |  |
| neighborhood | Jamaica Plain | 31.2 | 25.8 | 50.2 |  |
| neighborhood | Longwood | 6.6 | 8.4 | 9.7 |  |
| neighborhood | Mattapan | 26.5 | 22.4 | 32.1 |  |
| neighborhood | Mission Hill | 10.6 | 13.1 | 15.6 |  |
| neighborhood | North End | 23.4 | 14.6 | 37.3 |  |
| neighborhood | Roslindale | 33.0 | 28.2 | 42.5 |  |
| neighborhood | Roxbury | 23.9 | 17.4 | 30.8 |  |
| neighborhood | South Boston | 31.2 | 29.4 | 50.2 |  |
| neighborhood | South Boston Waterfront | 13.3 | 10.2 | 21.7 |  |
| neighborhood | South End | 32.4 | 27.7 | 37.1 |  |
| neighborhood | West End | 9.5 | 16.6 | 17.6 |  |
| neighborhood | West Roxbury | 39.9 | 37.4 | 45.2 |  |
| neighborhood | all | 24.1 | 20.5 | 35.2 |  |
| poverty_share | all | 24.1 | 20.5 | 35.2 |  |
| poverty_share | higher | 20.5 | 16.5 | 30.9 |  |
| poverty_share | lower | 29.2 | 26.1 | 41.0 |  |
| poc_share | all | 24.1 | 20.5 | 35.2 |  |
| poc_share | higher | 22.5 | 18.5 | 32.6 |  |
| poc_share | lower | 27.0 | 24.0 | 39.8 |  |
| limited_english_share | all | 24.1 | 20.5 | 35.2 |  |
| limited_english_share | higher | 24.0 | 19.9 | 34.2 |  |
| limited_english_share | lower | 24.4 | 21.5 | 36.8 |  |
| flood_zone | all | 24.1 | 20.5 | 35.2 |  |
| flood_zone | in flood zone | 22.4 | 18.8 | 34.4 |  |
| flood_zone | not in flood zone | 25.5 | 21.9 | 35.8 |  |
| system | all |  |  |  | 34.9 |
| system | legacy era |  |  |  | 24.0 |
| system | migration era |  |  |  | 45.8 |
| tract poverty_share | all | 24.0 | 20.4 | 34.9 |  |
| tract poverty_share | higher | 17.8 | 14.6 | 27.3 |  |
| tract poverty_share | lower | 31.0 | 27.0 | 43.6 |  |
| tract poc_share | all | 24.1 | 20.5 | 35.1 |  |
| tract poc_share | higher | 21.2 | 17.2 | 30.3 |  |
| tract poc_share | lower | 27.3 | 24.1 | 40.4 |  |
| tract limited_english_share | all | 24.1 | 20.5 | 35.1 |  |
| tract limited_english_share | higher | 21.2 | 18.1 | 31.4 |  |
| tract limited_english_share | lower | 27.7 | 23.5 | 39.8 |  |
| tract flood_zone | all | 24.1 | 20.5 | 35.2 |  |
| tract flood_zone | in flood zone | 23.9 | 21.4 | 37.4 |  |
| tract flood_zone | not in flood zone | 24.2 | 20.3 | 34.7 |  |

## Share of storm requests with no location

| slice | group | train 2015 to 2023 | validate 2024 | 2025 onward | 2025-10-27 to 2026-10-25 |
|---|---|---|---|---|---|
| neighborhood | Beacon Hill | 0.00% | 0.00% | 0.00% |  |
| neighborhood | Brighton | 0.00% | 0.00% | 0.00% |  |
| neighborhood | Charlestown | 0.05% | 0.00% | 0.00% |  |
| neighborhood | Dorchester | 0.01% | 0.00% | 0.00% |  |
| neighborhood | East Boston | 0.14% | 0.09% | 0.04% |  |
| neighborhood | Hyde Park | 0.00% | 0.00% | 0.05% |  |
| neighborhood | Jamaica Plain | 0.00% | 0.09% | 0.05% |  |
| neighborhood | Roslindale | 0.00% | 0.00% | 0.00% |  |
| neighborhood | South Boston | 0.08% | 0.00% | 0.00% |  |
| neighborhood | South End | 0.00% | 0.00% | 0.00% |  |
| neighborhood | West Roxbury | 0.00% | 0.00% | 0.04% |  |
| neighborhood | all | 0.02% | 0.02% | 0.03% |  |
| neighborhood | Downtown |  |  | 3.45% |  |
| neighborhood | North End |  |  | 0.00% |  |
| neighborhood | South Boston Waterfront |  |  | 0.00% |  |
| neighborhood | West End |  |  | 0.00% |  |
| poverty_share | all | 0.06% | 0.06% | 0.12% |  |
| poverty_share | higher | 0.10% | 0.08% | 0.21% |  |
| poverty_share | lower | 0.03% | 0.04% | 0.02% |  |
| poc_share | all | 0.03% | 0.03% | 0.03% |  |
| poc_share | higher | 0.03% | 0.03% | 0.03% |  |
| poc_share | lower | 0.02% | 0.02% | 0.02% |  |
| limited_english_share | all | 0.03% | 0.02% | 0.03% |  |
| limited_english_share | higher | 0.03% | 0.01% | 0.03% |  |
| limited_english_share | lower | 0.03% | 0.04% | 0.04% |  |
| flood_zone | all | 0.07% | 0.07% | 0.13% |  |
| flood_zone | in flood zone | 0.05% | 0.04% | 0.02% |  |
| flood_zone | not in flood zone | 0.09% | 0.09% | 0.22% |  |
| system | all |  |  |  | 0.38% |
| system | legacy system |  |  |  | 0.36% |
| system | new system |  |  |  | 0.46% |

## Surge rate of labeled days

| slice | group | train 2015 to 2023 | validate 2024 | 2025 onward | 2025-10-27 to 2026-10-25 and a year earlier |
|---|---|---|---|---|---|
| neighborhood | Allston | 2.26% | 3.28% | 5.13% |  |
| neighborhood | Back Bay | 2.98% | 3.55% | 5.43% |  |
| neighborhood | Beacon Hill | 2.23% | 3.28% | 3.77% |  |
| neighborhood | Brighton | 5.76% | 9.02% | 5.73% |  |
| neighborhood | Charlestown | 4.01% | 5.74% | 4.98% |  |
| neighborhood | Chinatown | 0.41% | 0.00% | 2.11% |  |
| neighborhood | Dorchester | 6.82% | 10.38% | 9.80% |  |
| neighborhood | Downtown | 1.75% | 2.73% | 4.22% |  |
| neighborhood | East Boston | 4.66% | 9.29% | 4.98% |  |
| neighborhood | Fenway | 3.70% | 4.92% | 4.37% |  |
| neighborhood | Harbor Islands | 0.00% | 0.00% | 0.00% |  |
| neighborhood | Hyde Park | 5.28% | 5.74% | 5.73% |  |
| neighborhood | Jamaica Plain | 5.49% | 6.83% | 6.18% |  |
| neighborhood | Longwood | 0.27% | 0.55% | 0.75% |  |
| neighborhood | Mattapan | 3.60% | 4.10% | 4.68% |  |
| neighborhood | Mission Hill | 1.34% | 2.73% | 3.62% |  |
| neighborhood | North End | 1.95% | 1.91% | 4.37% |  |
| neighborhood | Roslindale | 4.70% | 6.56% | 6.79% |  |
| neighborhood | Roxbury | 5.52% | 9.56% | 5.43% |  |
| neighborhood | South Boston | 5.59% | 7.10% | 5.43% |  |
| neighborhood | South Boston Waterfront | 0.96% | 1.09% | 2.87% |  |
| neighborhood | South End | 5.97% | 7.38% | 4.83% |  |
| neighborhood | West End | 0.93% | 2.46% | 1.81% |  |
| neighborhood | West Roxbury | 6.34% | 7.38% | 6.64% |  |
| neighborhood | all | 3.44% | 4.82% | 4.57% |  |
| poverty_share | all | 3.59% | 5.02% | 4.77% |  |
| poverty_share | higher | 3.40% | 4.97% | 4.61% |  |
| poverty_share | lower | 3.76% | 5.08% | 4.91% |  |
| poc_share | all | 3.59% | 5.02% | 4.77% |  |
| poc_share | higher | 3.98% | 5.76% | 5.35% |  |
| poc_share | lower | 3.23% | 4.35% | 4.24% |  |
| limited_english_share | all | 3.59% | 5.02% | 4.77% |  |
| limited_english_share | higher | 4.16% | 6.14% | 5.27% |  |
| limited_english_share | lower | 3.06% | 4.01% | 4.31% |  |
| flood_zone | all | 3.44% | 4.82% | 4.57% |  |
| flood_zone | in flood zone | 3.04% | 4.56% | 4.28% |  |
| flood_zone | not in flood zone | 3.72% | 5.00% | 4.77% |  |
| system | all |  |  |  | 4.96% |
| system | legacy era |  |  |  | 4.13% |
| system | migration era |  |  |  | 5.79% |

## Share of days labeled

| slice | group | train 2015 to 2023 | validate 2024 | 2025 onward | 2025-10-27 to 2026-10-25 and a year earlier |
|---|---|---|---|---|---|
| neighborhood | Allston | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Back Bay | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Beacon Hill | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Brighton | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Charlestown | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Chinatown | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Dorchester | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Downtown | 88.74% | 100.00% | 100.00% |  |
| neighborhood | East Boston | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Fenway | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Harbor Islands | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Hyde Park | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Jamaica Plain | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Longwood | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Mattapan | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Mission Hill | 88.74% | 100.00% | 100.00% |  |
| neighborhood | North End | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Roslindale | 88.74% | 100.00% | 100.00% |  |
| neighborhood | Roxbury | 88.74% | 100.00% | 100.00% |  |
| neighborhood | South Boston | 88.74% | 100.00% | 100.00% |  |
| neighborhood | South Boston Waterfront | 88.74% | 100.00% | 100.00% |  |
| neighborhood | South End | 88.74% | 100.00% | 100.00% |  |
| neighborhood | West End | 88.74% | 100.00% | 100.00% |  |
| neighborhood | West Roxbury | 88.74% | 100.00% | 100.00% |  |
| neighborhood | all | 88.74% | 100.00% | 100.00% |  |
| poverty_share | all | 88.74% | 100.00% | 100.00% |  |
| poverty_share | higher | 88.74% | 100.00% | 100.00% |  |
| poverty_share | lower | 88.74% | 100.00% | 100.00% |  |
| poc_share | all | 88.74% | 100.00% | 100.00% |  |
| poc_share | higher | 88.74% | 100.00% | 100.00% |  |
| poc_share | lower | 88.74% | 100.00% | 100.00% |  |
| limited_english_share | all | 88.74% | 100.00% | 100.00% |  |
| limited_english_share | higher | 88.74% | 100.00% | 100.00% |  |
| limited_english_share | lower | 88.74% | 100.00% | 100.00% |  |
| flood_zone | all | 88.74% | 100.00% | 100.00% |  |
| flood_zone | in flood zone | 88.74% | 100.00% | 100.00% |  |
| flood_zone | not in flood zone | 88.74% | 100.00% | 100.00% |  |
| system | all |  |  |  | 100.00% |
| system | legacy era |  |  |  | 100.00% |
| system | migration era |  |  |  | 100.00% |

## Recorded neighborhood names used for requests without a location

| recorded name | neighborhoods it covers |
|---|---|
| Allston | Allston, Brighton |
| Allston / Brighton | Allston, Brighton |
| Back Bay | Back Bay, Fenway, South End |
| Beacon Hill | Beacon Hill |
| Brighton | Brighton |
| Charlestown | Charlestown |
| Chinatown | Chinatown, Downtown |
| Dorchester | Dorchester |
| Downtown | Downtown |
| Downtown / Financial District | Chinatown, Downtown, North End, West End |
| East Boston | East Boston |
| Fenway | Fenway, Jamaica Plain |
| Fenway / Kenmore / Audubon Circle / Longwood | Fenway, Longwood |
| Greater Mattapan | Dorchester, Mattapan |
| Hyde Park | Hyde Park |
| Jamaica Plain | Jamaica Plain |
| Longwood | Jamaica Plain, Longwood, Mission Hill |
| Mattapan | Hyde Park, Mattapan |
| Mission Hill | Mission Hill, Roxbury |
| North End | North End |
| Roslindale | Roslindale |
| Roxbury | Dorchester, Roxbury, South End |
| South Boston | South Boston |
| South Boston / South Boston Waterfront | South Boston, South Boston Waterfront |
| South Boston Waterfront | South Boston Waterfront |
| South End | South End |
| West End | West End |
| West Roxbury | West Roxbury |
