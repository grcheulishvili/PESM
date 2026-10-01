# PESM v2 — Protocol Engine State Machine

A microcoded, cycle-exact protocol emulator for the IHP SG13G2 (130 nm)
Tiny Tapeout shuttle, 6×4 tiles, 50 MHz. UART, SPI, I2C and similar
protocols run as firmware. The chip has no hard protocol blocks and no
general-purpose ALU.

* ISA, timing model and host protocol: [`docs/ISA.md`](docs/ISA.md)
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

## Firmware examples (`firmware/`)

| File | Words | Demonstrates |
|---|---|---|
| `uart_tx.pasm` | 8 | grid-aligned bit edges, 115200 8N1 |
| `uart_rx.pasm` | 12 | edge re-phasing (`synchalf`), autopush, framing error → IRQ |
| `spi_master.pasm` | 9 | mode 0, full duplex, side-set SCLK, autopull/autopush, back-to-back bytes |
| `i2c_master_write.pasm` | 24 | open drain, clock stretching, ACK/NACK |
| `crc16_usb.pasm` | 18 | 1-cycle CRC step, host-flag handshake |
| `frac_div.pasm` | 2 | divider characterisation |

```
python3 tools/pesm_asm.py firmware/spi_master.pasm --spi
```

## Verification status

Everything below was run on this exact source tree.

| What | How | Result |
|---|---|---|
| Lint | `make lint` (Verilator 5.020 `-Wall`) | clean |
| Directed tests, RTL | `test/test.py`, 12 tests, black-box via the real SPI loader: loader random access and readback, write protection, FIFO and flags, illegal opcode, fractional divider (exact 256-period sums), setup-at-clk regression, UART TX (every edge within 1 clk of the grid), UART RX (±2 % baud, framing error), SPI master against a mode-0 slave model, I2C against a slave with clock stretching and NACK, CRC-16/USB check value 0xB4C8, edge-WAIT re-phasing + side-set | 12/12 pass |
| Constrained random, RTL | `test/test_crv.py`: random config + program + TX preload, pins compared every cycle against `test/pesm_model.py`, FIFOs and flags compared at the end | 40 × 1500 cycles (default) and 300 × 2000 cycles (seed 7, 471 692 instructions): pass |
| Mutation check | `test/mutate.sh`: 5 injected core bugs | all 5 caught (4 by CRV, `!osre` off-by-one only by the SPI/CRC directed tests) |
| Formal, unbounded | `make formal`: `pesm_fifo` (PDR; incl. data-ordering proof), `pesm_clkdiv` (k-induction at full 16.8 width: period ∈ {I, I+1}, exact SYNC/half phase), `pesm_host` (imem/cfg immutable while running), `pesm_core` (k-induction: FIFO handshake safety, pre-delayed instructions execute only on ticks, stall ⇒ pc frozen, HALT freezes pins, edge-WAIT needs an edge, autopush only at threshold, side-set isolation) | pass |
| Formal, bounded | top-level cross-block FIFO handshakes, open drain never drives 1 (BMC 40) | pass |
| Gate level | yosys netlist (SG13G2) + IHP cell models, full directed suite + CRV | pass (needs `synth/make_gl_models.sh` with Icarus 12, see AUDIT D2) |
| Area (pre-layout) | `synth/run_synth.sh`, SG13G2 typ | 7 830 cells, 1 050 flops, 121 900 µm² ≈ 17 % of the ~0.72 mm² 6×4 area |
| Timing (pre-layout) | ABC `stime`, zero wire load | longest combinational path 6.0 ns typ, 9.4 ns slow (1.08 V/125 °C), `pc → imem mux → decode → next state`; budget 20 ns |
| Place-and-route, STA, DRC/LVS | TT `gds` workflow | **not run here**; use the CI |

## Running

```
make lint
make test                 # needs iverilog + cocotb (test/requirements.txt)
make formal               # needs yosys, sby, z3 >= 4.12
PDK_ROOT=/path/IHP-Open-PDK make synth
PDK_ROOT=/path/IHP-Open-PDK make gl
CRV_ITERS=300 CRV_CYCLES=2000 CRV_SEED=7 make -C test COCOTB_TEST_MODULES=test_crv
```

## Known limits

* 32 instructions. Area use is about 17 %, so 64 words (one more PC bit,
  wider jump field) fits if the ISA is re-encoded.
* Inputs go through 2-FF synchronizers, so sampling has a fixed 2–3 clk
  latency and ±1 clk uncertainty on asynchronous edges.
* Peak bit rate is about f_clk / 4 for full-duplex protocols that need a
  `jmp` in the bit loop (about 12.5 Mb/s SPI at 50 MHz; not tested). 10BASE-T and FS USB are
  out of reach. LS USB fits the cycle budget, but no firmware exists for it.

## License

Apache-2.0
