; UART 8N1 receiver.  RX = TIN0 (ui_in[4]).
; Falling edge re-phases the tick grid to mid-bit, autopush fills RX FIFO.
; Framing error -> IRQ flag, wait for idle line.
.tick_hz 115200
.shift in=right              ; LSB first, byte lands in ISR[31:24]
.autopush 8
.define RX tin0

start:
    wait  fall RX synchalf   ; start edge; next tick = middle of start bit
    nop   [1]
    jpin  RX, 1, start       ; glitch, not a start bit
    ldi   x, 7
bit:
    in    pins, RX, 1 [1]    ; sample at mid-bit
    jmp   x--, bit
    nop   [1]                ; middle of stop bit
    jpin  RX, 0, ferr
    jmp   start
ferr:
    irq
    wait  high RX
    jmp   start
