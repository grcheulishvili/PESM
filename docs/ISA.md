# PESM v2 — Instruction Set, Timing Model and Host Protocol

This is the normative reference. `src/pesm_core.v`, `src/pesm_host.v`,
`test/pesm_model.py` and `tools/pesm_asm.py` all implement this document.

## 1. Execution model

* One instruction per `clk` (50 MHz). No fetch stage: `imem` is a 32 × 16 flop
  array read combinationally at `pc`.
* A free-running **tick grid** comes from the 16.8 fractional divider
  (`DIV_INT + DIV_FRAC/256` clk per tick, each period is `DIV_INT` or
  `DIV_INT+1`; `DIV_INT = 0` → tick every clk). The grid only matters to:
  * the 4-bit **pre-delay** field of class-A instructions,
  * `DLYT` (delay in ticks),
  * `WAIT ... SYNC/SYNCHALF` and `SYNC`, which re-phase the grid.
* **Pre-delay `[d]`, d > 0**: the instruction executes on the *d-th tick pulse*
  counted from (and including) the cycle in which it reaches `pc`. Every
  grid-aligned instruction therefore executes exactly on a tick cycle; setup
  instructions without a delay run back-to-back at clk rate in between.
  Rule: the instructions between two grid points must take fewer cycles
  than one tick period, otherwise the next grid point is skipped (still
  deterministic).
* **Side-set** is applied in every cycle the instruction *executes*,
  including stall cycles of `WAIT`, `PULL`, `PUSH`, `OUT` (autopull) and
  `IN` (autopush). If the main operation writes the same pin, the main
  operation wins.
* **Stalls** never commit partial state: a stalled instruction changes
  nothing except side-set pins (and the internal `DLY` counter).
* **Wrap**: when an instruction completes without a taken jump and
  `pc == WRAP_TOP`, the next `pc` is `WRAP_BOT` (PIO semantics). Taken jumps
  are never redirected.
* **Inputs** pass through 2-flop synchronizers: a pad change is visible to the
  core 2–3 clk later. All timing statements are relative to the synchronized
  value.
* **RX FIFO write port is registered**: a byte pushed in cycle t is in the
  FIFO (host-visible) at the end of cycle t+1. The core's view of "RX full"
  (`PUSH`/autopush stalls, input pin `rxnf`) counts that pending write, so
  back-to-back pushes stop at exactly 8 entries. The instruction fetch is a
  prefetch register (`instr <= imem[next_pc]`). It changes no visible
  timing: still one instruction per clock, no branch delay.
* **BOOT** (`MODE = 1`): the core is held in reset, `pc = ENTRY`, X/Y/ISR/OSR
  cleared, OSR marked empty, pins forced to `INIT_*` config values, divider
  phase reset. `imem`/config are writable only in BOOT. The first cycle after
  BOOT is released is a tick cycle.

## 2. Pin spaces

| Output index | Pad | Notes |
|---|---|---|
| 0–7 | `uio[0..7]` (BIO) | `OUT`, plus per-pin output enable (`DIR`); optional open-drain (`OD_MASK`) |
| 8–14 | `uo_out[1..7]` (TOUT0–6) | dedicated outputs, direction ignored |
| 15 | — | writes ignored |

| Input index | Source |
|---|---|
| 0–7 | `uio_in[0..7]` (pad level, also reads back own drive) |
| 8–11 | `ui_in[4..7]` (TIN0–3) |
| 12 | TX FIFO not empty (`txne`) |
| 13 | RX FIFO not full (`rxnf`) |
| 14 | host flag (`hflag`, set/cleared by host, cleared by `HCLR`) |
| 15 | constant 0 |

Pin arithmetic for multi-pin `IN`/`OUT` and side-set is modulo 16.

Open drain (`OD_MASK[i] = 1`): `uio_out[i] = 0`, `uio_oe[i] = OE[i] & ~OUT[i]`,
i.e. writing 1 releases the line, writing 0 pulls it low (if `OE[i] = 1`).

## 3. Registers

| Name | Width | Notes |
|---|---|---|
| `X`, `Y` | 8 | scratch / loop counters / 16-bit `{Y,X}` CRC register |
| `OSR`, `ISR` | 32 | shift registers |
| `osr_cnt` | 0–32 | bits shifted out since last (auto)pull; 32 = empty after BOOT |
| `isr_cnt` | 0–32 | bits shifted in since last (auto)push |
| `LB` | 1 | last bit: bit 0 of the most recent `IN`/`OUT` data chunk |

Byte view (used by `PUSH`, `MOV` from ISR/OSR): `ISR[31:24]` when IN shifts
right, else `ISR[7:0]`; `OSR[7:0]` when OUT shifts right, else `OSR[31:24]`.
Loading a byte into OSR (pull, `MOV OSR`) places it at `OSR[7:0]` (right) or
`OSR[31:24]` (left).

## 4. Encoding

```
 15  12 11                     4 3      0
+------+------------------------+--------+
|  op  |       operand          |  tail  |   class A: op 0,1,3,4,5,6,7
+------+------------------------+--------+
|  op  |          payload (12 bits)      |   class B: op 2,8,9
+------+---------------------------------+
```

`tail` of class-A instructions is split by `SIDE_COUNT` (sc, 0–4):
`tail[3:4-sc]` = side-set value (bit k → pin `SIDE_BASE+k`),
`tail[3-sc:0]` = pre-delay. Opcodes 0xA–0xF are illegal: `ERR` + `HALT`.

### 0x0 CTL — `0000 ffff aaaa tttt`

| f | Mnemonic | a (bits 5,4) | Semantics |
|---|---|---|---|
| 0 | `nop` | — | |
| 1 | `halt` | — | stop; `HALTED` status; leave via BOOT |
| 2 | `irq` | — | set sticky `IRQ` status flag |
| 3 | `push [iffull] [noblock]` | 5 = iffull, 4 = block | if `!iffull or isr_cnt ≥ PUSH_THRESH`: RX full → stall (block) or drop + `RX_OVF` + clear ISR; else push byte view, clear ISR |
| 4 | `pull [ifempty] [noblock]` | 5 = ifempty, 4 = block | if `!ifempty or osr_cnt ≥ PULL_THRESH`: TX empty → stall (block) or `OSR ← X`; else pop into OSR; `osr_cnt ← 0` |
| 5 | `sync [half]` | 4 = half | re-phase grid: next tick in `DIV_INT` (or `max(1,DIV_INT/2)`) cycles |
| 6 | `hclr` | — | clear host flag |
| 7 | `clr isr\|osr\|all` | 4 = ISR, 5 = OSR | ISR ← 0, isr_cnt ← 0 / OSR ← 0, osr_cnt ← 32 |
| 8–15 | — | — | reserved, execute as `nop` |

Assembler default is `block`.

### 0x1 JMP — `0001 ccc ttttt tttt`

`c`: 0 always · 1 `!x` (X = 0) · 2 `x--` (X ≠ 0, X decremented regardless) ·
3 `!y` · 4 `y--` · 5 `x!=y` · 6 `!osre` (`osr_cnt < PULL_THRESH`) · 7 `lb` (LB = 1).
Bits 8:4 = target.

### 0x2 JPIN (class B) — `0010 pppp l 00 ttttt`

Jump to `t` if input pin `p` == `l`. No side-set, no delay.

### 0x3 WAIT — `0011 pppp P E S H tttt`

`E = 0`: stall until input `p` == `P`.
`E = 1`: stall until an edge (P = 1 rising, P = 0 falling) is seen *in the
current cycle* (synchronized value vs. previous cycle). No latching of edges
that happened before the WAIT started.
`S = 1`: on release, re-phase the grid (H = 1 → half period). Used to align
sampling to the middle of a UART bit.

Assembler: `wait high|low|rise|fall <pin> [sync|synchalf]`.

### 0x4 IN — `0100 0 bbbb nnn tttt` / `0100 1 ss nnnnn tttt`

Pins form: shift `n+1` (1–8) bits from input pins `b, b+1, …`.
Register form: `s` = 0 X, 1 Y, 2 NULL (zeros), 3 OSR; `n` = 1–32 (0 = 32).
Shift direction by `IN_SHIFTDIR`. `isr_cnt` saturates at 32.
Autopush: if enabled and the new `isr_cnt ≥ PUSH_THRESH`: RX full → whole
instruction stalls; else the byte view of the shifted ISR is pushed and ISR is
cleared in the same cycle.

### 0x5 OUT — `0101 0 bbbb nnn tttt` / `0101 1 dd nnnnn tttt`

Pins form: `n+1` (1–8) bits to output pins `b, b+1, …`.
Register form: `d` = 0 X, 1 Y, 2 NULL, 3 PINDIRS (OE of BIO 0..n-1); `n` 1–32.
Autopull: if enabled and `osr_cnt ≥ PULL_THRESH` *before* the shift: TX empty
→ stall; else OSR is refilled from the TX FIFO and the OUT uses the new data
in the same cycle (no bubble).

### 0x6 SETP — `0110 pppp ff v 0 tttt`

`f`: 0 `set p, v` (OUT) · 1 `dir p, v` (OE, BIO only) · 2 `toggle p`
(inverts the pre-instruction OUT value) · 3 `drive p, v` (OUT = v and OE = 1).

### 0x7 MOV — `0111 ddd oo sss tttt`

`d`: 0 X · 1 Y · 2 ISR (byte placed per IN dir, `isr_cnt ← 8`) · 3 OSR
(byte placed per OUT dir, `osr_cnt ← 0`) · 4 PINS (OUT 0–7) · 5 PINDIRS
(OE 0–7) · 6 TOUT (OUT 8–14) · 7 PC (computed jump to value[4:0]).
`s`: 0 X · 1 Y · 2 ISR byte · 3 OSR byte · 4 inputs 0–7 · 5 inputs 8–15 ·
6 zero · 7 LB.
`o`: 0 copy · 1 invert `~` · 2 bit-reverse `rev` · 3 parity `par` (XOR-reduce
to bit 0).

`mov isr, pins` is the atomic 8-pin sample.

### 0x8 ALU (class B) — `1000 r fff iiiiiiii`

`r`: 0 X, 1 Y. `f`: 0 `ldi` · 1 `and` · 2 `or` · 3 `xor` · 4 `dec` · 5 `inc`
· 6 `crc lsb|msb` · 7 reserved (nop).
`crc` operates on `{Y,X}` with `CRC_POLY`, input bit = LB:
* lsb (reflected): `fb = crc[0]^LB; crc = (crc >> 1) ^ (fb ? POLY : 0)`
* msb: `fb = crc[15]^LB; crc = (crc << 1) ^ (fb ? POLY : 0)` (left-align
  CRCs shorter than 16 bits).

### 0x9 DLY (class B) — `1001 u x nnnnnnnnnn`

`u = 0` (`dly`): occupy exactly `N + 1` cycles. `u = 1` (`dlyt`): retire on
the N-th tick pulse counted from the first execution cycle (inclusive).
`x = 1`: `N = {X, Y}` (16 bit), else the 10-bit immediate. `N = 0` → 1 cycle.

## 5. Configuration registers (host `WRITE_CFG`, BOOT only)

| Addr | Name | Bits | Reset |
|---|---|---|---|
| 0 | `DIV_INT[7:0]` | 8 | 0 |
| 1 | `DIV_INT[15:8]` | 8 | 0 |
| 2 | `DIV_FRAC` | 8 | 0 |
| 3 | `SHIFTCTRL` | 0 OUT right, 1 IN right, 2 AUTOPULL, 3 AUTOPUSH | 0x03 |
| 4 | `PULL_THRESH` | 5 (0 = 32) | 8 |
| 5 | `PUSH_THRESH` | 5 (0 = 32) | 8 |
| 6 | `SIDECTRL` | 2:0 count (≥4 → 4), 3 side→PINDIRS, 7:4 base pin | 0 |
| 7 | `WRAP_TOP` | 5 | 31 |
| 8 | `WRAP_BOT` | 5 | 0 |
| 9 | `ENTRY` | 5 | 0 |
| 10 | `OD_MASK` | 8 | 0 |
| 11 | `CRC_POLY[7:0]` | 8 | 0 |
| 12 | `CRC_POLY[15:8]` | 8 | 0 |
| 13 | `INIT_BIO_OUT` | 8 | 0 |
| 14 | `INIT_BIO_OE` | 8 | 0 |
| 15 | `INIT_TOUT` | 7 | 0 |

## 6. Host SPI protocol

SPI mode 0, MSB first, `CS_N` frames a transaction. Oversampled in the clk
domain: `SCK` high and low phases must each be ≥ 4 clk (f_SCK ≤ f_clk/8;
6.25 MHz at 50 MHz). MISO changes ≈ 3–4 clk after the SCK falling edge.

Byte 0 is the command. MISO carries `STATUS0` during the command byte.

| Cmd | Name | Data phase |
|---|---|---|
| `000aaaaa` | WRITE_IMEM | `{hi, lo}` pairs → `imem[a++]` (wraps 31→0). BOOT only, else `ERR` |
| `001aaaaa` | READ_IMEM | MISO: `hi, lo` of `imem[a++]` … |
| `010-aaaa` | WRITE_CFG | bytes → `cfg[a++]`. BOOT only, else `ERR` |
| `011-aaaa` | READ_CFG | MISO: `cfg[a++]` … |
| `100-----` | WRITE_TX | bytes → TX FIFO (full → dropped, `TX_OVF`) |
| `101-----` | READ_RX | MISO: RX FIFO bytes (empty → 0x00, `RX_UNF`) |
| `110-----` | READ_STATUS | MISO: `STATUS0, STATUS1, PC, X, Y, 0, …` |
| `111fffff` | CONTROL | f0 FLUSH_TX, f1 FLUSH_RX, f2 HFLAG_SET, f3 HFLAG_CLR, f4 CLR_FLAGS |

`STATUS0 = {RUNNING, HALTED, IRQ, ERR, TX_OVF, RX_OVF, RX_UNF, HFLAG}`,
`STATUS1 = {TX_LEVEL[3:0], RX_LEVEL[3:0]}`.

An RX FIFO pop is committed on the first SCK rising edge of the byte that
carries it; ending a frame early never loses a byte.
