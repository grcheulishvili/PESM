; CRC-16/USB over the bytes the host streams into the TX FIFO.
; Host sets HFLAG when done; the engine pushes CRC low byte, high byte
; into the RX FIFO, clears HFLAG and halts.
;   poly 0x8005 reflected = 0xA001, init 0xFFFF, xorout 0xFFFF
.shift out=right in=right
.crc_poly 0xA001

    ldi   x, 0xff
    ldi   y, 0xff
loop:
    jpin  txne, 1, have
    jpin  hflag, 1, done
    jmp   loop
have:
    pull
bit:
    out   null, 1            ; lastbit = next data bit (LSB first)
    crc   lsb
    jmp   !osre, bit
    jmp   loop
done:
    xor   x, 0xff
    xor   y, 0xff
    mov   isr, x
    push
    mov   isr, y
    push
    hclr
    halt
