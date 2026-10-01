/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_clkdiv: 16.8 fixed-point tick generator.
 *
 *   Average tick period = DIV_INT + DIV_FRAC/256 clk cycles (DIV_INT >= 1).
 *   Each individual period is DIV_INT or DIV_INT+1 cycles (first-order
 *   sigma-delta on the fraction), so worst-case grid error is < 1 clk.
 *   DIV_INT == 0 means "tick every cycle" (fraction ignored).
 *
 *   tick is a 1-cycle pulse, combinational from the counter flops.
 *   The core never stalls on tick for ordinary instructions; tick only
 *   gates pre-delays, DLY in tick units, and grid alignment.
 *
 *   sync restarts the phase: next tick occurs exactly DIV_INT cycles
 *   (half=0) or max(1, DIV_INT/2) cycles (half=1) after the sync cycle,
 *   and clears the fractional accumulator.
 *
 *   IW/FW are parameters only so that formal can prove a width-reduced
 *   instance; the chip uses 16/8.
 */

`default_nettype none

module pesm_clkdiv #(
    parameter integer IW = 16,
    parameter integer FW = 8
) (
    input  wire          clk,
    input  wire          rst_n,
    input  wire          en,      // 0 = hold phase reset (first enabled cycle ticks)
    input  wire          sync,
    input  wire          half,
    input  wire [IW-1:0] div_int,
    input  wire [FW-1:0] div_frac,
    output wire          tick
);

    localparam [IW-1:0] I_ZERO = {IW{1'b0}};
    localparam [IW-1:0] I_ONE  = {{(IW-1){1'b0}}, 1'b1};
    localparam [FW-1:0] F_ZERO = {FW{1'b0}};

    reg [IW-1:0] cnt;
    reg [FW-1:0] acc;
    reg [IW-1:0] cnt_d;     // next counter value
    reg [FW-1:0] acc_d;
    reg          zero;      // registered (cnt == 0): keeps the zero-detect tree off the tick path

    wire [FW:0]   acc_sum  = {1'b0, acc} + {1'b0, div_frac};
    wire          int_zero = (div_int == I_ZERO);
    wire [IW-1:0] int_m1   = int_zero ? I_ZERO : (div_int - I_ONE);
    wire [IW-1:0] half_int = {1'b0, div_int[IW-1:1]};
    wire [IW-1:0] half_m1  = (half_int == I_ZERO) ? I_ZERO : (half_int - I_ONE);

    assign tick = en & zero;

    always @(*) begin
        if (!en) begin
            cnt_d = I_ZERO;
            acc_d = F_ZERO;
        end else if (sync) begin
            cnt_d = half ? half_m1 : int_m1;
            acc_d = F_ZERO;
        end else if (zero) begin
            if (int_zero) begin
                cnt_d = I_ZERO;
                acc_d = F_ZERO;
            end else begin
                cnt_d = int_m1 + {{(IW-1){1'b0}}, acc_sum[FW]};
                acc_d = acc_sum[FW-1:0];
            end
        end else begin
            cnt_d = cnt - I_ONE;
            acc_d = acc;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            cnt  <= I_ZERO;
            acc  <= F_ZERO;
            zero <= 1'b1;
        end else begin
            cnt  <= cnt_d;
            acc  <= acc_d;
            zero <= (cnt_d == I_ZERO);
        end
    end

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume (!rst_n);

    // Configuration is only writable while the core is in BOOT (en == 0).
    always @(posedge clk) begin
        if (f_past_valid && en && $past(en)) begin
            assume ($stable(div_int));
            assume ($stable(div_frac));
        end
    end

    // the registered zero flag always equals the zero-detect of the counter
    always @(*) if (rst_n) assert (zero == (cnt == I_ZERO));

    // Counter bound
    always @(*) begin
        if (rst_n && en) begin
            if (int_zero) assert (cnt == I_ZERO);
            else          assert (cnt <= div_int);
        end
        if (rst_n && !en) assert (!tick);
    end

    // DIV_INT == 0 => tick on every enabled cycle
    always @(*) if (rst_n && en && int_zero) assert (tick);

    // Interval tracking between consecutive ticks (no sync in between).
    reg [IW:0] f_since;
    reg        f_seen;
    reg        f_carry;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            f_since <= {(IW+1){1'b0}};
            f_seen  <= 1'b0;
            f_carry <= 1'b0;
        end else if (!en || sync) begin
            f_since <= {(IW+1){1'b0}};
            f_seen  <= 1'b0;
            f_carry <= 1'b0;
        end else if (tick) begin
            f_since <= {{IW{1'b0}}, 1'b1};
            f_seen  <= 1'b1;
            f_carry <= acc_sum[FW] & ~int_zero;
        end else begin
            f_since <= f_since + 1'b1;
        end
    end
    always @(*) begin
        if (rst_n && en && f_seen && !int_zero) begin
            // inductive strengthening: elapsed + remaining == length of this interval
            assert ({1'b0, cnt} + f_since == {1'b0, div_int} + {{IW{1'b0}}, f_carry});
            if (div_frac == F_ZERO) assert (!f_carry);
            if (tick) begin
                assert (f_since >= {1'b0, div_int});
                assert (f_since <= {1'b0, div_int} + 1'b1);
                if (div_frac == F_ZERO) assert (f_since == {1'b0, div_int});
            end
        end
    end

    // After SYNC the first tick comes exactly DIV_INT (or max(1, DIV_INT/2)) cycles later.
    reg [IW:0] f_since_sync;
    reg        f_sync_armed;
    reg        f_sync_half;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            f_sync_armed <= 1'b0;
            f_since_sync <= {(IW+1){1'b0}};
            f_sync_half  <= 1'b0;
        end else if (!en) begin
            f_sync_armed <= 1'b0;
        end else if (sync) begin
            f_sync_armed <= 1'b1;
            f_sync_half  <= half;
            f_since_sync <= {{IW{1'b0}}, 1'b1};
        end else if (tick) begin
            f_sync_armed <= 1'b0;
        end else begin
            f_since_sync <= f_since_sync + 1'b1;
        end
    end
    always @(*) begin
        if (rst_n && en && f_sync_armed && !int_zero) begin
            if (f_sync_half)
                assert ({1'b0, cnt} + f_since_sync ==
                        ((half_int == I_ZERO) ? {{IW{1'b0}}, 1'b1} : {1'b0, half_int}));
            else
                assert ({1'b0, cnt} + f_since_sync == {1'b0, div_int});
        end
    end

    always @(*) cover (rst_n && en && f_seen && tick && f_carry);
`endif

endmodule

`default_nettype wire
