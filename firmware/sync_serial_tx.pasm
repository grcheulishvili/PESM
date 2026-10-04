; Synchronous serial transmitter with a free-running bit clock.
; The background clock generator drives TOUT1 (1 MHz, idle low) and costs no
; instructions. TX FIFO bytes are shifted out MSB first on TOUT2; every bit
; changes exactly 2 clk after a falling clock edge, also the first one after
; an idle period. FIFO empty: the data line holds its last level.
.tick_hz 2000000
.shift out=left
.pull_thresh 8
.bgclk pin=tout1 hz=1000000 auto idle=0

.wrap_target
    pull ifempty            ; next byte when the OSR is used up (blocks while idle)
    wait fall bgclk
    out pins, tout2, 1
.wrap
