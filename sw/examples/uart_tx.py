"""UART 8N1 transmitter on TOUT0 at 115200 baud (== firmware/uart_tx.pasm)."""
from pesm.builder import PESMProgram

program = PESMProgram("uart_tx").clock(tick_hz=115200).shift(out="right")
program.macro_uart_tx("tout0", label="idle")
