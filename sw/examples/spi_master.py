"""SPI mode-0 master, 1 MHz, side-set SCLK (== firmware/spi_master.pasm).
SCLK=TOUT1 (side-set), MOSI=TOUT2, CS_N=TOUT3, MISO=TIN1."""
from pesm.builder import PESMProgram

MOSI, CS, MISO = "tout2", "tout3", "tin1"
p = PESMProgram("spi_master").clock(tick_hz=2_000_000)
p.shift(out="left", in_="left").autopull(8).autopush(8)
p.sideset(1, base="tout1").init_pins(tout=0x08)
(p.label("idle")
  .set_pin(CS, 1, side=0)
  .wait_pin("txne", "high", side=0)
  .set_pin(CS, 0, side=0, delay=1)
  .label("bitloop")
  .shift_out(MOSI, 1, side=0, delay=1)
  .shift_in(MISO, 1, side=1, delay=1)
  .jmp_if_osr_not_empty("bitloop", side=1)
  .jmp_if_pin("txne", 1, "bitloop")
  .nop(side=0, delay=1)
  .jmp("idle", side=0))
program = p
