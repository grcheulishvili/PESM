<!---
This file is used to generate the Tiny Tapeout datasheet.
-->

## How it works

PESM is a small, single-issue engine whose instruction set is built for
bit-banging: driving, sampling and waiting on pins with cycle-exact timing.
Protocols (UART, SPI, I2C, USB low-speed packets, CRC-checked framings, …)
are firmware loaded at run time. The chip contains no hard UART, SPI or I2C
block.

* 64 × 16-bit instruction memory, one instruction per clock (50 MHz)
* 16.8 fractional tick divider. Instructions with a pre-delay execute exactly
  on a tick, so bit edges have zero jitter relative to the grid. Setup
  instructions in between run at full clock speed.
* `WAIT … SYNCHALF` re-phases the grid on an input edge, so UART RX samples
  mid-bit
* 32-bit OSR/ISR with autopull/autopush, 8-byte TX and RX FIFOs
* up to 4 side-set pins per instruction; per-pin direction and open drain on
  all 8 bidirectional pins
* pattern branch: a masked compare of up to 8 input pins in one cycle
* background clock generator tied to the tick grid: a free-running clock on
  any output pin at no instruction cost, also usable as an internal timebase
* minimal ALU on X/Y: `ldi and or xor inc dec`, `x!=y` branch, parity,
  bit-reverse, and a 1-cycle 16-bit CRC step (USB CRC5/CRC16, CAN CRC15, …)

Full reference: `docs/ISA.md`. Python DSL, assembler and host programmer:
`docs/DSL_GUIDE.md`.

## How to test

1. Hold `MODE` (`ui[3]`) high (BOOT). Over the host SPI port (`ui[0]` SCK,
   `ui[1]` MOSI, `ui[2]` CS_N, `uo[0]` MISO, mode 0, SCK ≤ 6 MHz), write the
   config registers (`0x80` + 20 bytes) and the program (`0x00` + 64 × 2
   bytes). `python3 tools/pesm_asm.py firmware/uart_tx.pasm --spi` prints the
   frames. Read back with `0x40` (program) / `0xA0` (config). `0xD0` returns
   status; its sixth byte is the chip ID `0x30`.
2. Pull `MODE` low. The core starts at `ENTRY`.
3. Exchange data with `0xC0` (write TX FIFO) and `0xC8` (read RX FIFO). Read
   status with `0xD0`.

`python3 -m pesm.run_pipeline firmware/uart_tx.pasm --backend ftdi --tx "hello"`
does all of this through an FT232H.

Example: with `firmware/uart_tx.pasm` loaded, each byte written to the TX
FIFO comes out of `uo[1]` as 115200 8N1.

## External hardware

A microcontroller (for example the TT demo board RP2040) or a USB-SPI
adapter (FT232H) as host. Use pull-up resistors on any `uio` pin used
open-drain (I2C).
