# PESM v3 - Protocol Engine State Machine

A microcoded, cycle-exact protocol emulator ASIC. UART, SPI, I2C, USB
low-speed packets and similar protocols run as firmware. The chip has no
hard protocol blocks and no general-purpose ALU.

| | |
|---|---|
| Process | IHP CMOS5L (130 nm, `ihp-sg13cmos5l`), Tiny Tapeout shuttle |
| Size | 6×4 tiles |
| Clock | 50 MHz, one instruction per clock |
| Program | 64 × 16-bit instruction words, loaded over the SPI host port |
| Sign-off | slow-corner setup **+5.13 ns**, fast-corner hold **+0.13 ns**, 0 DRC / LVS / antenna violations (CI, commit 359f960) |

## Contents

* [Documentation](#documentation)
* [Quick start](#quick-start)
* [Features](#features)
* [Pin map](#pin-map)
* [Architecture](#architecture)
* [Software (`sw/pesm`)](#software-swpesm)
* [Firmware examples](#firmware-examples)
* [Verification status](#verification-status)
* [Running the checks](#running-the-checks)
* [Repository layout](#repository-layout)
* [Known limits](#known-limits)
* [License](#license)

## Documentation

| Document | Content |
|---|---|
| [`docs/ISA.md`](docs/ISA.md) | ISA, timing model and host protocol (normative) |
| [`docs/DSL_GUIDE.md`](docs/DSL_GUIDE.md) | Python DSL, assembler, host programmer, pipeline |
| [`docs/FEASIBILITY.md`](docs/FEASIBILITY.md) | v3 extension study (64-word memory, pattern branch, background clock, dual core) |
| [`pnr/README.md`](pnr/README.md) | Physical flow, local replay of the Tiny Tapeout flow |
| [`ci-results/README.md`](ci-results/README.md) | Summaries of the Tiny Tapeout CI runs |
| [`AUDIT.md`](AUDIT.md) | Review of v1 and what changed since |
| [`docs/info.md`](docs/info.md) | Tiny Tapeout datasheet page |

## Quick start

```
pip install -e sw && pytest sw/tests
```

Run a program without hardware (emulator), then on the chip through an
FT232H:

```
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend emulator --tx 123456789 --hflag --wait-halt --rx 2
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend ftdi --tx 123456789 --hflag --wait-halt --rx 2
```

Print the host SPI frames of an assembled program:

```
python3 tools/pesm_asm.py firmware/spi_master.pasm --spi
```

The step-by-step tutorial is in [`docs/DSL_GUIDE.md`](docs/DSL_GUIDE.md),
section 2.

## Features

* 64 × 16-bit instruction memory, one instruction per clock (50 MHz)
* 16.8 fractional tick divider: instructions with a pre-delay execute exactly
  on a tick, setup instructions in between run at full clock speed
* 32-bit OSR/ISR with autopull/autopush, 8-byte TX and RX FIFOs
* up to 4 side-set pins per instruction; per-pin direction and open drain on
  all 8 bidirectional pins
* minimal ALU on X/Y, plus parity, bit-reverse and a 1-cycle 16-bit CRC step

v3 additions over v2 (details in `docs/ISA.md` section 7):

* **64-word instruction memory** (6-bit pc)
* **`JPAT`**: masked compare of up to 8 input pins and branch, in one cycle
* **Background clock**: free-running clock on any output pin, tied to the
  tick grid, started/stopped by the program, readable as input pin 15

## Pin map

| Pin | Function |
|---|---|
| `ui_in[0..2]` | host SPI SCK, MOSI, CS_N (mode 0) |
| `ui_in[3]` | MODE: 1 = BOOT (core held, program/config writable), 0 = RUN |
| `ui_in[4..7]` | TIN0-3 → core input pins 8-11 |
| `uo_out[0]` | host SPI MISO |
| `uo_out[1..7]` | TOUT0-6 → core output pins 8-14 |
| `uio[0..7]` | BIO0-7 → core pins 0-7, per-pin OE, optional open drain |

## Architecture

```
 ui_in[3:0] ─2FF─► pesm_host ──────────── imem 64×16, cfg 20×8 ──────┐
                    (SPI slave, │ TX FIFO 8×8 ───────────────┐         │ instr
 uo_out[0] ◄──────── MISO)      └ RX FIFO 8×8 ◄───────────┐  │         ▼
                                                       pesm_core (1 instr/clk)
 ui_in[7:4], uio_in ─2FF───────────────────────────────►  X Y OSR ISR LB pc
                                                          pesm_clkdiv (16.8 tick)
                                                          background clock
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
| 4:1 mux of the imem read tree | `src/pesm_mux4.v` |

## Software (`sw/pesm`)

| Tool | |
|---|---|
| `pesm.builder.PESMProgram` | Python DSL: chainable instructions plus UART/SPI/I2C/CRC/USB-LS macros, compiled straight to an image |
| `python -m pesm.assembler` | `.pasm` → listing / `.bin` (156 B: header + imem + cfg) / `.mem` ($readmemh) / Python lists / JSON; `--all` writes every format |
| `python -m pesm.programmer` | flash + readback-verify over FT232H (pyftdi) or Raspberry Pi spidev; `--monitor` |
| `python -m pesm.run_pipeline` | DSL `.py`, `.pasm`/`.asm`, `.bin` or `.json` → flash → run → TX/RX/status/monitor; `--backend emulator` needs no hardware |

## Firmware examples

`firmware/*.pasm`, with DSL equivalents in `sw/examples/`.

| File | Words | Demonstrates |
|---|---|---|
| `uart_tx.pasm` | 8 | grid-aligned bit edges, 115200 8N1 |
| `uart_rx.pasm` | 12 | edge re-phasing (`synchalf`), autopush, framing error → IRQ |
| `spi_master.pasm` | 9 | mode 0, full duplex, side-set SCLK, autopull/autopush, back-to-back bytes |
| `i2c_master_write.pasm` | 24 | open drain, clock stretching, ACK/NACK |
| `crc16_usb.pasm` | 18 | 1-cycle CRC step, host-flag handshake |
| `crc5_usb.pasm` | 17 | CRC-5/USB of a token's 11-bit ADDR/ENDP field |
| `usb_ls_tx.pasm` | 23 | USB low-speed packet TX: NRZI, bit stuffing, SE0-SE0-J EOP, grid-aligned at 1.5 Mb/s |
| `usb_ls_token.pasm` | 45 | two USB LS tokens with constant fields, resolved at compile time (needs the 64-word memory) |
| `addr_strobe_capture.pasm` | 5 | pattern branch: address + strobe qualified parallel capture |
| `sync_serial_tx.pasm` | 3 | background clock as a free-running bit clock |
| `frac_div.pasm` | 2 | divider characterisation |

## Verification status

Everything below was run on the v3 sources (commit 359f960) unless a row
says otherwise.

### Sign-off (Tiny Tapeout CI, commit 359f960)

Tiny Tapeout `gds` workflow on CMOS5L (`TinyTapeout/tt-gds-action@ihp-cmos5l`,
LibreLane 3.1.0.dev3). Summaries: `ci-results/cmos5l-359f960/`.

| Check | Result |
|---|---|
| Setup slack typ / slow / fast | +10.57 / **+5.13** (1.08 V, 125 °C) / +13.74 ns |
| Hold slack typ / slow / fast | +0.33 / +0.68 / **+0.13** ns |
| Slew / cap violations | 0 / 0 in all corners |
| Routing DRC | **0** |
| Magic DRC | **0** |
| KLayout DRC (precheck) | **0** |
| LVS errors | **0** |
| Antenna violations | **0** |
| Tiny Tapeout precheck | all nine items pass |
| Size | 11 932 cells (1 652 flops), 211 497 µm² = 23.4 % of the core |
| Detailed routing | 28 min |
| CI tests (`test` and `gl_test` workflows) | all 28 cocotb tests (directed, CRV, toolchain) on RTL and on the routed gate-level netlist: 28/28 and 28/28 pass |

### Functional verification

| What | How | Result |
|---|---|---|
| Lint | `make lint` (Verilator 5.020 `-Wall`) | clean |
| Toolchain unit tests | `pytest sw/tests` (91) | pass |
| Directed tests, RTL | `test/test.py`, 22 tests, black-box via the real SPI loader | 22/22 pass |
| Toolchain on the design | `test/test_toolchain.py` | 5/5 pass |
| Constrained random, RTL | `test/test_crv.py` | 40 × 1500 cycles (default) and 300 × 2000 cycles (seed 7, 498 202 instructions): pass |
| Mutation check | `test/mutate.sh`: 25 injected bugs in core, host and top | 25/25 caught |
| Formal, unbounded | `make formal` | pass |
| Formal, bounded | top level, BMC depth 40 | pass |

What each row covers:

* **Toolchain unit tests**
  * all 65 536 words × 5 side-set counts round-trip through disassembler
    and assembler
  * every DSL example is bit-identical to its `firmware/*.pasm`
  * pinned v3 encodings; output formats incl. `.bin` header checks
  * programmer verify/retry, chip-ID check, FIFO flow control, runtime
    monitor and pipeline on the emulator
  * FTDI and spidev transports against fake drivers
  * USB packet encoder/decoder
  * every Python block of the DSL guide runs and every public DSL method is
    documented
* **Directed tests, RTL**
  * v2 set: loader random access and readback, write protection, FIFO and
    flags, illegal opcodes, fractional divider (exact 256-period sums),
    setup-at-clk regression, UART TX/RX, SPI master, I2C with clock
    stretching and NACK, CRC-16/USB, edge-WAIT re-phasing + side-set, USB LS
    TX with on-chip CRC5/CRC16 against an independent NRZI/de-stuff/EOP
    receiver, back-to-back autopush into a full RX FIFO, pin-map isolation
  * New: far jumps/entry/wrap across the 64-word memory, pattern branch (all
    64 pin combinations, exact 4-clk latency, X-compare), background clock
    (exact integer and fractional periods, pin takeover, start/stop/reset,
    input pin 15, timebase mode), write gate at BOOT exit (MODE edge swept
    across a write), exact `DLY`/`DLYT`, `addr_strobe_capture` and
    `sync_serial_tx` firmware
* **Toolchain on the design**
  * `programmer.py`/`run_pipeline` over the real SPI port via a cocotb
    transport
  * DSL UART-byte, SPI-transfer, I2C-read macros against device models
  * USB LS token macro (two compile-time tokens, 45 words) against the
    independent USB receiver
* **Constrained random, RTL**
  * random config + 64-word program + TX preload
  * pins compared every cycle against `sw/pesm/model.py`, FIFOs and flags
    compared at the end
  * functional coverage goals include taken branches of every kind, `bgclk`,
    background clock toggling on a pin
* **Mutation check**: branch conditions, 6-bit targets, pattern compare,
  background clock, prefetch/stall, write gate, read-port hand-over, read
  mux tree, readback register, command decode
* **Formal, unbounded**
  * `pesm_fifo`: PDR; incl. data-ordering proof
  * `pesm_clkdiv`: k-induction at full 16.8 width: period ∈ {I, I+1}, exact
    SYNC/half phase
  * `pesm_host`: imem/cfg frozen outside BOOT and at the edge that ends
    BOOT; read port serves the core whenever it owns it
  * `pesm_core`: k-induction: prefetch consistency `instr == imem[pc]`, next
    pc = target or fall-through, branches never stall, FIFO handshake
    safety, pre-delayed instructions execute only on ticks, stall ⇒ pc
    frozen, HALT freezes pins, edge-WAIT needs an edge, autopush only at
    threshold, side-set isolation, `JPAT` = masked compare, background clock
    changes only on ticks or by `bgclk reset`
* **Formal, bounded**: cross-block FIFO handshakes incl. the registered RX
  write never hitting a full FIFO, open drain never drives 1, background
  clock owns exactly its pin, `boot` is `boot_pre` delayed

### Local physical flow

| What | How | Result |
|---|---|---|
| Place & route + 3-corner STA, local | local replay of the Tiny Tapeout flow (`pnr/run_local.sh`, same LibreLane and `src/config.json`, different OpenROAD build; see `pnr/README.md`). Reports: `pnr/reports/v3-local/` | setup +4.68 ns slow, hold +0.14 ns fast, 0 routing DRC, 0 antenna: within 0.5 ns of the CI result |
| Gate level, local | all 28 cocotb tests on the netlist of the local run, IHP CMOS5L cell models, Icarus 13 | 28/28 pass |

### Earlier CI run

| What | How | Result |
|---|---|---|
| CI, v2 (011b2fb) | Tiny Tapeout `gds` workflow on CMOS5L | DRC/LVS/antenna/precheck clean, RTL 20/20, GL 20/20; setup +4.26 typ, **-5.23 ns slow**: the reason for the configuration changes in this revision |

## Running the checks

```
make lint
make test                 # needs iverilog + cocotb (test/requirements.txt)
make sw-test              # pytest
make formal               # needs yosys, sby, z3 >= 4.12
make mutate               # mutation check, about 40 min
PDK_ROOT=/path/IHP-Open-PDK make synth
PDK_ROOT=/path/IHP-Open-PDK make gl
pnr/setup.sh && PDK_ROOT=/path/IHP-Open-PDK TT_TOOLS=/path/tt-support-tools make pnr
CRV_ITERS=300 CRV_CYCLES=2000 CRV_SEED=7 make -C test COCOTB_TEST_MODULES=test_crv
```

## Repository layout

| Path | Content |
|---|---|
| `src/` | RTL, and `config.json` for the Tiny Tapeout hardening flow |
| `info.yaml` | Tiny Tapeout project description and pinout |
| `docs/` | ISA reference, DSL guide, feasibility study, datasheet page |
| `sw/pesm/` | Python package: DSL, assembler, programmer, emulator, pipeline |
| `sw/examples/` | DSL versions of the firmware examples |
| `sw/tests/` | pytest suite of the toolchain |
| `firmware/` | `.pasm` firmware examples |
| `tools/pesm_asm.py` | wrapper for `pesm.assembler` |
| `test/` | cocotb tests (directed, constrained random, toolchain) and the mutation script |
| `formal/` | SymbiYosys jobs |
| `synth/` | pre-layout synthesis and gate-level model scripts |
| `pnr/` | local replay of the Tiny Tapeout flow, and its reports |
| `ci-results/` | summaries of the Tiny Tapeout CI runs |
| `audit/` | probes that reproduce the v1 findings of `AUDIT.md` |

## Known limits

* 64 instructions, two 8-bit registers, no register-to-register arithmetic.
* Inputs go through 2-FF synchronizers, so sampling has a fixed 2-3 clk
  latency and ±1 clk uncertainty on asynchronous edges.
* Peak bit rate is about f_clk / 4 for full-duplex protocols that need a
  `jmp` in the bit loop (about 12.5 Mb/s SPI at 50 MHz; not tested).
  10BASE-T and FS USB are out of reach.
* LS USB transmit is demonstrated; an LS receiver (NRZI decode + de-stuff +
  on-the-fly CRC) does not fit the register budget in one program (X/Y are
  the CRC register).
* One background clock generator; its frequency is a divisor of the tick
  rate.
* `READ_IMEM` works only in BOOT.

## License

Apache-2.0
