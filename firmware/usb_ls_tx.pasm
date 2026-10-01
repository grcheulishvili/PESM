; USB low-speed (1.5 Mb/s) packet transmitter: NRZI, bit stuffing, EOP.
;   D+ = BIO0 (uio[0]), D- = BIO1 (uio[1]); low-speed idle J = D- high.
; Host queues one complete packet (SYNC 0x80, PID, payload, CRC) into the
; TX FIFO; the packet ends when the FIFO runs dry at a byte boundary,
; then SE0 SE0 J is sent and the line idles in J. Keep the FIFO fed for
; packets longer than 8 bytes (the host port is ~30x faster than the bus).
; Every line transition executes on the 1.5 MHz grid (33.33 clk @ 50 MHz).
.tick_hz 1500000
.shift out=right             ; USB is LSB first
.init_out 0x02               ; J
.init_oe  0x03

idle:
    wait  high txne
    ldi   x, 0x02            ; x = current line state (J)
    ldi   y, 5               ; ones remaining before a stuff bit
bit:
    jmp   !osre, have
    jpin  txne, 0, eop       ; FIFO empty at byte boundary -> end of packet
    pull
have:
    out   null, 1            ; LB = next data bit
    jmp   lb, one
    xor   x, 0x03            ; data 0: NRZI transition
    ldi   y, 5
    mov   pins, x [1]
    jmp   bit
one:
    mov   pins, x [1]        ; data 1: no transition
    jmp   y--, bit
    xor   x, 0x03            ; 6 ones sent: stuff a 0
    mov   pins, x [1]
    ldi   y, 5
    jmp   bit
eop:
    mov   pins, null [1]     ; SE0
    ldi   x, 0x02
    mov   pins, x [2]        ; J after two bit times
    nop   [1]                ; hold J one bit time
    jmp   idle
