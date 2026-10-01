; UART 8N1 transmitter.  TX = TOUT0 (uo_out[1]).
; Host writes bytes with WRITE_TX; one frame per byte.
; Every bit edge is executed on the tick grid -> zero jitter.
.tick_hz 115200              ; 1 tick per bit (50 MHz: 434 + 7/256)
.shift out=right             ; LSB first
.init_tout 0x01              ; line idles high while in BOOT
.define TX tout0

idle:
    set   TX, 1
    pull                     ; block until a byte is queued (clk rate)
    set   TX, 0 [1]          ; start bit, aligned to next tick
    ldi   x, 7
bit:
    out   pins, TX, 1 [1]    ; data bit on the grid
    jmp   x--, bit
    set   TX, 1 [1]          ; stop bit
    jmp   idle
