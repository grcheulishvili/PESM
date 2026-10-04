# Feasibility of the v3 extensions on the 6×4 CMOS5L tile

Baseline: PESM v2 (commit 011b2fb), first CMOS5L `gds` run
(`ci-results/cmos5l-011b2fb`).

## 1. Baseline audit

| Check | Required | CI result (011b2fb) | |
|---|---|---|---|
| Setup slack, typical 1.20 V / 25 °C | | +4.26 ns | |
| Setup slack, slow 1.08 V / 125 °C | > 3.0 ns | **−5.23 ns** (87 endpoints, TNS −312 ns) | **fail** |
| Setup slack, fast 1.32 V / −40 °C | | +9.80 ns | |
| Hold slack, typ / slow / fast | > 0 | +0.29 / +0.61 / +0.11 ns | pass |
| Max slew violations (slow) | 0 | 31 | fail |
| Routing DRC, Magic DRC, LVS, antenna | 0 | 0 / 0 / 0 / 0 | pass |
| Tiny Tapeout precheck (KLayout DRC, pins, layers) | pass | pass | pass |
| RTL tests / gate-level tests | pass | 20/20, 20/20 | pass |
| Utilization | | 152 010 µm² = 16.8 % of the core | |
| Detailed routing | | 8 883 initial violations, 34 iterations, 2 h 43 min | |

The flow reported success because LibreLane treats only typical-corner
violations as errors for this PDK (`TIMING_VIOLATION_CORNERS = *typ*`).

Root causes and the configuration fix are in `pnr/README.md`. With a first
version of the fix alone (same v2 RTL, local replay of the flow; realistic
wire RC, 40 % density, post-global-route repair, 3.5 ns resizer margin):
slow-corner setup **+3.75 ns**, hold +0.10 ns (fast), 0 routing DRC, 0
antenna, detailed routing 5 iterations.

## 2. Candidates

Area is pre-layout standard-cell area (Yosys + ABC, typical library; v2 =
119 600 µm², 1 076 flops). The core area of the tile is 902 417 µm².

| | A: 64-word IMEM | B: pattern branch `JPAT` | C: background clock | D: dual core |
|---|---|---|---|---|
| **Area** | +38 700 µm² (+512 flops, 64:1 read mux, 6-bit pc) | +3 300 µm² | +2 000 µm² (27 flops) | +86 000 µm² sharing one IMEM (core, divider, two FIFOs, config, second read tree), +157 000 µm² with its own |
| **Utilization, pre-layout** | 13.3 % → 17.5 % | +0.4 % | +0.2 % | v3 18.1 % → 28–36 % (36–46 % placed, with hold and repair buffers) |
| **Timing** | Read mux gains one 4:1 level. Left to synthesis, the 64:1 mux becomes 64 decoded word lines + AND-OR trees, which cost about 6 ns of slow-corner slack before timing repair and 20 % more wire. Built as an explicit mux4 tree with a single, time-shared read port, the prefetch path ends up level with the execute path | Masked 8-bit compare in the branch decision, parallel to the `JMP` conditions; same path class as `JPIN` | One mux at the pads, after the output registers; not on a critical path | Core logic unchanged, but twice the cells on a stack with one horizontal signal layer; v2 at template settings already needed 34 routing iterations |
| **Opcode / bitfield fit** | `JMP` has no spare bit: target bit 5 = opcode bit 3 (opcodes 1 and 9; `DLY` moves 9 → A). `JPIN` uses one of its two reserved bits. `MOV PC` takes 6 bits. `WRAP_*`/`ENTRY` 6 bits. Host commands re-encoded for a 6-bit address | Free opcode B, class B: expected-source, invert, 4-bit window base, 6-bit target = 16 bits exactly. Mask and value in two new config registers | CTL function 8 (free). Two new config registers. Input pin 15 (was constant 0) reads the level | No instruction change. The 8-bit host command has no bit left for a core select: two-byte commands or a select register |
| **Pins** | — | — | Takes over one output pin when enabled | 5 host + 19 target pins stay: both cores must share 15 outputs and 12 inputs through ownership muxes |
| **Protocol capability** | Whole transactions in one program: I2C register read (56 words), two USB LS tokens (45), "Hello!" over UART without the FIFO (57) | Multi-pin conditions in one cycle without sampling skew: bus address + strobe, USB SE0, quadrature and handshake states, compare against X | Free-running bit/reference clock at zero instruction cost (synchronous serial, MCLK, PWM carrier), gated bursts, internal timeout timebase | Two independent protocols at once (for example UART RX and TX, or a bridge) |
| **Verification cost** | Model, CRV, formal and tests widen with the pc | Local: one compare; formal property + CRV | Local: 27 flops; formal properties + CRV | Model, lockstep CRV and formal for two cores plus pin and FIFO arbitration |
| **Decision** | **Implemented** | **Implemented** | **Implemented** | **Not implemented** |

D is rejected for this tapeout: it does not fit the pin budget without
arbitration logic that would itself need a new host protocol, it roughly
doubles routing demand in a process where routing (not area) is the limit,
and it doubles the verification scope. A, B and C together cost 38 % more
synthesized area than v2 (117 700 → 162 800 µm² in the LibreLane synthesis
step).

## 3. Result with A + B + C

Tiny Tapeout `gds` CI run on commit 359f960 (`ci-results/cmos5l-359f960/`)
and local replays of the same flow with the final `src/config.json`
(`pnr/reports/v3-local/`).

| | v2, CI (template config) | v2, local replay (final config) | v3, local replay (final config) | **v3, CI (final config)** |
|---|---|---|---|---|
| Setup slack typ / slow / fast | +4.26 / **−5.23** / +9.80 ns | +10.41 / +4.81 / +13.66 ns | +10.27 / +4.68 / +13.53 ns | +10.57 / **+5.13** / +13.74 ns |
| Hold slack typ / slow / fast | +0.29 / +0.61 / +0.11 ns | +0.31 / +0.66 / +0.12 ns | +0.34 / +0.69 / +0.14 ns | +0.33 / +0.68 / **+0.13** ns |
| Slew violations (slow corner) | 31 | 0 | 0 | 0 |
| Routing DRC / antenna violations | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Cells, area, utilization | 10 215, 152 010 µm², 16.8 % | 10 347, 154 059 µm², 17.1 % | 11 925, 211 053 µm², 23.4 % | 11 932, 211 497 µm², 23.4 % |
| Routed wirelength | 616 mm | 669 mm | 669 mm | 678 mm |
| Detailed routing | 34 iterations, 2 h 43 min (CI runner) | 5 iterations, 16 min (2 cores) | 6 iterations, 13 min (2 cores) | 28 min (CI runner) |
| Magic DRC / LVS / precheck | clean | not run locally | not run locally | **clean** (0 / 0 / all pass) |
| Gate-level tests | 20/20 | not run | 28/28 | 28/28 |

Under the same flow and configuration, A + B + C cost 0.13 ns of slow-corner
setup slack against v2 (+4.81 → +4.68 ns). The equal total wirelength is a
coincidence: the per-layer split differs (Metal2/3/4 = 207/376/86 mm in v2,
241/385/44 mm in v3).

The CI run confirms the local replay: same worst path, slow-corner slack
0.45 ns better than predicted, hold within 0.01 ns.

The two worst slow-corner path groups in v3 (local run) are the prefetch path
(instruction register → branch decision → read mux → instruction register,
+4.68 ns) and the execute path (instruction register → shifter → ISR,
+5.43 ns).

Microarchitecture changes that came with A:

* **Single time-shared read port.** v2 had a core read mux and a host
  readback mux. v3 has one 64:1 tree: it serves the core while it runs and
  in its last BOOT cycle, and the host readback during the rest of BOOT
  (`READ_IMEM` is BOOT-only). The host side is registered.
* **Early branch decision.** Branches never stall, so the next read address
  depends only on registers; the late stall/tick logic only chooses between
  "take the prefetched word" and "hold".
* **Write gate.** imem/cfg writes are accepted only while the core stays in
  BOOT for one more cycle, so everything the core loads when it starts is
  stable; this replaced the v2 write-bypass mux.
* **Explicit mux4 tree** (`src/pesm_mux4.v`), instantiating the library's
  4:1 mux when the IHP standard-cell library is the synthesis target.
