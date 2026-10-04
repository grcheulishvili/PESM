"""Synchronous serial transmitter with a free-running bit clock
(== firmware/sync_serial_tx.pasm).

The background clock generator drives the bit clock on TOUT1 (1 MHz, idle
low) without using any instruction. Data bits from the TX FIFO change on
TOUT2 exactly two clk after a falling clock edge, MSB first; the receiver
samples on the rising edge. An empty FIFO just holds the data line.
"""
from pesm.builder import PESMProgram

program = PESMProgram("sync_serial_tx").clock(tick_hz=2_000_000)
program.shift(out="left").thresholds(pull=8)
program.background_clock(pin="tout1", hz=1_000_000, auto=True, idle=0)
program.wrap_target()
program.pull(ifempty=True)
program.wait_pin("bgclk", "fall")
program.shift_out("tout2", 1)
program.wrap()
