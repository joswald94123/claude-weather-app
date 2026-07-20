# Research Index

This folder holds vendored references or source notes that matter to this repo.

## Current Contents

- `../assets/manuals/PIM_TBM960E0R1_DRAFT.pdf` is the vendored Daher TBM 960 performance source.
- `../assets/pim_tables_snapshot.json` is the deterministic, hash-gated parse used at runtime.
- `../assets/faa_nasr_fallback.json.gz` contains the current and preview FAA NASR cycles for offline lookup.
- `../docs/AVWX_FUTURE_ENHANCEMENT.md` records the boundaries for any future secondary weather source.

Live weather is retrieved from the official [Aviation Weather Center Data API](https://aviationweather.gov/data/api/).
FAA waypoint refreshes use the official NASR subscription pages linked in the fallback bundle metadata.

## Notes

- Prefer stable, reusable source material over links-only when copying is appropriate.
- If only links are recorded, explain why the source was not copied locally.
