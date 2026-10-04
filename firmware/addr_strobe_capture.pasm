; Address-qualified parallel capture using the pattern branch (JPAT).
; Bus: data BIO0..3, address BIO4..7, strobe TIN0.
; When address == 0xA and strobe == 1, sample BIO0..7 into the RX FIFO.
;
; JPAT window = 8 inputs starting at BIO4: bit0..3 = BIO4..7, bit4 = TIN0.
.shift in=left
.pattern 0x1f 0x1a          ; mask: address + strobe; value: 0b1_1010

idle:
    jnpat bio4, idle        ; wait for address + strobe (5 pins, one cycle)
    mov isr, pins           ; atomic sample of BIO0..7
    push
hold:
    jpat bio4, hold         ; wait until the pattern is gone
    jmp idle
