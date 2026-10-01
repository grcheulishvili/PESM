# PESM Python Toolchain and DSL Guide

The `sw/pesm` package covers the whole user workflow:

| Module | Purpose |
|---|---|
| `pesm.isa` | encoders, exact disassembler, `Config` (16 registers), `Image` (32 words + 16 config bytes) and output formats |
| `pesm.builder` | **Python DSL**: `PESMProgram` with chainable instructions and protocol macros |
| `pesm.assembler` | text assembler (`.pasm`), same encoders as the DSL |
| `pesm.hostproto` | SPI host-port frames and `Status` decoding |
| `pesm.programmer` | host programmer: FT232H (pyftdi), Linux spidev, or emulator backends |
| `pesm.emulator` | pure-Python chip (host port + cycle-accurate core model) for dry runs |
| `pesm.run_pipeline` | one call or command: source → image → flash → verify → run → TX/RX |

```
pip install -e sw            # or: export PYTHONPATH=$PWD/sw
pip install pyftdi           # FT232H backend
pip install spidev gpiod     # Raspberry Pi / Linux backend
```

How it is checked:

* `sw/tests` (pytest, no simulator): all 65 536 instruction words round-trip
  through the disassembler and assembler for every side-set count. Every
  `sw/examples/*.py` DSL program compiles to exactly the words and config of
  the matching `firmware/*.pasm`, and those programs are tested on the RTL
  and gate-level netlist. The programmer, emulator and pipeline are tested
  end to end, including verify-retry and FIFO flow control.
* `test/test_toolchain.py` (cocotb): `programmer.py` and `run_pipeline` drive
  the real design's SPI port through a cocotb transport (the same code path
  as an FT232H). The UART, SPI and I2C-read macros are checked against
  device models.

---

## 1. Execution model in five rules

1. **One instruction per clock** (50 MHz). Setup work (loads, ALU, branches)
   is effectively free between bit edges.
2. **Tick grid.** `clock(tick_hz=…)` sets a fractional divider. An
   instruction with `delay=d` executes on the *d-th tick* from the cycle it
   is reached (`d=1`: next grid point). Give every bit edge `delay=1` and the
   edges land exactly on the grid, whatever runs in between, as long as that
   takes fewer cycles than one tick.
3. **Side-set** (`side=`) changes up to 4 pins in the same cycle as the
   instruction. Configure it with `sideset(count, base)`.
4. **Inputs are synchronized**: a pad change is seen 2–3 clocks later.
5. **Stalls never half-execute.** `pull`, `push`, `wait_pin`, autopull `OUT`
   and autopush `IN` either complete or change nothing except side-set pins.

Pins: output `bio0..bio7` (bidirectional `uio`, per-pin direction, optional
open drain), `tout0..tout6` (`uo_out[1..7]`). Input `bio0..bio7`,
`tin0..tin3` (`ui_in[4..7]`), and the status pins `txne` (TX FIFO not empty),
`rxnf` (RX FIFO not full) and `hflag` (host flag).

---

## 2. `PESMProgram` API reference

All methods return the program, so calls chain. Class-A instructions accept
`side=` and `delay=`. Branch targets are label strings (forward references
allowed) or absolute addresses.

### Configuration

| Method | Effect |
|---|---|
| `clock(tick_hz=…, divider=…, div_int=…, div_frac=…)` | tick grid (`divider` in clk cycles, fractional) |
| `shift(out='right'\|'left', in_='right'\|'left')` | right = LSB first, left = MSB first |
| `autopull(threshold=8, enable=True)` / `autopush(...)` | automatic OSR refill / ISR push |
| `thresholds(pull=None, push=None)` | thresholds without enabling auto mode |
| `sideset(count, base=0, pindir=False)` | 0–4 side pins from `base`; `pindir` drives OE instead of OUT |
| `open_drain(*pins)` | BIO pins become open drain (1 = release, 0 = pull low) |
| `crc_poly(poly)` | 16-bit polynomial for `crc_step` |
| `init_pins(bio_out=, bio_oe=, tout=)` | pin state in BOOT and at start |
| `wrap_target()` / `wrap()` | PIO-style zero-cost loop: fall-through from the `wrap()` instruction goes to `wrap_target()` |
| `entry(label)` | start address |
| `label(name)`, `new_label(hint)` | define / create a unique label |

Macros apply the configuration they need (for example, SPI sets MSB-first
shifting). A conflicting setting raises `DslError` at the call that causes
the conflict.

### Instructions

| Method | ISA | Notes |
|---|---|---|
| `nop()` | `nop` | use `nop(delay=n)` to wait for a grid point |
| `halt()`, `irq()`, `hclr()` | CTL | `irq` sets a sticky host-visible flag; `hclr` clears `hflag` |
| `push(iffull=False, block=True)` | `push` | ISR byte → RX FIFO |
| `pull(ifempty=False, block=True)` | `pull` | TX FIFO → OSR |
| `sync_grid(half=False)` | `sync` | re-phase the tick grid now |
| `clear(isr=True, osr=False)` | `clr` | |
| `jmp(target, cond='always')` | `jmp` | `cond`: `!x !y x-- y-- x!=y !osre lb` |
| `jmp_if_zero(reg, t)` / `jmp_dec(reg, t)` | `jmp !r` / `jmp r--` | `jmp_dec` loops `reg+1` times |
| `jmp_if_ne(t)` | `jmp x!=y` | compare-and-branch |
| `jmp_if_osr_not_empty(t)` / `jmp_if_lastbit(t)` | `!osre` / `lb` | |
| `jmp_if_pin(pin, level, t)` | `jpin` | no side/delay |
| `jmp_reg(reg)` | `mov pc, reg` | computed jump / jump table |
| `wait_pin(pin, level, sync=None)` | `wait` | level `0/1/'low'/'high'/'rise'/'fall'`; `sync='full'\|'half'` re-phases the grid on release |
| `wait_cycles(n, use_xy=False)` | `dly` | exactly n clk; > 1024 needs `use_xy` (clobbers X, Y) |
| `wait_ticks(n, use_xy=False)` | `dlyt` | until the n-th tick |
| `set_pin(pin, v)`, `toggle_pin(pin)` | `set` / `toggle` | |
| `dir_pin(pin, output=True)`, `release_pin(pin)` | `dir` | BIO only |
| `drive_pin(pin, v)` | `drive` | OUT = v and OE = 1 in one cycle |
| `set_pins(src)`, `set_pindirs(src)`, `set_tout(src)` | `mov pins/pindirs/tout` | atomic multi-pin write |
| `sample_pins()` | `mov isr, pins` | atomic 8-pin sample |
| `shift_out(dst, n=1)` | `out` | `dst`: pin (n ≤ 8 consecutive pins) or `'x' 'y' 'null' 'pindirs'` (n ≤ 32) |
| `shift_in(src, n=1)`, `get_pin(pin)` | `in` | `src`: pin(s) or `'x' 'y' 'null' 'osr'` |
| `mov(dst, src, op='')` | `mov` | `op`: `'' '~' 'rev' 'par'` |
| `ldi/and_/or_/xor(reg, imm)` | ALU | 8-bit immediate |
| `inc(reg)`, `dec(reg)` | ALU | |
| `crc_step(msb=False)` | `crc` | `{Y,X}` Galois step, input bit = last shifted bit |
| `raw(word)` | `.word` | |

### Protocol macros

| Macro | Words | Tick | Applies | Clobbers |
|---|---|---|---|---|
| `macro_uart_tx_byte(pin, byte=None, stop_bits=1)` | 7 (+1/extra stop) | baud | OUT right | X, OSR |
| `macro_uart_tx(pin, label)` | 8 | baud | OUT right, line idle high | X, OSR |
| `macro_uart_rx(pin, label, on_framing_error='irq')` | 12 | baud | IN right, autopush 8 | X, ISR |
| `macro_spi_transfer(sclk, mosi, miso, bits=8, source='fifo', sink='fifo')` | 9 | 2·f_SCLK | MSB first, no auto pull/push | X, OSR, ISR |
| `macro_i2c_start(sda, scl)` | 2 | 4·f_SCL | open drain, released | – |
| `macro_i2c_stop(sda, scl)` | 4 | 4·f_SCL | waits out clock stretching | – |
| `macro_i2c_write_byte(sda, scl, nack_label=None, source='fifo')` | 11–12 | 4·f_SCL | MSB first | X, OSR |
| `macro_i2c_read_byte(sda, scl, ack=True, sink='fifo')` | 13 | 4·f_SCL | MSB first, no autopush | X, ISR |
| `macro_crc_osr(msb=False)` | 3 | – | – | X, Y (CRC), OSR |
| `macro_usb_ls_tx(dp, dm, label)` | 23 | 1.5 MHz | OUT right, J idle | X, Y, OSR |

Register conventions: the macros use **X** as their bit counter. `crc_step`
uses `{Y,X}` as the CRC register. **Y** is otherwise free. FIFO data moves
through OSR/ISR.

### Compiling and output

```python
img = p.compile()            # pesm.isa.Image
img.words, img.cfg           # 32 x 16-bit, 16 x 8-bit
img.listing()                # disassembly with labels and source
img.to_bin()                 # 80 bytes: imem big-endian + cfg (exact SPI payloads)
img.to_mem(); img.to_cfg_mem()   # $readmemh
img.to_py(); img.to_json()
p.to_asm()                   # equivalent .pasm source (assembles to the same image)
```

Text assembler outputs the same formats:

```
python -m pesm.assembler firmware/uart_tx.pasm -f bin  -o uart_tx.bin
python -m pesm.assembler firmware/uart_tx.pasm -f mem  -o uart_tx.mem   # + uart_tx_cfg.mem
python -m pesm.assembler firmware/uart_tx.pasm -f py
python -m pesm.assembler firmware/uart_tx.pasm -f json -o uart_tx.json
```

---

## 3. Examples

### UART

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

### SPI (mode 0)

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

### I2C (100 kHz, open drain, clock stretching)

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

### CRC (CRC-16/USB, CRC-5/USB, CAN CRC-15)

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

### Timing helpers

```python
p.wait_cycles(3)              # exactly 3 clk
p.wait_cycles(20000, use_xy=True)
p.wait_ticks(10)              # 10 grid periods
p.wait_pin("tin2", "rise", sync="half")   # align the grid to mid-bit after an edge
```

---

## 4. Host side

### Wiring an FT232H

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
pins, MODE on any GPIO (`--mode-gpio gpiochip0:17`).

### Programmer API

```python
from pesm.programmer import FtdiTransport, Programmer

with FtdiTransport("ftdi://ftdi:232h/1", freq=1e6) as t:
    p = Programmer(t, log=print)
    p.load(img)                # MODE=1, flush FIFOs, write cfg + imem, read back and compare
    p.patch(31, [0x0100])      # random-access fix-up while in BOOT
    p.run()                    # MODE=0: core starts at ENTRY
    p.write_tx(b"hello")       # flow-controlled: never overflows the 8-byte FIFO
    print(p.read_rx(5))        # waits for 5 bytes (timeout)
    print(p.status())          # pc, X, Y, FIFO levels, RUNNING/HALTED/IRQ/ERR/...
```

The load sequence is: MODE high (core held) → CONTROL (flush, clear flags)
→ `WRITE_CFG 0` + 16 bytes → `WRITE_IMEM 0` + 32 words → `READ_IMEM` /
`READ_CFG` readback and compare (retries, then `VerifyError`) → MODE low.
Every frame is one CS_N-low transaction. MISO returns STATUS0 during the
command byte.

### One-shot pipeline

```
# DSL file (defines `program` or build())
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend ftdi \
       --tx "123456789" --hflag --wait-halt --rx 2
# assembler source, Raspberry Pi
python -m pesm.run_pipeline firmware/uart_tx.pasm --backend spidev --spi 0.0 \
       --mode-gpio gpiochip0:17 --tx "hello\r\n"
# no hardware: emulator
python -m pesm.run_pipeline sw/examples/crc16_usb.py --backend emulator \
       --tx "123456789" --hflag --wait-halt --rx 2
```

```python
from pesm.run_pipeline import run_pipeline
from pesm.programmer import EmulatorTransport
res = run_pipeline(prog, EmulatorTransport(), tx=b"123456789", hflag=True,
                   wait_halt=True, rx=2)
print(res.status, [hex(b) for b in res.rx])
```

---

## 5. Tutorial: from Python to pins

1. **Write the protocol** (`blink_uart.py`):

   ```python
   from pesm.builder import PESMProgram

   program = PESMProgram("hello").clock(tick_hz=115200).init_pins(tout=0x01)
   for ch in b"Hi\r\n":
       program.macro_uart_tx_byte("tout0", ch)
   program.halt()
   ```

   That is 4 × 7 + 1 = 29 words. `program.listing()` prints the code.
   `compile()` raises an error if it exceeds 32 words or a delay does not
   fit the side-set configuration.

2. **Try it without hardware**:

   ```
   python -m pesm.run_pipeline blink_uart.py --backend emulator --wait-halt --listing
   ```

3. **Wire it up**: FT232H as in section 4, a USB-UART adapter RX on
   `uo_out[1]` (TOUT0), common ground.

4. **Flash and run**:

   ```
   python -m pesm.run_pipeline blink_uart.py --backend ftdi --url ftdi://ftdi:232h/1 --wait-halt
   ```

   The programmer verifies the readback before releasing MODE. Your
   terminal shows `Hi`.

5. **Iterate**: change the program and run again. To patch one word, call
   `Programmer.patch(addr, [word])` while in BOOT.

### Checklist when something does not work

| Symptom | Check |
|---|---|
| `VerifyError` | SCK too fast (≤ f_clk/8), MISO wiring, MODE not high during load |
| `status().err` set | imem/cfg written while running (MODE was low) |
| Core stuck, `pc` constant | a `wait_pin`/`pull`/`push` condition never met; read `status()` |
| TX FIFO never drains | program never pulls, or halted |
| Edges jitter | an edge instruction is missing `delay=1`, or the code between grid points is longer than one tick |
