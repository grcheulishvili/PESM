; USB low-speed token packets with constant fields.
; Generated from sw/examples/usb_ls_token.py (PESMProgram.macro_usb_ls_token):
; CRC5, bit stuffing and NRZI are resolved at compile time, one grid-aligned
; D+/D- write per line transition. tick = 1.5 MHz, D+ = BIO0, D- = BIO1.
;   x = J (D- high), y = K (D+ high), null = SE0
; On the host flag: SETUP addr 0x15 endp 0xE, 4 idle bits, IN addr 0x7F endp 0xF, halt.
.div_raw 33 85
.shift out=right in=right
.pull_thresh 8
.push_thresh 8
.cfg 6 0x00
.cfg 7 63
.cfg 8 0
.cfg 9 0
.od 0x00
.crc_poly 0x0000
.init_out 0x02
.init_oe 0x03
.init_tout 0x00
.pattern 0x00 0x00
.cfg 18 0x00
.cfg 19 0x00
    wait high hflag
    ldi x, 0x02
    ldi y, 0x01
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [3]
    mov pins, y [3]
    mov pins, x [2]
    mov pins, y [1]
    mov pins, x [2]
    mov pins, y [2]
    mov pins, x [2]
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [5]
    mov pins, null [4]
    mov pins, x [2]
    nop [4]
    ldi x, 0x02
    ldi y, 0x01
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [3]
    mov pins, y [1]
    mov pins, x [2]
    mov pins, y [3]
    mov pins, x [7]
    mov pins, y [6]
    mov pins, x [1]
    mov pins, y [1]
    mov pins, x [2]
    mov pins, null [1]
    mov pins, x [2]
    nop [4]
    halt
