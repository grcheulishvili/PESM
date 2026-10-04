# Tiny Tapeout CI results

Summaries of `gds` workflow runs (the artifacts themselves are on GitHub).

| Folder | Commit | PDK | LibreLane | Result |
|---|---|---|---|---|
| `cmos5l-359f960/` | 359f960 (PESM v3) | `ihp-sg13cmos5l` | 3.1.0.dev3 | precheck (incl. KLayout DRC), Magic DRC, LVS, antenna: 0 errors; RTL 28/28 and gate-level 28/28 tests pass; setup slack +10.57 typical, **+5.13 ns slow**, +13.74 fast; hold +0.33 / +0.68 / **+0.13 ns**; no slew or capacitance violations |
| `cmos5l-011b2fb/` | 011b2fb (PESM v2) | `ihp-sg13cmos5l` | 3.1.0.dev3 | precheck, DRC, LVS, antenna, RTL and gate-level tests pass; setup slack +4.26 ns typical, **−5.23 ns slow** (see `AUDIT.md` section D) |
| `sg13g2-55910b7/` | 55910b7 (PESM v2) | `ihp-sg13g2` | 3.0.5 | all checks pass, slow-corner setup +4.87 ns. Not the competition process |

The `timing_signoff` job of the `gds` workflow prints the three-corner table
and fails if slow-corner setup slack is not above 3 ns (LibreLane itself only
fails on typical-corner violations for this PDK). The same report can be
produced from a downloaded `GDS_logs` artifact:

```
python3 pnr/ll_report.py <GDS_logs>/runs/wokwi --steps --check 3.0
```

| File | Content |
|---|---|
| `metrics.csv` | LibreLane final metrics |
| `sta_summary.rpt` | post-PnR STA, three corners |
| `ll_report.txt` | `pnr/ll_report.py --steps` output (per-step metrics and sign-off table) |
| `precheck.md` | Tiny Tapeout precheck (KLayout DRC, pins, layers, …) |
| `commit_id.json`, `pdk.json` | what was built, with which PDK revision |
| `synthesis-stats.txt` | Yosys cell statistics (SG13G2 run only) |
