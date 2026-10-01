"""CRC-16/USB over TX FIFO bytes; host sets HFLAG when done; result (lo, hi)
pushed to RX FIFO (== firmware/crc16_usb.pasm)."""
from pesm.builder import PESMProgram

p = PESMProgram("crc16_usb").shift(out="right", in_="right").crc_poly(0xA001)
p.ldi("x", 0xFF).ldi("y", 0xFF)
p.label("loop").jmp_if_pin("txne", 1, "have").jmp_if_pin("hflag", 1, "done").jmp("loop")
p.label("have").pull().macro_crc_osr(msb=False).jmp("loop")
p.label("done").xor("x", 0xFF).xor("y", 0xFF)
p.mov("isr", "x").push().mov("isr", "y").push().hclr().halt()
program = p
