# Audit of PESM v1 (`protocol_engine.zip`), resolution in v2, and the v3 / CMOS5L sign-off work

Severity: **S1** breaks function/tapeout, **S2** wrong behaviour in some
cases, **S3** hazard, area, or documentation.
"Probe" = reproduced in simulation against the v1 RTL
(`audit/run_probes.sh <v1>/src/tt_um_protocol_engine.v`).

## A. Input specification conflict (resolved: the interface map was chosen)

`prompt.txt` §3 and `TINYTAPEOUT_24-PIN_ASIC_INTERFACE_MAP.txt` disagree on
`ui_in[3:0]`:

| Pin | prompt.txt | interface map | v2 uses |
|---|---|---|---|
| ui_in[0] | HOST_DATA | HOST_SPI_SCK | SCK |
| ui_in[1] | HOST_CLK | HOST_SPI_MOSI | MOSI |
| ui_in[2] | HOST_MODE (imem/cfg) | HOST_SPI_CS_N | CS_N |
| ui_in[3] | HOST_EN (1 = load) | MODE_SELECT (1 = BOOT) | MODE (1 = BOOT) |

v2 follows the interface map: it has CS_N framing (needed for command
headers / random access) and a MISO return path. The data/clock pin swap
matters for the board wiring.

## B. v1 RTL defects

| # | Sev | Finding | Evidence | v2 |
|---|---|---|---|---|
| 1 | S1 | FETCH and EXEC each wait for `tick`: at least 2 ticks per instruction, so setup instructions take whole bit periods and README §9.2 UART runs at half the baud rate | Probe P2: 2.0 clk/instr at the fastest divider | 1 instr/clk; tick only gates pre-delay/`DLYT`/grid alignment |
| 2 | S1 | Fractional divider does nothing: carry is never applied, the `>= 255` test is wrong, and period = INT+1, not INT | Probe P1: INT=4 FRAC=128 gives 4.98 clk (expected 4.5) | first-order Σ-Δ; formally proven at full 16.8 width |
| 3 | S1 | No synchronizers on any pad input; the loader edge-detects raw `uio_in[1]` | inspection | 2-FF on every async input; reset synchronizer |
| 4 | S1 | The loader/host pins (`uio[3:0]`) are also core output pins: `SET pindirs` makes the chip drive HOST_EN/MODE/CLK/DATA | Probe P6 | host moved to `ui_in[3:0]` + `uo_out[0]`; all 8 `uio` belong to the core |
| 5 | S1 | Host FIFO strobes are level-sensitive: one strobe held for N clk pushes N bytes | Probe P7: 5 bytes from one strobe | SPI byte-framed host port |
| 6 | S1 | `tx_dequeue`/`rx_enqueue` are not gated by load mode, but the pointer updates are: counts and pointers diverge if a tick hits while loading | inspection | single FIFO module, count = f(push, pop), formally proven |
| 7 | S2 | Side-set is applied on `JMP`, whose side-set field holds the target: target bits leak onto pins | Probe P3 | side-set and branch target in separate fields |
| 8 | S2 | Autopush compares the count *before* the current `IN`: threshold 8 pushes on the 9th `IN`, and the pushed byte is shifted by one | Probe P4 | count after the shift; formal assertion |
| 9 | S2 | Autopush with RX full does not stall: `isr_count` wraps, data lost silently | inspection | `IN` stalls; noblock `PUSH` sets `RX_OVF` |
| 10 | S2 | Edge `WAIT` matches edges latched before the `WAIT` began | Probe P5 | edge must occur while waiting |
| 11 | S2 | Wrap jumps to `wrap_bottom` whenever `pc_target > wrap_top`, so explicit jumps past `wrap_top` are hijacked; README says wrap is unused, checklist says done | inspection | PIO semantics: only fall-through from `WRAP_TOP` |
| 12 | S2 | `SET pindirs` writes `pin_oe[7:4] = 0`; `uio[7:4]` can never be outputs | inspection | full 8-bit `PINDIRS`, per-pin `DIR`/`DRIVE` |
| 13 | S2 | `uo_out` and `uio_out` both carry `pin_out`; `ui_in` is never read; host reads mux `uo_out` | inspection | 15 independent outputs, 12 inputs |
| 14 | S2 | Delay off-by-one: `delay = d` waits d+1 ticks; `DELAY` waits `{X,Y}+1` | inspection | defined and tested semantics |
| 15 | S3 | Blocking and non-blocking assignments mixed on `next_state` in one clocked block (sim/synth mismatch risk); 20+ Verilator BLKSEQ warnings | `verilator -Wall` | Verilator `-Wall` clean |
| 16 | S3 | `irq_pending` is write-only (no host read): synthesis removes it | Verilator UNUSED | `IRQ` visible in STATUS0 |
| 17 | S3 | Config path never tested: tb pokes `cfg_*` hierarchically | tb.v `write_cfg` | all tests use the real SPI loader, including readback |
| 18 | S3 | `config.json` hand-sets `DIE_AREA "0 0 1002 432"` (sky130 tile pitch; the IHP 6x4 die is 1289.28 × 710.64 µm) and `FP_SIZING`; the TT flow generates the die from `info.yaml`. *Correction:* an earlier revision of this audit also called `FP_PDN_VPITCH 50.0` / `FP_PDN_VWIDTH 2.1` non-template. That was wrong: they are the values of the CMOS5L template branch, which I had not compared against | diff vs `ttihp-verilog-template@cmos5l` | CMOS5L template `config.json` |
| 19 | S3 | `info.yaml` lists `tt_um_protocol_engine.v` and pins that do not match the RTL; README §10/§14 claim formal/CRV coverage that does not exist; Python assembler mentioned but not in the archive | inspection | rewritten; claims match what was run |

## C. Items in `corrections.txt`

| Item | Status in v2 |
|---|---|
| Setup-instruction stall | Fixed (B1). `test_setup_instructions_run_at_clk` checks 1 instr/clk with a 5000-clk tick |
| Missing bitwise/ALU primitives | Added: `ldi/and/or/xor/dec/inc` on X/Y, `x!=y` compare branch, `par`/`rev`/invert in `MOV`, 1-cycle `crc` (16-bit Galois, reflected or MSB-first). No add/sub between registers, no mul/div |
| Host loader occupies `uio[3:0]` | Fixed (B4) |
| Encoding diagram overlap | Replaced; `docs/ISA.md` gives every format bit-exact |
| Checklist contradictions | README states only what was run, with reproduction commands |
| No random-access loader | `WRITE_IMEM`/`WRITE_CFG` take a start address; `READ_*` for readback |

## D. First CMOS5L CI run (commit 011b2fb): what it showed

All Tiny Tapeout checks passed (precheck, KLayout/Magic DRC, LVS, antenna,
RTL and gate-level tests, typical-corner timing). Two things did not meet
the project's own sign-off criteria (`ci-results/cmos5l-011b2fb`):

| # | Finding | Evidence | Resolution |
|---|---|---|---|
| D1 | Slow-corner setup slack −5.23 ns (87 endpoints); 31 slow-corner slew violations. The flow still reported success: for this PDK LibreLane only fails on typical-corner violations | `55-openroad-stapostpnr/summary.rpt` | D2, D3; `timing_signoff` CI job checks the slow corner |
| D2 | The CMOS5L PDK configuration has an empty `LAYERS_RC`: timing repair during placement, CTS and global routing used 9.7e-5 pF/µm and 0.39 Ω/µm, the routed design extracts at 1.7e-4 pF/µm and 1.2 Ω/µm. The resizer found "no setup violations" and inserted no setup buffers | `14-openroad-dumprcvalues`, `37-openroad-resizertimingpostcts`, SPEF vs DEF fit | `LAYERS_RC`/`VIAS_R` in `src/config.json` |
| D3 | Routing congestion: signals use Metal2–Metal4 only (one horizontal layer). 8 883 initial detailed-routing violations, 34 iterations, 2 h 43 min; a two-pin critical net was routed 998 µm for 247 µm | `44-openroad-detailedrouting`, final DEF | placement density 60 → 40 %, post-global-route design and timing repair enabled |
| D4 | My earlier local OpenROAD script predicted +3.42 ns for this run. It used SG13G2 layer RC values and its own flow, so it did not have D2 | — | script removed; `pnr/` now replays the real LibreLane flow (same scripts, same Yosys; extraction + STA reproduce the CI numbers exactly on the CI layout) |

## E. v3 changes (this revision)

Functional (ISA v3, `docs/ISA.md` section 7): 64-word instruction memory,
pattern branch `JPAT`, background clock generator, host command re-encoding,
chip ID. Selection rationale: `docs/FEASIBILITY.md`.

Structural, each covered by a formal property or a directed test:

| Change | Why | Check |
|---|---|---|
| Single imem read port, shared between core prefetch and host readback (BOOT only); host readback registered | v2 had two read muxes; a second 64:1 mux costs routing the process does not have | formal: `rd_instr == imem[rd_pc]` whenever the core owns the port; loader test at the minimum SCK period |
| Branch decision taken from registers only; stall/tick logic only selects "advance or hold" | keeps the late execute logic out of the read address | formal: branches never stall; `pc` after a completed instruction is the target or the fall-through; `instr == imem[pc]` |
| imem/cfg writes accepted only if the core stays in BOOT one more cycle (`boot & boot_pre`); the core sees MODE one cycle later than the host block | everything the core loads when it leaves BOOT (entry word, pc, pins, background clock state) is stable; replaces the v2 write bypass | formal (host): no register changes at the edge that ends BOOT; directed sweep of the MODE edge across a write (`test_write_gate_at_boot_exit`) |
| imem read mux is an explicit tree of 4:1 muxes (`pesm_mux4.v`, library cell when the IHP library is the target) | synthesis otherwise builds 64 decoded word lines and AND-OR trees: about 6 ns of slow-corner slack and 20 % more wire | gate-level tests run on the netlist with the instantiated cells |

Bugs found by the new tests while writing v3 (all in new code or new
firmware, none in v2 silicon-bound RTL):

* `firmware/sync_serial_tx.pasm` first version used autopull: after an idle
  period the first data bit changed at an arbitrary phase of the clock.
  Found by `test_fw_sync_serial_tx`; fixed with `pull ifempty` before the
  edge wait.
* The DSL guide's I2C register-read example was 66 words. Found by
  `sw/tests/test_docs.py`; rewritten (56 words).
* The mutation run showed that "DLYT retires without waiting for the tick"
  was no longer caught after the CRV instruction mix changed. Added
  `test_dly_and_dlyt_exact` and biased the CRV generator to short delays.

## F. Remaining risks (not closed by this work)

1. **The CMOS5L `gds` workflow has not been run on this revision.** The
   numbers in the README come from the local replay (`pnr/README.md`): same
   LibreLane version, scripts, configuration and Yosys as CI, a different
   OpenROAD build, and no Magic/KLayout DRC, LVS or TT precheck. The first
   CI run on v2 is the only CI evidence for DRC/LVS cleanliness in this
   process; v3 uses the same cell set plus `sg13cmos5l_mux4_1` instances.
2. **`src/config.json` deviates from the Tiny Tapeout template** (density,
   `LAYERS_RC`/`VIAS_R`, post-GRT repair, resizer margins). These are
   ordinary LibreLane variables, but the template asks not to edit below its
   marker line; if the shuttle rejects them, D1 returns.
3. **Model independence.** The reference model and RTL share an author. One
   common-mode error (JPIN decoded as class A) was in both in v2 and only
   showed up in a directed protocol test. Directed tests check against
   protocol specifications (UART framing, SPI slave, I2C slave, CRC-16/USB
   check value, CRC-5 spec vector, USB LS receiver), not against the model.
4. **Top-level formal is bounded.** Unbounded proofs cover `pesm_fifo`,
   `pesm_clkdiv`, `pesm_host` and `pesm_core` separately; the assumptions the
   core proof makes about the host (configuration frozen outside BOOT, read
   port ownership) are proved for `pesm_host`, and the one assumption the
   host proof makes about the top level (`boot` is `boot_pre` delayed) is a
   top-level assertion.
5. **USB.** LS transmit is demonstrated (FIFO-fed with NRZI/stuffing/EOP, and
   compile-time tokens). An LS receiver with on-the-fly CRC does not fit the
   register budget. 10BASE-T and FS USB exceed 1 instr/clk at 50 MHz.
6. **`READ_IMEM` in RUN returns undefined data** (the read port belongs to
   the core). The emulator returns the real contents; do not rely on it.
7. **Not simulated at gate level with SDF.** Gate-level runs use functional
   cell models; timing rests on STA.
8. **Discord handle in `info.yaml` is empty** (optional for Tiny Tapeout, but
   the directive asked for it): it has to come from the author.
