"""Address-qualified parallel capture with the pattern branch
(== firmware/addr_strobe_capture.pasm).

Bus: data on BIO0..3, address on BIO4..7, strobe on TIN0. When the address
is 0xA and the strobe is high (one JPAT, five pins compared in one cycle),
all eight BIO pins are sampled into the RX FIFO; then wait until the pattern
is gone.
"""
from pesm.builder import PESMProgram

program = PESMProgram("addr_strobe_capture").shift(in_="left")
# window = inputs 4..11: BIO4..7 (address), TIN0 (strobe), TIN1..3 (ignored)
program.pattern_pins({"bio4": 0, "bio5": 1, "bio6": 0, "bio7": 1, "tin0": 1}, base="bio4")
program.label("idle").jmp_if_pattern("idle", base="bio4", match=False)   # wait for a match
program.sample_pins().push()
program.wait_pattern(base="bio4", match=False)                            # wait for release
program.jmp("idle")
