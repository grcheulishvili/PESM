; I2C master, write-only transactions, 100 kHz, open-drain, clock stretching.
;   SDA = BIO0 (uio[0]), SCL = BIO1 (uio[1]); external pull-ups required.
; Host queues: address byte (addr<<1 | 0), then data bytes. When the TX
; FIFO runs dry the engine issues STOP. NACK -> IRQ flag + STOP.
.tick_hz 400000              ; 4 ticks per SCL period
.shift out=left              ; MSB first
.od 0x03                     ; BIO0/1 open drain
.init_oe 0x03
.init_out 0x03               ; both released (high)
.define SDA bio0
.define SCL bio1

idle:
    wait  high txne
    set   SDA, 0 [1]         ; START: SDA falls while SCL high
    set   SCL, 0 [1]
byte:
    pull
    ldi   x, 7
bit:
    out   pins, SDA, 1 [1]   ; data change while SCL low
    set   SCL, 1 [1]
    wait  high SCL           ; clock stretching
    set   SCL, 0 [2]
    jmp   x--, bit
    set   SDA, 1 [1]         ; release SDA for ACK
    set   SCL, 1 [1]
    wait  high SCL
    jpin  SDA, 1, nack
    set   SCL, 0 [2]
    jpin  txne, 1, byte
stop:
    set   SDA, 0 [1]
    set   SCL, 1 [1]
    wait  high SCL           ; slave may still stretch after the last ACK
    set   SDA, 1 [1]         ; STOP: SDA rises while SCL high
    jmp   idle
nack:
    irq
    set   SCL, 0 [2]
    jmp   stop
