"""UART 8N1 receiver on TIN0 at 115200 baud (== firmware/uart_rx.pasm)."""
from pesm.builder import PESMProgram

program = PESMProgram("uart_rx").clock(tick_hz=115200)
program.macro_uart_rx("tin0", label="start")
