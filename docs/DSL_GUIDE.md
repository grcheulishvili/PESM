# PESM Python Toolchain and DSL Guide

How to write a protocol in Python, compile it, load it into the chip and
talk to it. The instruction set itself is specified in `docs/ISA.md`.

## Contents

| Section | Topic |
|---|---|
| [1. Overview](#1-overview) | package modules, installation |
| [2. Quick start](#2-quick-start-from-python-to-pins) | first program, from Python to pins |
| [3. Execution model](#3-execution-model) | six rules, pins, register rules |
| [4. `PESMProgram` API reference](#4-pesmprogram-api-reference) | configuration, instructions, macros, output formats, text assembler |
| [5. Examples](#5-examples) | UART, SPI, I2C, USB low speed, pattern branch, background clock, CRC, timing |
| [6. Host side](#6-host-side) | wiring, programmer API, load sequence, monitor, one-shot pipeline |
| [7. Troubleshooting](#7-troubleshooting) | symptom → check |
| [8. How the toolchain is checked](#8-how-the-toolchain-is-checked) | tests behind this guide |

---

## 1. Overview

The `sw/pesm` package covers the whole user workflow:

```
 Python DSL (.py)  --+
                     +--> Image (64 words + 20 config bytes) --> programmer --> chip
 assembler (.pasm) --+        |                                                  |
                              +--> .bin .mem .py .json .lst .asm                 +--> TX/RX FIFOs, status
```

| Module | Purpose |
|---|---|
| `pesm.isa` | encoders, exact disassembler, `Config` (20 registers), `Image` (64 words + 20 config bytes) and output formats |
| `pesm.builder` | **Python DSL**: `PESMProgram` with chainable instructions and protocol macros |
| `pesm.assembler` | text assembler (`.pasm` / `.asm`), same encoders as the DSL |
| `pesm.usb` | USB packet helpers: PIDs, CRC5/CRC16, NRZI + bit-stuffing encoder and decoder |
| `pesm.hostproto` | SPI host-port frames and `Status` decoding |
| `pesm.programmer` | host programmer: FT232H (pyftdi), Linux spidev, or emulator backends; runtime monitor |
| `pesm.emulator` | pure-Python chip (host port + cycle-accurate core model) for dry runs |
| `pesm.run_pipeline` | one call or command: source → image → flash → verify → run → TX/RX/monitor |

### Installation

```
pip install -e sw            # or: export PYTHONPATH=$PWD/sw
pip install pyftdi           # FT232H backend
pip install spidev gpiod     # Raspberry Pi / Linux backend
```

---

## 2. Quick start: from Python to pins

1. **Write the protocol** (`blink_uart.py`):

   ```python
   from pesm.builder import PESMProgram

   program = PESMProgram("hello").clock(tick_hz=115200).init_pins(tout=0x01)
   for ch in b"Hello!\r\n":
       program.macro_uart_tx_byte("tout0", ch)
   program.halt()
   ```

   That is 8 × 7 + 1 = 57 words. `program.listing()` prints the code.
   `compile()` raises an error if it exceeds 64 words or a delay does not
   fit the side-set configuration.

2. **Try it without hardware**:

   ```
   python -m pesm.run_pipeline blink_uart.py --backend emulator --wait-halt --listing
   ```

3. **Wire it up**: FT232H as in section 6, a USB-UART adapter RX on
   `uo_out[1]` (TOUT0), common ground.

4. **Flash and run**:

   ```
   python -m pesm.run_pipeline blink_uart.py --backend ftdi --url ftdi://ftdi:232h/1 --wait-halt
   ```

   The programmer verifies the readback before releasing MODE. Your
   terminal shows `Hello!`.

5. **Iterate**: change the program and run again. To patch one word, call
   `Programmer.patch(addr, [word])` while in BOOT.

---

## 3. Execution model

### Six rules

1. **One instruction per clock** (50 MHz), 64 instruction words. Setup work
   (loads, ALU, branches) is effectively free between bit edges.
2. **Tick grid.** `clock(tick_hz=…)` sets a fractional divider. An
   instruction with `delay=d` executes on the *d-th tick* from the cycle it
   is reached (`d=1`: next grid point). Give every bit edge `delay=1` and the
   edges land exactly on the grid, whatever runs in between, as long as that
   takes fewer cycles than one tick.
3. **Side-set** (`side=`) changes up to 4 pins in the same cycle as the
   instruction. Configure it with `sideset(count, base)`. With side-set on,
   *every* class-A instruction drives the side pins (default value 0), and
   the `delay` field shrinks to `4 - count` bits.
4. **Inputs are synchronized**: a pad change is seen 2-3 clocks later.
5. **Stalls never half-execute.** `pull`, `push`, `wait_pin`, autopull `OUT`
   and autopush `IN` either complete or change nothing except side-set pins.
6. **Background clock.** One generator, tied to the tick grid, can drive a
   pin with a free-running clock and is readable as input pin `bgclk`. It
   costs no instructions.

### Pins

| Direction | Names | Pads |
|---|---|---|
| Output | `bio0..bio7` | bidirectional `uio`, per-pin direction, optional open drain |
| Output | `tout0..tout6` | `uo_out[1..7]` |
| Input | `bio0..bio7` | `uio_in[0..7]` |
| Input | `tin0..tin3` | `ui_in[4..7]` |
| Input (status) | `txne` | TX FIFO not empty |
| Input (status) | `rxnf` | RX FIFO not full |
| Input (status) | `hflag` | host flag |
| Input (status) | `bgclk` | background clock level |

### Register rules

| Register | Width | Rules |
|---|---|---|
| `X` | 8 | general purpose; loop counter (`jmp_dec`); expected value of `jmp_if_pattern(expect='x')`; substitute data for a non-blocking `pull` on an empty FIFO; low byte of the CRC register |
| `Y` | 8 | general purpose; loop counter; high byte of the CRC register |
| `{X,Y}` | 16 | count for `wait_cycles/wait_ticks(use_xy=True)` (X = high byte) |
| `OSR` | 32 | output shift register. Loaded 8 bits at a time (`pull`, autopull, `mov('osr', …)`); placed at the end the shift direction consumes first |
| `ISR` | 32 | input shift register. `push`/autopush send the byte the shift direction filled last |
| `LB` | 1 | bit 0 of the last `shift_in`/`shift_out` data; input of `crc_step`, condition of `jmp_if_lastbit` |
| `pc` | 6 | `jmp_reg(reg)` loads `reg[5:0]` |

* The built-in macros use **X** as their bit counter and state; the USB
  macros use **X and Y**. `crc_step` uses `{Y,X}`. Each macro's docstring and
  the macro table in section 4 list what it clobbers.
* There is no register-to-register arithmetic: ALU operations take an 8-bit
  immediate (`ldi/and_/or_/xor`), plus `inc/dec`, and `mov` with
  invert / bit-reverse / parity.
* `BOOT` clears X, Y, ISR, OSR and marks the OSR empty.

---

## 4. `PESMProgram` API reference

`PESMProgram(name="pesm", clk_hz=50_000_000)`

* All methods return the program, so calls chain.
* Class-A instructions accept `side=` and `delay=`.
* Branch targets are label strings (forward references allowed) or absolute
  addresses 0-63.

### 4.1 Configuration

| Method | Effect |
|---|---|
| `clock(tick_hz=None, divider=None, div_int=None, div_frac=0)` | tick grid (`divider` in clk cycles, fractional) |
| `shift(out=None, in_=None)` | `'right'` = LSB first, `'left'` = MSB first |
| `autopull(threshold=8, enable=True)` / `autopush(threshold=8, enable=True)` | automatic OSR refill / ISR push |
| `thresholds(pull=None, push=None)` | thresholds without enabling auto mode |
| `sideset(count, base=0, pindir=False)` | 0-4 side pins from `base`; `pindir` drives OE instead of OUT |
| `open_drain(*pins)` | BIO pins become open drain (1 = release, 0 = pull low) |
| `crc_poly(poly)` | 16-bit polynomial for `crc_step` |
| `pattern(mask, value=0)` | pattern-compare registers for `jmp_if_pattern` / `wait_pattern` |
| `pattern_pins({pin: level, …}, base=0)` | the same, from pin names; all pins must lie in the 8-input window starting at `base` |
| `background_clock(pin=None, hz=None, div=None, auto=True, idle=0)` | free-running clock, toggling every `div+1` ticks (`hz` is converted; call `clock()` first). `pin=None`: internal timebase only. `auto=False`: stopped at `idle` until `bgclk_start()` |
| `init_pins(bio_out=None, bio_oe=None, tout=None)` | pin state in BOOT and at start |
| `wrap_target()` / `wrap()` | PIO-style zero-cost loop: fall-through from the `wrap()` instruction goes to `wrap_target()` |
| `entry(label)` | start address |
| `label(name)`, `new_label(hint='L')` | define / create a unique label |

Macros apply the configuration they need (for example, SPI sets MSB-first
shifting). A conflicting setting raises `DslError` at the call that causes
the conflict.

### 4.2 Instructions

**Control and FIFO**

| Method | ISA | Notes |
|---|---|---|
| `nop()` | `nop` | use `nop(delay=n)` to wait for a grid point |
| `halt()`, `irq()`, `hclr()` | CTL | `irq` sets a sticky host-visible flag; `hclr` clears `hflag` |
| `push(iffull=False, block=True)` | `push` | ISR byte → RX FIFO |
| `pull(ifempty=False, block=True)` | `pull` | TX FIFO → OSR |
| `sync_grid(half=False)` | `sync` | re-phase the tick grid now |
| `clear(isr=True, osr=False)` | `clr` | |
| `bgclk_start(reset=False)` / `bgclk_stop(reset=False)` | `bgclk on/off` | `reset=True`: level back to idle, full first half-period |

**Branches** (never stall)

| Method | ISA | Notes |
|---|---|---|
| `jmp(target, cond='always')` | `jmp` | `cond`: `!x !y x-- y-- x!=y !osre lb` |
| `jmp_if_zero(reg, t)` / `jmp_dec(reg, t)` | `jmp !r` / `jmp r--` | `jmp_dec` loops `reg+1` times |
| `jmp_if_ne(t)` | `jmp x!=y` | compare-and-branch |
| `jmp_if_osr_not_empty(t)` / `jmp_if_lastbit(t)` | `!osre` / `lb` | |
| `jmp_if_pin(pin, level, t)` | `jpin` | no side/delay |
| `jmp_if_pattern(t, base=0, expect='cfg', match=True)` | `jpat` | up to 8 pins in one cycle: jump if `((window ^ expected) & mask) == 0` (`match=False`: `!= 0`); `expect='x'` compares against X; no side/delay |
| `jmp_reg(reg)` | `mov pc, reg` | computed jump / jump table (`reg[5:0]`) |

**Waits and delays**

| Method | ISA | Notes |
|---|---|---|
| `wait_pin(pin, level, sync=None)` | `wait` | level `0/1/'low'/'high'/'rise'/'fall'`; `sync='full'\|'half'` re-phases the grid on release |
| `wait_pattern(base=0, expect='cfg', match=True)` | `jpat` to itself | one word; stalls until the pattern matches (or stops matching) |
| `wait_cycles(n, use_xy=False)` | `dly` | exactly n clk; > 1024 needs `use_xy` (clobbers X, Y) |
| `wait_ticks(n, use_xy=False)` | `dlyt` | until the n-th tick |

**Pins**

| Method | ISA | Notes |
|---|---|---|
| `set_pin(pin, v)`, `toggle_pin(pin)` | `set` / `toggle` | |
| `dir_pin(pin, output=True)`, `release_pin(pin)` | `dir` | BIO only |
| `drive_pin(pin, v)` | `drive` | OUT = v and OE = 1 in one cycle |
| `set_pins(src)`, `set_pindirs(src)`, `set_tout(src)` | `mov pins/pindirs/tout` | atomic multi-pin write |
| `sample_pins()` | `mov isr, pins` | atomic 8-pin sample |

**Shifting and data**

| Method | ISA | Notes |
|---|---|---|
| `shift_out(dst, n=1)` | `out` | `dst`: pin (n ≤ 8 consecutive pins) or `'x' 'y' 'null' 'pindirs'` (n ≤ 32) |
| `shift_in(src, n=1)`, `get_pin(pin)` | `in` | `src`: pin(s) or `'x' 'y' 'null' 'osr'` |
| `mov(dst, src, op='')` | `mov` | `op`: `'' '~' 'rev' 'par'` |

**ALU, CRC and raw words**

| Method | ISA | Notes |
|---|---|---|
| `ldi/and_/or_/xor(reg, imm)` | ALU | 8-bit immediate |
| `inc(reg)`, `dec(reg)` | ALU | |
| `crc_step(msb=False)` | `crc` | `{Y,X}` Galois step, input bit = last shifted bit |
| `raw(word)` | `.word` | |

### 4.3 Protocol macros

| Macro | Words | Tick | Applies | Clobbers |
|---|---|---|---|---|
| `macro_uart_tx_byte(pin, byte=None, stop_bits=1)` | 7 (+1/extra stop) | baud | OUT right | X, OSR |
| `macro_uart_tx(pin, label='uart_tx')` | 8 | baud | OUT right, line idle high | X, OSR |
| `macro_uart_rx(pin, label='uart_rx', on_framing_error='irq')` | 12 | baud | IN right, autopush 8 | X, ISR |
| `macro_spi_transfer(sclk, mosi, miso, bits=8, source='fifo', sink='fifo')` | 9 | 2·f_SCLK | MSB first, no auto pull/push | X, OSR, ISR |
| `macro_i2c_start(sda, scl)` | 2 | 4·f_SCL | open drain, released | - |
| `macro_i2c_stop(sda, scl)` | 4 | 4·f_SCL | waits out clock stretching | - |
| `macro_i2c_write_byte(sda, scl, nack_label=None, source='fifo')` | 11-12 | 4·f_SCL | MSB first | X, OSR |
| `macro_i2c_read_byte(sda, scl, ack=True, sink='fifo')` | 13 | 4·f_SCL | MSB first, no autopush | X, ISR |
| `macro_crc_osr(msb=False)` | 3 | - | - | X, Y (CRC), OSR |
| `macro_usb_ls_tx(dp='bio0', dm='bio1', label='usb_tx')` | 23 | 1.5 MHz | OUT right, J idle | X, Y, OSR |
| `macro_usb_ls_token(pid, addr=0, endp=0, dp='bio0', dm='bio1', idle_ticks=1)` | 19-31 | 1.5 MHz | J idle, D+/D- driven | X, Y |

The two USB macros:

* `macro_usb_ls_token` sends one complete token (`pid` = `'setup'`, `'out'`,
  `'in'` or a 4-bit PID number) with constant address and endpoint. SYNC,
  PID, CRC5, bit stuffing and NRZI are resolved at compile time; the
  microcode is one atomic D+/D- write per line transition, each on the tick
  grid, followed by SE0-SE0-J.
* `macro_usb_ls_tx` is the FIFO-fed transmitter for arbitrary packets (the
  host supplies SYNC, PID, payload and CRC; `pesm.usb` builds them).

### 4.4 Compiling and output

```python
img = p.compile()            # pesm.isa.Image
img.words, img.cfg           # 64 x 16-bit, 20 x 8-bit
img.to_lists()               # the same two Python lists
img.listing()                # disassembly with labels and source
img.to_bin()                 # 156 bytes: header + imem big-endian + cfg (exact SPI payloads)
img.to_mem(); img.to_cfg_mem()   # $readmemh
img.to_py(); img.to_json()
p.to_bin(); p.to_mem(); p.to_lists()   # straight from the program
p.save("build/uart")         # writes .bin .mem _cfg.mem .py .json .lst .asm
p.to_asm()                   # equivalent .pasm source (assembles to the same image)
```

`compile()` raises `DslError` if the program exceeds 64 words, a label is
undefined, or a delay / side value does not fit the side-set configuration.

### 4.5 Text assembler

The text assembler outputs the same formats:

```
python -m pesm.assembler firmware/uart_tx.pasm                 # listing
python -m pesm.assembler firmware/uart_tx.pasm -f bin  -o uart_tx.bin
python -m pesm.assembler firmware/uart_tx.pasm -f mem  -o uart_tx.mem   # + uart_tx_cfg.mem
python -m pesm.assembler firmware/uart_tx.pasm -f py                    # UART_TX_IMEM / UART_TX_CFG lists
python -m pesm.assembler firmware/uart_tx.pasm -f json -o uart_tx.json
python -m pesm.assembler firmware/uart_tx.pasm --all -o build/uart_tx   # every format
python -m pesm.assembler firmware/uart_tx.pasm --spi                    # host SPI frames
```

Assembler directives:

| Group | Directives |
|---|---|
| Constants | `.define` |
| Tick grid | `.tick_hz`, `.div`, `.div_raw`, `.clk_hz` |
| Shifting and FIFO | `.shift out=… in=…`, `.autopull [n]`, `.autopush [n]`, `.pull_thresh`, `.push_thresh` |
| Side-set | `.side n [base=pin] [pindir]` |
| Program flow | `.wrap_target`, `.wrap`, `.entry label`, `.org n` |
| Pins | `.od mask`, `.init_out`, `.init_oe`, `.init_tout` |
| CRC, pattern, background clock | `.crc_poly p`, `.pattern mask value`, `.bgclk [pin=p] [div=n \| hz=f] [auto] [idle=0\|1]` |
| Raw | `.cfg addr value`, `.word w` |

Mnemonics are listed in `docs/ISA.md`.

---

## 5. Examples

### 5.1 UART

```python
from pesm.builder import PESMProgram

# Transmitter: every byte the host writes to the TX FIFO becomes an 8N1 frame on TOUT0.
tx = PESMProgram("uart_tx").clock(tick_hz=115200)
tx.macro_uart_tx("tout0")

# Receiver on TIN0: bytes appear in the RX FIFO; framing errors set IRQ.
rx = PESMProgram("uart_rx").clock(tick_hz=115200)
rx.macro_uart_rx("tin0")

# Fixed message without the FIFO: "OK"
msg = PESMProgram("hello").clock(tick_hz=9600).init_pins(tout=0x01)
msg.macro_uart_tx_byte("tout0", ord("O")).macro_uart_tx_byte("tout0", ord("K")).halt()
```

### 5.2 SPI (mode 0)

Hand-written with side-set (the fastest form, `sw/examples/spi_master.py`):

```python
p = PESMProgram("spi").clock(tick_hz=2_000_000)            # 1 MHz SCLK
p.shift(out="left", in_="left").autopull(8).autopush(8)
p.sideset(1, base="tout1").init_pins(tout=0x08)             # SCLK side pin, CS_N high
(p.label("idle").set_pin("tout3", 1, side=0)
  .wait_pin("txne", "high", side=0)
  .set_pin("tout3", 0, side=0, delay=1)
  .label("bit").shift_out("tout2", 1, side=0, delay=1)      # MOSI, SCLK falls
  .shift_in("tin1", 1, side=1, delay=1)                     # SCLK rises, sample MISO
  .jmp_if_osr_not_empty("bit", side=1)
  .jmp_if_pin("txne", 1, "bit")
  .nop(side=0, delay=1).jmp("idle", side=0))
```

With the macro (no side-set needed):

```python
p = PESMProgram("spi2").clock(tick_hz=2_000_000).init_pins(tout=0x08)
p.set_pin("tout3", 0, delay=1)
p.macro_spi_transfer("tout1", "tout2", "tin1")      # TX FIFO byte out, reply to RX FIFO
p.macro_spi_transfer("tout1", "tout2", "tin1")
p.set_pin("tout3", 1, delay=1).halt()
```

### 5.3 I2C (100 kHz, open drain, clock stretching)

```python
SDA, SCL = "bio0", "bio1"
w = PESMProgram("i2c_write").clock(tick_hz=400_000)        # 4 ticks per SCL period
w.label("idle").wait_pin("txne", "high").macro_i2c_start(SDA, SCL)
w.label("byte").macro_i2c_write_byte(SDA, SCL, nack_label="nack")
w.jmp_if_pin("txne", 1, "byte")
w.label("stop").macro_i2c_stop(SDA, SCL).jmp("idle")
w.label("nack").irq().set_pin(SCL, 0, delay=2).jmp("stop")

r = PESMProgram("i2c_read").clock(tick_hz=400_000)
r.macro_i2c_start(SDA, SCL).macro_i2c_write_byte(SDA, SCL)  # host queues (addr<<1)|1
r.macro_i2c_read_byte(SDA, SCL, ack=False).macro_i2c_stop(SDA, SCL).halt()
```

The 64-word memory holds a complete register read (write the register
address, then read one byte) in one program, 56 words:

```python
SDA, SCL = "bio0", "bio1"
t = PESMProgram("i2c_reg_read").clock(tick_hz=400_000)
t.wait_pin("txne", "high")
t.macro_i2c_start(SDA, SCL).ldi("y", 1)
t.label("wr").macro_i2c_write_byte(SDA, SCL, nack_label="fail")    # addr + W, then register
t.jmp_dec("y", "wr")
t.macro_i2c_stop(SDA, SCL)
t.macro_i2c_start(SDA, SCL).macro_i2c_write_byte(SDA, SCL, nack_label="fail")   # addr + R
t.macro_i2c_read_byte(SDA, SCL, ack=False)
t.label("done").macro_i2c_stop(SDA, SCL).halt()
t.label("fail").irq().set_pin(SCL, 0, delay=2).jmp("done")
```

### 5.4 USB low speed

```python
# Constant tokens, fully resolved at compile time (sw/examples/usb_ls_token.py)
u = PESMProgram("usb_tokens").clock(tick_hz=1_500_000)
u.wait_pin("hflag", "high")
u.macro_usb_ls_token("setup", addr=0x15, endp=0xE, dp="bio0", dm="bio1", idle_ticks=4)
u.macro_usb_ls_token("in", addr=0x15, endp=0xE, idle_ticks=4)
u.halt()

# FIFO-fed transmitter for arbitrary packets
g = PESMProgram("usb_tx").clock(tick_hz=1_500_000)
g.macro_usb_ls_tx("bio0", "bio1")
```

The host side of the FIFO-fed transmitter:

```python
from pesm import usb

setup = usb.token_packet("setup", addr=0x15, endp=0xE)            # [0x80, 0x2D, 0x15, 0xEF]
data0 = usb.data_packet("data0", [0x80, 0x06, 0x00, 0x01])        # SYNC, PID, payload, CRC16
states = usb.line_states(setup)                                    # 'K','J',... per bit time
assert usb.decode_line(states) == setup
```

### 5.5 Pattern branch: several pins in one cycle

```python
# Parallel bus: data BIO0..3, address BIO4..7, strobe TIN0 (sw/examples/addr_strobe_capture.py)
b = PESMProgram("capture").shift(in_="left")
b.pattern_pins({"bio4": 0, "bio5": 1, "bio6": 0, "bio7": 1, "tin0": 1}, base="bio4")
b.label("idle").jmp_if_pattern("idle", base="bio4", match=False)   # wait: address 0xA and strobe
b.sample_pins().push()                                             # all 8 BIO pins -> RX FIFO
b.wait_pattern(base="bio4", match=False)                           # until released
b.jmp("idle")

# USB: branch on SE0 (D+ and D- both low) in a receiver
s = PESMProgram("se0").pattern_pins({"bio0": 0, "bio1": 0})
s.label("rx").jmp_if_pattern("eop").jmp("rx")
s.label("eop").irq().halt()

# Compare against a run-time value: X is the expected window
x = PESMProgram("match_x").pattern(mask=0xFF)
x.pull().shift_out("x", 8)                 # expected byte from the host
x.wait_pattern(expect="x")                 # stall until BIO0..7 == X
x.irq().halt()
```

* `jmp_if_pin` tests one pin; the pattern branch replaces a chain of them
  (and the race between their sample times) with one masked compare of the
  8-input window that starts at `base`.
* Window positions wrap through the input space: `base="tin0"` gives
  TIN0..3, `txne`, `rxnf`, `hflag`, `bgclk`.

### 5.6 Background clock

```python
# Free-running 1 MHz bit clock on TOUT1; data follows the falling edge
# (sw/examples/sync_serial_tx.py). The clock costs no instructions.
c = PESMProgram("sync_tx").clock(tick_hz=2_000_000).shift(out="left").thresholds(pull=8)
c.background_clock(pin="tout1", hz=1_000_000, auto=True, idle=0)
c.wrap_target()
c.pull(ifempty=True).wait_pin("bgclk", "fall").shift_out("tout2", 1)
c.wrap()

# Gated clock burst: 16 half-periods on BIO3, started and stopped by the program
g = PESMProgram("burst").clock(tick_hz=1_000_000).init_pins(bio_oe=0x08)
g.background_clock(pin="bio3", div=0, auto=False, idle=1)
g.wait_pin("hflag", "high").hclr()
g.bgclk_start(reset=True, delay=1)         # first edge one tick later
g.wait_ticks(16)
g.bgclk_stop(reset=True).halt()            # back to the idle level

# Internal timebase (no pin): time out a wait after 256 ticks
w = PESMProgram("timeout").clock(tick_hz=100_000)
w.background_clock(div=255, auto=False)
w.bgclk_start(reset=True)
w.label("poll").jmp_if_pin("tin0", 1, "got").jmp_if_pin("bgclk", 0, "poll")
w.irq().halt()                             # timed out
w.label("got").halt()
```

* Frequency: `f = f_tick / (2 · (div + 1))`; the edges coincide with
  tick-grid instructions.
* `sync_grid()` and `wait_pin(…, sync=…)` re-phase the clock together with
  the grid.
* Input pin `bgclk` is not delayed by a synchronizer:
  `wait_pin("bgclk", "rise")` completes one clk after the edge and the next
  instruction acts on the second.

### 5.7 CRC (CRC-16/USB, CRC-5/USB, CAN CRC-15)

```python
c = PESMProgram("crc16").shift(out="right", in_="right").crc_poly(0xA001)
c.ldi("x", 0xFF).ldi("y", 0xFF)
c.label("loop").jmp_if_pin("txne", 1, "have").jmp_if_pin("hflag", 1, "done").jmp("loop")
c.label("have").pull().macro_crc_osr().jmp("loop")
c.label("done").xor("x", 0xFF).xor("y", 0xFF)
c.mov("isr", "x").push().mov("isr", "y").push().hclr().halt()
```

* Reflected CRCs (USB): `crc_step(msb=False)`, register right-aligned in
  `{Y,X}`. CRC-5/USB uses poly `0x14`, init `0x1F`.
* MSB-first CRCs (CAN CRC-15 `0x4599`): left-align the register and the
  polynomial (`crc_poly(0x4599 << 1)`), use `crc_step(msb=True)`; the result
  is in bits 15..1.

### 5.8 Timing helpers

```python
p = PESMProgram("timing").clock(divider=8)
p.wait_cycles(3)              # exactly 3 clk
p.wait_cycles(20000, use_xy=True)
p.wait_ticks(10)              # 10 grid periods
p.wait_pin("tin2", "rise", sync="half")   # align the grid to mid-bit after an edge
```

---

## 6. Host side

### 6.1 Wiring

FT232H:

| FT232H | PESM | |
|---|---|---|
| AD0 (SCK) | `ui_in[0]` HOST_SCK | ≤ f_clk/8 (6.25 MHz at 50 MHz) |
| AD1 (MOSI) | `ui_in[1]` HOST_MOSI | |
| AD2 (MISO) | `uo_out[0]` HOST_MISO | |
| AD3 (CS0) | `ui_in[2]` HOST_CS_N | |
| AD4 (GPIO) | `ui_in[3]` MODE | 1 = BOOT, 0 = RUN |
| AD5 (GPIO, optional) | `rst_n` | |
| GND | GND | common ground, 3.3 V I/O |

Raspberry Pi (`--backend spidev`): SPI0 SCLK/MOSI/MISO/CE0 to the same
pins, MODE on any GPIO (`--mode-gpio gpiochip0:17`), optional reset
(`--reset-gpio gpiochip0:27`).

### 6.2 Programmer API

```python
from pesm.programmer import FtdiTransport, Programmer

with FtdiTransport("ftdi://ftdi:232h/1", freq=1e6) as t:
    p = Programmer(t, log=print)
    p.load(img)                # MODE=1, flush, check ID, write cfg + imem, read back and compare
    p.patch(63, [0x0100])      # random-access fix-up while in BOOT
    p.run()                    # MODE=0: core starts at ENTRY
    p.write_tx(b"hello")       # flow-controlled: never overflows the 8-byte FIFO
    print(p.read_rx(5))        # waits for 5 bytes (timeout)
    print(p.status())          # pc, X, Y, FIFO levels, RUNNING/HALTED/IRQ/ERR/...
    m = p.monitor(duration=2.0, interval=0.01,
                  on_status=print, on_rx=lambda d: print(bytes(d)))
```

`Programmer.reset_chip()` pulses `rst_n` if the transport has it wired.

### 6.3 Load sequence

What `Programmer.load()` followed by `Programmer.run()` does on the wire:

| Step | Action |
|---|---|
| 1 | MODE high (core held) |
| 2 | CONTROL (flush, clear flags) |
| 3 | READ_STATUS (chip ID 0x30) |
| 4 | `WRITE_CFG 0` + 20 bytes |
| 5 | `WRITE_IMEM 0` + 64 words |
| 6 | `READ_IMEM` / `READ_CFG` readback over MISO and compare (retries, then `VerifyError`) |
| 7 | MODE low, which releases the core |

Every frame is one CS_N-low transaction. MISO returns STATUS0 during the
command byte. The command bytes are listed in `docs/ISA.md` section 6.

### 6.4 Runtime monitor

`Programmer.monitor(duration=None, interval=0.01, drain_rx=True,
until_halt=False, on_status=None, on_rx=None, max_polls=None)`

* polls READ_STATUS,
* drains the RX FIFO as bytes arrive,
* calls `on_status` on every change of `pc`, flags or FIFO levels,
* returns a `MonitorResult` (`statuses`, `rx`, `polls`, `final`).

Sticky flags are reported, not cleared.

| `Status` field | Meaning |
|---|---|
| `running` / `halted` | core state (`halted` also after an illegal opcode) |
| `irq` | set by the `irq` instruction |
| `err` | illegal opcode, or an imem/cfg write outside BOOT |
| `tx_ovf` / `rx_unf` | host wrote a full TX FIFO / read an empty RX FIFO |
| `rx_ovf` | a non-blocking `push` hit a full RX FIFO |
| `hflag` | host flag |
| `tx_level`, `rx_level` | FIFO fill, 0-8 |
| `pc`, `x`, `y`, `chip_id` | live register values, `0x30` |

### 6.5 One-shot pipeline

Command line:

```
# DSL file (defines `program` or build())
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend ftdi \
       --tx "123456789" --hflag --wait-halt --rx 2
# assembler source, Raspberry Pi, live monitor for 5 s
python -m pesm.run_pipeline firmware/uart_rx.pasm --backend spidev --spi 0.0 \
       --mode-gpio gpiochip0:17 --monitor 5
# no hardware: emulator
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend emulator \
       --tx "123456789" --hflag --wait-halt --rx 2
# compile only, write every artifact
python -m pesm.run_pipeline sw/examples/usb_ls_token.py --no-flash --save build/usb_ls_token
```

| Group | Options |
|---|---|
| Data | `--tx TEXT`, `--tx-hex "55 aa"`, `--rx N` |
| Run control | `--hflag`, `--wait-halt`, `--run-time S`, `--timeout S` |
| Monitor | `--monitor S`, `--interval S` |
| Build and load | `--save BASE`, `--no-flash`, `--no-verify`, `--reset`, `--listing` |
| Transport | `--backend ftdi\|spidev\|emulator`, `--url`, `--freq`, `--mode-pin`, `--reset-pin`, `--spi`, `--mode-gpio`, `--reset-gpio` |

Python:

```python
from pesm.builder import PESMProgram
from pesm.run_pipeline import run_pipeline
from pesm.programmer import EmulatorTransport

prog = PESMProgram("echo").label("l").pull().shift_out("x", 8).mov("isr", "x").push().jmp("l")
res = run_pipeline(prog, EmulatorTransport(), tx=b"abc", rx=3)
print(res.status, bytes(res.rx))

seen = []
res = run_pipeline(prog, EmulatorTransport(), tx=b"abc", monitor=0.001, interval=0.0001,
                   on_status=seen.append, on_rx=print)
```

`run_pipeline(source, transport, tx=b"", rx=None, hflag=False,
wait_halt=False, run_time=0.0, timeout=1.0, verify=True, monitor=None,
interval=0.01, on_status=None, on_rx=None, save=None, reset=False, log=None)`

* returns `PipelineResult(image, status, rx, history, files)`;
* `source` is a `PESMProgram`, an `Image`, or a path to `.py`,
  `.pasm`/`.asm`, `.bin` or `.json`;
* `transport=None` compiles (and saves) without touching hardware.

---

## 7. Troubleshooting

| Symptom | Check |
|---|---|
| `unexpected chip ID` | wiring, clock running, SCK ≤ f_clk/8, chip is a PESM v3 |
| `VerifyError` | SCK too fast (≤ f_clk/8), MISO wiring, MODE not high during load |
| `not a PESM v3 .bin` | image built by the v2 toolchain: rebuild from source |
| `status().err` set | illegal opcode, or imem/cfg written while running (MODE was low) |
| Core stuck, `pc` constant | a `wait_pin`/`pull`/`push`/`wait_pattern` condition never met; read `status()` or use `--monitor` |
| TX FIFO never drains | program never pulls, or halted |
| Edges jitter | an edge instruction is missing `delay=1`, or the code between grid points is longer than one tick |
| A pin does not follow `set_pin` | the background clock owns it (`background_clock(pin=…)`) |
| Program runs into `halt` at word 32+ | v2 program that relied on the implicit wrap at word 31: add `wrap()` |

---

## 8. How the toolchain is checked

`sw/tests` (pytest, no simulator):

* All 65 536 instruction words round-trip through the disassembler and
  assembler for every side-set count.
* Every `sw/examples/*.py` DSL program compiles to exactly the words and
  config of the matching `firmware/*.pasm`, and those programs are tested on
  the RTL and gate-level netlist.
* The programmer, emulator, monitor and pipeline are tested end to end,
  including verify-retry and FIFO flow control.
* Every Python block in this guide that does not need an FT232H is executed
  by `sw/tests/test_docs.py`, and every program in it is compiled.

`test/test_toolchain.py` (cocotb):

* `programmer.py` and `run_pipeline` drive the real design's SPI port
  through a cocotb transport (the same code path as an FT232H).
* The UART, SPI, I2C-read and USB-token macros are checked against device
  models.
