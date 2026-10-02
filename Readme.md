# PESM v2 — Protocol Engine State Machine

A microcoded, cycle-exact protocol emulator for the IHP CMOS5L (130 nm,
`ihp-sg13cmos5l`) Tiny Tapeout shuttle, 6×4 tiles, 50 MHz. UART, SPI, I2C and similar
protocols run as firmware. The chip has no hard protocol blocks and no
general-purpose ALU.

* ISA, timing model and host protocol: [`docs/ISA.md`](docs/ISA.md)
* Python DSL, assembler, host programmer, pipeline: [`docs/DSL_GUIDE.md`](docs/DSL_GUIDE.md)
* Review of v1 and what changed: [`AUDIT.md`](AUDIT.md)

## Pin map

| Pin | Function |
|---|---|
| `ui_in[0..2]` | host SPI SCK, MOSI, CS_N (mode 0) |
| `ui_in[3]` | MODE: 1 = BOOT (core held, program/config writable), 0 = RUN |
| `ui_in[4..7]` | TIN0–3 → core input pins 8–11 |
| `uo_out[0]` | host SPI MISO |
| `uo_out[1..7]` | TOUT0–6 → core output pins 8–14 |
| `uio[0..7]` | BIO0–7 → core pins 0–7, per-pin OE, optional open drain |

## Architecture

```
 ui_in[3:0] ─2FF─► pesm_host ──────────── imem 32×16, cfg 16×8 ───────┐
                    (SPI slave, │ TX FIFO 8×8 ───────────────┐          │ instr
 uo_out[0] ◄──────── MISO)      └ RX FIFO 8×8 ◄───────────┐  │          ▼
                                                       pesm_core (1 instr/clk)
 ui_in[7:4], uio_in ─2FF───────────────────────────────►  X Y OSR ISR LB pc
                                                          pesm_clkdiv (16.8 tick)
 uo_out[7:1], uio_out, uio_oe ◄───────────────────────── OUT[14:0] OE[7:0]
```

| Block | File |
|---|---|
| top, reset synchronizer, pad logic | `src/tt_um_protocol_engine.v` |
| core | `src/pesm_core.v` |
| tick generator | `src/pesm_clkdiv.v` |
| host SPI, imem, config | `src/pesm_host.v` |
| FIFO | `src/pesm_fifo.v` |
| synchronizer | `src/pesm_sync.v` |

## Software (`sw/pesm`)

| Tool | |
|---|---|
| `pesm.builder.PESMProgram` | Python DSL: chainable instructions plus UART/SPI/I2C/CRC/USB-LS macros, compiled straight to an image |
| `python -m pesm.assembler` | `.pasm` → listing / `.bin` (80 B: imem + cfg) / `.mem` ($readmemh) / Python lists / JSON |
| `python -m pesm.programmer` | flash + readback-verify over FT232H (pyftdi) or Raspberry Pi spidev |
| `python -m pesm.run_pipeline` | DSL `.py`, `.pasm`, `.bin` or `.json` → flash → run → TX/RX/status; `--backend emulator` needs no hardware |

```
pip install -e sw && pytest sw/tests
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend ftdi --tx 123456789 --hflag --wait-halt --rx 2
```

## Firmware examples (`firmware/`, DSL equivalents in `sw/examples/`)

| File | Words | Demonstrates |
|---|---|---|
| `uart_tx.pasm` | 8 | grid-aligned bit edges, 115200 8N1 |
| `uart_rx.pasm` | 12 | edge re-phasing (`synchalf`), autopush, framing error → IRQ |
| `spi_master.pasm` | 9 | mode 0, full duplex, side-set SCLK, autopull/autopush, back-to-back bytes |
| `i2c_master_write.pasm` | 24 | open drain, clock stretching, ACK/NACK |
| `crc16_usb.pasm` | 18 | 1-cycle CRC step, host-flag handshake |
| `crc5_usb.pasm` | 17 | CRC-5/USB of a token's 11-bit ADDR/ENDP field |
| `usb_ls_tx.pasm` | 23 | USB low-speed packet TX: NRZI, bit stuffing, SE0-SE0-J EOP, grid-aligned at 1.5 Mb/s |
| `frac_div.pasm` | 2 | divider characterisation |

```
python3 tools/pesm_asm.py firmware/spi_master.pasm --spi
```

## Verification status

Everything below was run on this exact source tree.

| What | How | Result |
|---|---|---|
| Lint | `make lint` (Verilator 5.020 `-Wall`) | clean |
| Toolchain unit tests | `pytest sw/tests` (56): all 65 536 words × 5 side-set counts round-trip through disassembler and assembler; every DSL example is bit-identical to its `firmware/*.pasm`; `to_asm` round-trip; output formats; programmer verify/retry, FIFO flow control and CRC pipeline on the emulator; FTDI and spidev transports against fake drivers; every DSL code block in the guide compiles | pass |
| Toolchain on the design | `test/test_toolchain.py`: `programmer.py`/`run_pipeline` over the real SPI port via a cocotb transport; DSL UART-byte (exact 50-clk bits), SPI-transfer (slave model) and I2C-read (slave model, NACK) macros | 4/4 pass |
| Directed tests, RTL | `test/test.py`, 15 tests, black-box via the real SPI loader: loader random access and readback, write protection, FIFO and flags, illegal opcode, fractional divider (exact 256-period sums), setup-at-clk regression, UART TX (every edge within 1 clk of the grid), UART RX (±2 % baud, framing error), SPI master against a mode-0 slave model, I2C against a slave with clock stretching and NACK, CRC-16/USB check value 0xB4C8, edge-WAIT re-phasing + side-set, USB LS: on-chip CRC5 (spec vector) and CRC16, then SETUP token + DATA0 packet transmitted and decoded by an independent NRZI/de-stuff/EOP/CRC receiver (all transitions within 0.71 clk of the 1.5 MHz grid), back-to-back autopush into a full RX FIFO through the registered write port, pin-map isolation (host traffic never moves a uio/TOUT pin) | 15/15 pass |
| Constrained random, RTL | `test/test_crv.py`: random config + program + TX preload, pins compared every cycle against `sw/pesm/model.py`, FIFOs and flags compared at the end | 40 × 1500 cycles (default) and 300 × 2000 cycles (seed 7, 459 792 instructions): pass |
| Mutation check | `test/mutate.sh`: 6 injected core bugs | all caught (`!osre` off-by-one and RX pending-full only by directed tests, the rest also by CRV) |
| Formal, unbounded | `make formal`: `pesm_fifo` (PDR; incl. data-ordering proof), `pesm_clkdiv` (k-induction at full 16.8 width: period ∈ {I, I+1}, exact SYNC/half phase), `pesm_host` (imem/cfg immutable while running), `pesm_core` (k-induction: prefetch consistency `instr == imem[pc]`, FIFO handshake safety, pre-delayed instructions execute only on ticks, stall ⇒ pc frozen, HALT freezes pins, edge-WAIT needs an edge, autopush only at threshold, side-set isolation) | pass |
| Formal, bounded | top-level cross-block FIFO handshakes incl. the registered RX write never hitting a full FIFO, open drain never drives 1 (BMC 40) | pass |

### Physical implementation

Target process is **IHP CMOS5L** (`ihp-sg13cmos5l`, template branch
`cmos5l`): Metal1–4 + TopMetal1, signal routing limited to Metal4, 6×4 die
1289.28 × 710.64 µm. The first CI run (commit 55910b7) was made on SG13G2 by
mistake; it is kept as a reference (`ci-results/`).

| | CMOS5L (target) | SG13G2 (reference) |
|---|---|---|
| Flow | local OpenROAD, `pnr/run_pnr.sh` (not LibreLane) | **Tiny Tapeout CI**, LibreLane 3.0.5 |
| Setup slack typ / slow / fast | +9.68 / **+3.42** / +13.34 ns | +10.44 / **+4.87** / +13.73 ns |
| Hold slack typ / slow / fast | +0.37 / +0.75 / **+0.16** ns | +0.31 / +0.63 / **+0.12** ns |
| Routing DRC | 0 markers (TritonRoute) | 0 (router), Magic DRC 0, KLayout DRC pass |
| LVS | not run locally | 0 errors (Netgen) |
| Antenna | 0 violations | 0 violations |
| Max slew / cap | 0 | 0 |
| Max fanout entries | 0 | 74, all clock-tree leaf buffers (warning only) |
| Std-cell area, utilisation | 157 459 µm², 17 % | 150 131 µm², 16.6 % |
| TT precheck | not run | 10/10 pass |
| Gate-level tests on the routed netlist | 20/20 (Icarus 13, CMOS5L models) | 20/20 (CI `gl_test`) |
| RTL tests in CI | – | 20/20 |
| Reports | `pnr/reports/cmos5l/` | `ci-results/sg13g2-55910b7/`, `pnr/reports/sg13g2/` |

**Not yet done:** the CMOS5L `gds`, `precheck` and `gl_test` workflows. Push
this revision to run them; LibreLane/KLayout/Magic/Netgen on CMOS5L are the
signoff, the local OpenROAD numbers are a prediction (on SG13G2 the same
local flow predicted +4.49 ns slow-corner setup against +4.87 ns in CI).

Pre-layout synthesis (`synth/run_synth.sh`, identical for both PDKs: same
cell set and areas): 8 483 cells, 1 076 flops, 122 955 µm² = 13.4 % of the
die.

## Running

```
make lint
make test                 # needs iverilog + cocotb (test/requirements.txt)
make formal               # needs yosys, sby, z3 >= 4.12
# PDK defaults to ihp-sg13cmos5l (IHP-Open-PDK rev 2bbec755, the one the TT action pins);
# TT_TOOLS = tt-support-tools, branch ihp-sg13cmos5l
PDK_ROOT=/path/IHP-Open-PDK make synth
PDK_ROOT=/path/IHP-Open-PDK make gl                                          # Icarus >= 13
PDK_ROOT=/path/IHP-Open-PDK TT_TOOLS=/path/tt-support-tools pnr/run_pnr.sh   # ~70 min, 2 cores
STA_ONLY=1 ... pnr/run_pnr.sh                                                # re-extract + STA on the routed checkpoint
CRV_ITERS=300 CRV_CYCLES=2000 CRV_SEED=7 make -C test COCOTB_TEST_MODULES=test_crv
```

## Known limits


* 32 instructions. Area use is about 13 %, so 64 words (one more PC bit,
  wider jump field) fits if the ISA is re-encoded.
* Inputs go through 2-FF synchronizers, so sampling has a fixed 2–3 clk
  latency and ±1 clk uncertainty on asynchronous edges.
* Peak bit rate is about f_clk / 4 for full-duplex protocols that need a
  `jmp` in the bit loop (about 12.5 Mb/s SPI at 50 MHz; not tested). 10BASE-T and FS USB are
  out of reach. LS USB transmit is demonstrated; an LS receiver (NRZI decode + de-stuff +
  on-the-fly CRC) does not fit the register budget in one program (X/Y are the CRC register).

## License

Apache-2.0
