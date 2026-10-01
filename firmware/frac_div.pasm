; Divider characterisation: TOUT0 toggles on every tick.
.div_raw 4 128               ; 4.5 clk per tick
loop:
    toggle tout0 [1]
    jmp   loop
