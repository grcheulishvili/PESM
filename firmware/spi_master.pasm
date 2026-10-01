; SPI master, mode 0, MSB first, full duplex, 1 MHz SCLK @ 50 MHz clk.
;   SCLK = TOUT1 (uo_out[2], side-set), MOSI = TOUT2 (uo_out[3]),
;   CS_N = TOUT3 (uo_out[4]),            MISO = TIN1 (ui_in[5])
; CS_N stays low while the TX FIFO has data; each TX byte returns one RX byte.
.tick_hz 2000000             ; 2 ticks per SPI bit
.shift out=left in=left
.autopull 8
.autopush 8
.side 1 base=tout1
.init_tout 0x08              ; CS_N high, SCLK low
.define MOSI tout2
.define CS   tout3
.define MISO tin1

idle:
    set   CS, 1 side 0
    wait  high txne side 0
    set   CS, 0 side 0 [1]
bitloop:
    out   pins, MOSI, 1 side 0 [1]   ; SCLK falls, MOSI changes
    in    pins, MISO, 1 side 1 [1]   ; SCLK rises, sample MISO
    jmp   !osre, bitloop side 1
    jpin  txne, 1, bitloop           ; next byte back-to-back
    nop   side 0 [1]                 ; final SCLK low
    jmp   idle side 0
