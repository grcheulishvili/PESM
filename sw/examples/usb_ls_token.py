"""USB low-speed token packets with constant fields (== firmware/usb_ls_token.pasm).

On the host flag: SETUP to address 0x15 endpoint 0xE, four idle bit times,
IN to address 0x7F endpoint 0xF, then halt. CRC5, bit stuffing and NRZI are
resolved at compile time; every line transition is one grid-aligned
D+/D- write. Needs the 64-word instruction memory (45 words).
"""
from pesm.builder import PESMProgram

program = PESMProgram("usb_ls_token").clock(tick_hz=1_500_000)
program.wait_pin("hflag", "high")
program.macro_usb_ls_token("setup", addr=0x15, endp=0xE, dp="bio0", dm="bio1", idle_ticks=4)
program.macro_usb_ls_token("in", addr=0x7F, endp=0xF, dp="bio0", dm="bio1", idle_ticks=4)
program.halt()
