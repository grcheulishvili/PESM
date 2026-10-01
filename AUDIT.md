# Audit of PESM v1 (`protocol_engine.zip`) and resolution in v2

Severity: **S1** breaks function/tapeout, **S2** wrong behaviour in some
cases, **S3** hazard, area, or documentation.
"Probe" = reproduced in simulation against the v1 RTL
(`audit/run_probes.sh <v1>/src/tt_um_protocol_engine.v`).

## A. Input specification conflict (decision needed from you)

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
| 18 | S3 | `config.json` hand-sets `DIE_AREA "0 0 1002 432"` (sky130 tile pitch) and non-template PDN pitch/width; the TT IHP flow derives the die from `info.yaml` | diff vs `ttihp-verilog-template` | template `config.json` restored |
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

## D. Remaining risks (not closed by this work)

1. **Signoff is not complete.** `pnr/run_pnr.sh` runs OpenROAD (place, CTS,
   route, OpenRCX, three-corner STA) on the real TT 6x4 template, but it is
   not LibreLane. Magic/KLayout DRC, LVS, the TT precheck and GDS have not
   been run. The TT `gds` workflow is the signoff.
2. **Gate-level CI (resolved).** The TT `gl_test` action installs
   TinyTapeout's Icarus 13 build, which drives the IHP `delayed_*` nets. The
   unmodified cell models pass the full suite with it.
   `synth/make_gl_models.sh` is only needed with Icarus 12.
3. **Model independence.** The reference model and RTL share an author. One
   common-mode error (JPIN decoded as class A) was in both and only showed
   up in a directed protocol test. Directed tests check against protocol
   specifications (UART framing, SPI slave, I2C slave, CRC-16/USB check
   value, CRC-5 spec vector, USB LS receiver), not against the model.
4. **Top-level formal is bounded** (BMC depth 40). Unbounded proofs cover
   `pesm_fifo`, `pesm_clkdiv`, `pesm_host` and `pesm_core` separately.
5. **USB.** LS transmit is demonstrated: NRZI, stuffing, EOP, on-chip CRC5
   and CRC16. An LS receiver with on-the-fly CRC does not fit the register
   budget. 10BASE-T and FS USB exceed 1 instr/clk at 50 MHz.
6. **Critical path.** pc → imem 32:1 mux → decode → IN shifter/autopush
   byte select → RX FIFO write data: 46 logic levels. A first OpenROAD run
   without a setup margin closed at only +0.76 ns in the slow corner (see
   README for the margin-driven result).
