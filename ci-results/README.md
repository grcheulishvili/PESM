# Tiny Tapeout CI results

Summaries of `gds` workflow runs (the artifacts themselves are on GitHub).

## Runs

| Folder | Commit | PDK | LibreLane |
|---|---|---|---|
| `cmos5l-359f960/` | 359f960 (PESM v3) | `ihp-sg13cmos5l` | 3.1.0.dev3 |
| `cmos5l-011b2fb/` | 011b2fb (PESM v2) | `ihp-sg13cmos5l` | 3.1.0.dev3 |
| `sg13g2-55910b7/` | 55910b7 (PESM v2) | `ihp-sg13g2` | 3.0.5 |

### `cmos5l-359f960/` (PESM v3, sign-off run)

| Check | Result |
|---|---|
| Precheck (incl. KLayout DRC), Magic DRC, LVS, antenna | 0 errors |
| RTL / gate-level tests | 28/28 and 28/28 pass |
| Setup slack typical / slow / fast | +10.57 / **+5.13 ns** / +13.74 |
| Hold slack typical / slow / fast | +0.33 / +0.68 / **+0.13 ns** |
| Slew / capacitance violations | none |

### `cmos5l-011b2fb/` (PESM v2)

| Check | Result |
|---|---|
| Precheck, DRC, LVS, antenna, RTL and gate-level tests | pass |
| Setup slack typical / slow | +4.26 ns / **-5.23 ns** (see `AUDIT.md` section D) |

### `sg13g2-55910b7/` (PESM v2)

| Check | Result |
|---|---|
| All checks | pass |
| Setup slack, slow corner | +4.87 ns |
| Note | not the competition process |

## Checking a run

The `timing_signoff` job of the `gds` workflow prints the three-corner table
and fails if slow-corner setup slack is not above 3 ns (LibreLane itself only
fails on typical-corner violations for this PDK). The same report can be
produced from a downloaded `GDS_logs` artifact:

```
python3 pnr/ll_report.py <GDS_logs>/runs/wokwi --steps --check 3.0
```

## Files in each folder

| File | Content |
|---|---|
| `metrics.csv` | LibreLane final metrics |
| `sta_summary.rpt` | post-PnR STA, three corners |
| `ll_report.txt` | `pnr/ll_report.py --steps` output (per-step metrics and sign-off table) |
| `precheck.md` | Tiny Tapeout precheck (KLayout DRC, pins, layers, …) |
| `commit_id.json`, `pdk.json` | what was built, with which PDK revision |
| `synthesis-stats.txt` | Yosys cell statistics (SG13G2 run only) |
