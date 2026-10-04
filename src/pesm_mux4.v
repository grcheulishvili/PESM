/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_mux4: one 4:1 multiplexer, y = d[s].
 *
 * The instruction-memory read path is a tree of these. Written as plain
 * logic, synthesis turns a 64:1 mux into 64 decoded word lines and wide
 * AND-OR trees, which is slow and wire-hungry on a 3-metal routing stack.
 * When the IHP standard-cell library is the target (LibreLane defines
 * SCL_<library>), the library's 4:1 mux cell is instantiated directly so the
 * tree structure survives; every other flow (simulation, formal, other
 * PDKs) uses the behavioural form.
 */

`default_nettype none

module pesm_mux4 (
    input  wire [3:0] d,
    input  wire [1:0] s,
    output wire       y
);

`ifdef SCL_sg13cmos5l_stdcell
    sg13cmos5l_mux4_1 u_mux4 (
        .A0(d[0]), .A1(d[1]), .A2(d[2]), .A3(d[3]), .S0(s[0]), .S1(s[1]), .X(y)
    );
`elsif SCL_sg13g2_stdcell
    sg13g2_mux4_1 u_mux4 (
        .A0(d[0]), .A1(d[1]), .A2(d[2]), .A3(d[3]), .S0(s[0]), .S1(s[1]), .X(y)
    );
`else
    assign y = d[s];
`endif

endmodule

`default_nettype wire
