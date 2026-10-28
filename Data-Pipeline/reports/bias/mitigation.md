# Bias mitigation: self-relative 311 lags (#33, ADR-025)

90,091 labeled neighborhood-days with every lag version, 3,554 of them surge days. A gap is a lower group mean under 0.8 times the higher one (four-fifths rule).

## Gaps by version (every slice, period, storm group and window)

| days | version | gaps | comparisons |
|---|---|---|---|
| all labeled days | raw | 52 | 96 |
| all labeled days | per capita | 45 | 96 |
| all labeled days | self-relative | 0 | 96 |
| surge days | raw | 53 | 96 |
| surge days | per capita | 64 | 96 |
| surge days | self-relative | 7 | 96 |

## 7-day sums, all labeled days (lower group mean vs higher, ratio)

| slice | storm_group | version | train 2015 to 2023 | validate 2024 | 2025 onward |
|---|---|---|---|---|---|
| poverty_share | snow_ice | raw | 5.35 vs 5.49 (0.97) | 3.34 vs 3.82 (0.88) | 13.71 vs 16.38 (0.84) |
| poverty_share | snow_ice | per capita | 0.12 vs 0.20 (0.62, gap) | 0.08 vs 0.14 (0.57, gap) | 0.36 vs 0.52 (0.69, gap) |
| poverty_share | snow_ice | self-relative | 1.09 vs 1.11 (0.99) | 1.63 vs 1.73 (0.94) | 1.79 vs 1.85 (0.97) |
| poverty_share | tree | raw | 6.22 vs 6.41 (0.97) | 7.77 vs 7.84 (0.99) | 5.54 vs 5.75 (0.96) |
| poverty_share | tree | per capita | 0.14 vs 0.24 (0.59, gap) | 0.18 vs 0.30 (0.61, gap) | 0.12 vs 0.21 (0.58, gap) |
| poverty_share | tree | self-relative | 1.23 vs 1.25 (0.98) | 1.30 vs 1.34 (0.97) | 0.88 vs 0.92 (0.97) |
| poc_share | snow_ice | raw | 4.23 vs 6.71 (0.63, gap) | 2.89 vs 4.35 (0.66, gap) | 11.50 vs 18.79 (0.61, gap) |
| poc_share | snow_ice | per capita | 0.15 vs 0.17 (0.88) | 0.10 vs 0.12 (0.87) | 0.41 vs 0.47 (0.88) |
| poc_share | snow_ice | self-relative | 1.09 vs 1.11 (0.99) | 1.59 vs 1.79 (0.89) | 1.77 vs 1.88 (0.95) |
| poc_share | tree | raw | 5.32 vs 7.42 (0.72, gap) | 6.44 vs 9.30 (0.69, gap) | 4.74 vs 6.64 (0.71, gap) |
| poc_share | tree | per capita | 0.17 vs 0.22 (0.75, gap) | 0.21 vs 0.27 (0.80, gap) | 0.15 vs 0.19 (0.76, gap) |
| poc_share | tree | self-relative | 1.22 vs 1.25 (0.98) | 1.29 vs 1.35 (0.96) | 0.89 vs 0.91 (0.98) |
| limited_english_share | snow_ice | raw | 4.11 vs 6.84 (0.60, gap) | 2.95 vs 4.28 (0.69, gap) | 11.25 vs 19.06 (0.59, gap) |
| limited_english_share | snow_ice | per capita | 0.16 vs 0.17 (0.97) | 0.10 vs 0.11 (0.89) | 0.42 vs 0.46 (0.92) |
| limited_english_share | snow_ice | self-relative | 1.10 vs 1.11 (0.99) | 1.57 vs 1.81 (0.87) | 1.76 vs 1.89 (0.93) |
| limited_english_share | tree | raw | 4.86 vs 7.92 (0.61, gap) | 5.67 vs 10.15 (0.56, gap) | 4.46 vs 6.95 (0.64, gap) |
| limited_english_share | tree | per capita | 0.19 vs 0.20 (0.91) | 0.24 vs 0.24 (0.97) | 0.16 vs 0.18 (0.87) |
| limited_english_share | tree | self-relative | 1.22 vs 1.26 (0.97) | 1.27 vs 1.37 (0.93) | 0.89 vs 0.91 (0.98) |
| flood_zone | snow_ice | raw | 5.14 vs 5.84 (0.88) | 3.34 vs 3.98 (0.84) | 13.57 vs 17.19 (0.79, gap) |
| flood_zone | snow_ice | per capita | 0.16 vs 0.17 (0.95) | 0.10 vs 0.11 (0.92) | 0.42 vs 0.47 (0.91) |
| flood_zone | snow_ice | self-relative | 1.10 vs 1.10 (1.00) | 1.62 vs 1.78 (0.91) | 1.80 vs 1.85 (0.97) |
| flood_zone | tree | raw | 6.27 vs 6.36 (0.99) | 7.79 vs 7.84 (0.99) | 5.57 vs 5.70 (0.98) |
| flood_zone | tree | per capita | 0.16 vs 0.22 (0.76, gap) | 0.21 vs 0.26 (0.79, gap) | 0.14 vs 0.19 (0.70, gap) |
| flood_zone | tree | self-relative | 1.23 vs 1.24 (0.99) | 1.31 vs 1.33 (0.98) | 0.85 vs 0.93 (0.92) |

## 7-day sums, surge days (lower group mean vs higher, ratio)

| slice | storm_group | version | train 2015 to 2023 | validate 2024 | 2025 onward |
|---|---|---|---|---|---|
| poverty_share | snow_ice | raw | 23.27 vs 29.72 (0.78, gap) | 19.61 vs 23.27 (0.84) | 127.49 vs 175.75 (0.73, gap) |
| poverty_share | snow_ice | per capita | 0.57 vs 0.83 (0.69, gap) | 0.35 vs 0.70 (0.51, gap) | 3.50 vs 4.83 (0.73, gap) |
| poverty_share | snow_ice | self-relative | 2.48 vs 2.83 (0.88) | 7.79 vs 8.93 (0.87) | 9.60 vs 10.12 (0.95) |
| poverty_share | tree | raw | 9.90 vs 10.68 (0.93) | 11.40 vs 12.04 (0.95) | 4.33 vs 4.38 (0.99) |
| poverty_share | tree | per capita | 0.19 vs 0.32 (0.60, gap) | 0.21 vs 0.37 (0.57, gap) | 0.07 vs 0.14 (0.46, gap) |
| poverty_share | tree | self-relative | 1.55 vs 1.72 (0.90) | 1.35 vs 1.46 (0.92) | 0.78 vs 0.81 (0.95) |
| poc_share | snow_ice | raw | 22.07 vs 29.84 (0.74, gap) | 17.60 vs 24.78 (0.71, gap) | 120.20 vs 175.36 (0.69, gap) |
| poc_share | snow_ice | per capita | 0.63 vs 0.81 (0.77, gap) | 0.53 vs 0.54 (0.99) | 3.66 vs 4.86 (0.75, gap) |
| poc_share | snow_ice | self-relative | 2.61 vs 2.67 (0.98) | 7.19 vs 9.38 (0.77, gap) | 9.52 vs 10.22 (0.93) |
| poc_share | tree | raw | 9.44 vs 10.97 (0.86) | 8.74 vs 14.14 (0.62, gap) | 3.71 vs 4.91 (0.76, gap) |
| poc_share | tree | per capita | 0.22 vs 0.31 (0.70, gap) | 0.29 vs 0.30 (0.95) | 0.09 vs 0.13 (0.71, gap) |
| poc_share | tree | self-relative | 1.60 vs 1.68 (0.95) | 1.26 vs 1.53 (0.82) | 0.80 vs 0.80 (1.00) |
| limited_english_share | snow_ice | raw | 20.63 vs 30.65 (0.67, gap) | 21.50 vs 21.60 (1.00) | 114.44 vs 181.36 (0.63, gap) |
| limited_english_share | snow_ice | per capita | 0.65 vs 0.79 (0.82) | 0.44 vs 0.66 (0.67, gap) | 3.85 vs 4.62 (0.83) |
| limited_english_share | snow_ice | self-relative | 2.56 vs 2.70 (0.95) | 7.87 vs 8.76 (0.90) | 9.63 vs 10.04 (0.96) |
| limited_english_share | tree | raw | 8.86 vs 11.37 (0.78, gap) | 7.57 vs 14.64 (0.52, gap) | 4.02 vs 4.64 (0.87) |
| limited_english_share | tree | per capita | 0.23 vs 0.30 (0.79, gap) | 0.29 vs 0.30 (0.96) | 0.08 vs 0.13 (0.62, gap) |
| limited_english_share | tree | self-relative | 1.59 vs 1.68 (0.95) | 1.31 vs 1.48 (0.89) | 0.76 vs 0.84 (0.90) |
| flood_zone | snow_ice | raw | 24.10 vs 29.79 (0.81) | 21.50 vs 21.57 (1.00) | 129.02 vs 182.20 (0.71, gap) |
| flood_zone | snow_ice | per capita | 0.68 vs 0.77 (0.89) | 0.47 vs 0.58 (0.81) | 3.92 vs 4.68 (0.84) |
| flood_zone | snow_ice | self-relative | 2.62 vs 2.67 (0.98) | 7.87 vs 9.19 (0.86) | 9.51 vs 10.36 (0.92) |
| flood_zone | tree | raw | 9.98 vs 10.73 (0.93) | 10.95 vs 12.84 (0.85) | 4.07 vs 4.79 (0.85) |
| flood_zone | tree | per capita | 0.20 vs 0.30 (0.69, gap) | 0.24 vs 0.33 (0.72, gap) | 0.08 vs 0.13 (0.59, gap) |
| flood_zone | tree | self-relative | 1.50 vs 1.73 (0.87) | 1.40 vs 1.42 (0.99) | 0.77 vs 0.81 (0.95) |
