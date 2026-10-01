"""USB low-speed packet transmitter, D+=BIO0, D-=BIO1 (== firmware/usb_ls_tx.pasm)."""
from pesm.builder import PESMProgram

program = PESMProgram("usb_ls_tx").clock(tick_hz=1_500_000)
program.macro_usb_ls_tx("bio0", "bio1", label="idle")
