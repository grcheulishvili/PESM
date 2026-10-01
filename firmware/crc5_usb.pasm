; CRC-5/USB of a token's 11-bit ADDR/ENDP field.
; Host writes 2 bytes: {ENDP[0],ADDR[6:0]}, {5'b0, ENDP[3:1]}.
; Result (5 bits, ready to place in byte 2 bits 7:3) is pushed to RX FIFO.
;   poly x^5+x^2+1 reflected = 0x14, init 0x1F, xorout 0x1F
.shift out=right in=right
.crc_poly 0x0014

    ldi   x, 0x1f
    ldi   y, 0x00
    pull
b8:
    out   null, 1
    crc   lsb
    jmp   !osre, b8          ; 8 bits of the first byte
    pull
    out   null, 1            ; 3 bits of the second byte
    crc   lsb
    out   null, 1
    crc   lsb
    out   null, 1
    crc   lsb
    xor   x, 0x1f
    mov   isr, x
    push
    halt
