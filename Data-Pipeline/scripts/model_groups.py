"""Write the model bias check's reported groups (#110, ADR-033) to Model-Development/config/slice_groups.csv.

The training image has neither geopandas nor the static layers (ADR-032), so the model side reads
each neighborhood's groups from that file. They are #32's groups, from bias_slicing's own
functions: people of color, limited English and flood zone, and whether the neighborhood is a
student area (#126). Poverty is not here: the gate's groups are poverty_groups.csv (ADR-026).

This is a script of its own, not part of a DVC stage, so writing the file never changes
Data-Pipeline/dvc.lock, whose sha256 is the data version. Run it on the host, with the static
layers loaded, after a change to the ACS groups, the student-area rule or the layers; a
Data-Pipeline test fails while the file is stale.
"""

import argparse
from pathlib import Path

import pandas as pd

from bias_slicing import load_settings, slice_groups, student_slices, unit_attributes

HEADER = ["neighborhood", "poc", "limited_english", "flood_zone", "student_area"]


def model_groups(units: pd.DataFrame, factor: float) -> pd.DataFrame:
    """Each neighborhood's group in the reported families, sorted by neighborhood. A neighborhood
    with no value in a family is "ungrouped"."""
    groups = slice_groups(units, "unit")
    students, _ = student_slices(units, factor)
    columns = {"poc": groups["poc_share"], "limited_english": groups["limited_english_share"],
               "flood_zone": groups["flood_zone"], "student_area": students["student_area"]}
    out = pd.DataFrame({name: series.astype(object).where(series.notna(), "ungrouped") for name, series in columns.items()})
    return out.rename_axis("neighborhood").sort_index().reset_index()[HEADER]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--static-dir", default="Data-Pipeline/data/interim/static")
    parser.add_argument("--config", default="Data-Pipeline/config/bias_slicing.csv")
    parser.add_argument("--out", default="Model-Development/config/slice_groups.csv")
    args = parser.parse_args(argv)
    table = model_groups(unit_attributes(args.static_dir), load_settings(args.config)["student_area_factor"])
    Path(args.out).write_text(table.to_csv(index=False, lineterminator="\n"), encoding="utf-8")
    print(f"wrote {len(table)} neighborhoods to {args.out}")


if __name__ == "__main__":
    main()
