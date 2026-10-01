"""CRC-5/USB of a token's 11-bit ADDR/ENDP field (== firmware/crc5_usb.pasm)."""
from pesm.builder import PESMProgram

p = PESMProgram("crc5_usb").shift(out="right", in_="right").crc_poly(0x0014)
p.ldi("x", 0x1F).ldi("y", 0x00).pull()
p.macro_crc_osr()                       # 8 bits of byte 0
p.pull()
for _ in range(3):                      # 3 bits of byte 1
    p.shift_out("null", 1).crc_step()
p.xor("x", 0x1F).mov("isr", "x").push().halt()
program = p
