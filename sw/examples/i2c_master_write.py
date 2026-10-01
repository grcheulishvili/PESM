"""I2C master write, 100 kHz, clock stretching, NACK -> IRQ
(== firmware/i2c_master_write.pasm). SDA=BIO0, SCL=BIO1."""
from pesm.builder import PESMProgram

SDA, SCL = "bio0", "bio1"
p = PESMProgram("i2c_master_write").clock(tick_hz=400_000)
p.label("idle").wait_pin("txne", "high")
p.macro_i2c_start(SDA, SCL)
p.label("byte").macro_i2c_write_byte(SDA, SCL, nack_label="nack")
p.jmp_if_pin("txne", 1, "byte")
p.label("stop").macro_i2c_stop(SDA, SCL).jmp("idle")
p.label("nack").irq().set_pin(SCL, 0, delay=2).jmp("stop")
program = p
