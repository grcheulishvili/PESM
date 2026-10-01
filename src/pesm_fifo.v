/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_fifo: 8-entry x 8-bit synchronous FIFO, show-ahead read port.
 *  - push while full is accepted only if a pop happens in the same cycle
 *  - pop while empty is ignored
 *  - flush has priority over push/pop
 */

`default_nettype none

module pesm_fifo (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       flush,
    input  wire       push,
    input  wire [7:0] wdata,
    input  wire       pop,
    output wire [7:0] rdata,
    output wire       empty,
    output wire       full,
    output wire [3:0] level
);

    reg [7:0] mem [0:7];
    reg [2:0] wp;
    reg [2:0] rp;
    reg [3:0] cnt;

    wire do_pop  = pop  & (cnt != 4'd0);
    wire do_push = push & ((cnt != 4'd8) | do_pop);

    assign rdata = mem[rp];
    assign empty = (cnt == 4'd0);
    assign full  = (cnt == 4'd8);
    assign level = cnt;

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            wp  <= 3'd0;
            rp  <= 3'd0;
            cnt <= 4'd0;
            for (i = 0; i < 8; i = i + 1) mem[i] <= 8'h00;
        end else if (flush) begin
            wp  <= 3'd0;
            rp  <= 3'd0;
            cnt <= 4'd0;
        end else begin
            if (do_push) begin
                mem[wp] <= wdata;
                wp      <= wp + 3'd1;
            end
            if (do_pop) rp <= rp + 3'd1;
            case ({do_push, do_pop})
                2'b10:   cnt <= cnt + 4'd1;
                2'b01:   cnt <= cnt - 4'd1;
                default: cnt <= cnt;
            endcase
        end
    end

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume (!rst_n);

    // Structural invariants (inductive)
    always @(*) begin
        if (rst_n) begin
            assert (cnt <= 4'd8);
            assert (wp == rp + cnt[2:0]);
            assert ((cnt == 4'd8) || (cnt[3] == 1'b0));
        end
    end

    // Level tracks push/pop exactly
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(flush)) begin
            if ($past(do_push) && !$past(do_pop)) assert (cnt == $past(cnt) + 4'd1);
            if (!$past(do_push) && $past(do_pop)) assert (cnt == $past(cnt) - 4'd1);
            if ($past(do_push) == $past(do_pop)) assert (cnt == $past(cnt));
            if ($past(full) && !$past(pop)) assert (cnt == 4'd8);
            if ($past(empty)) assert (!$past(do_pop));
        end
        if (f_past_valid && rst_n && $past(rst_n) && $past(flush)) assert (cnt == 4'd0);
    end

    // Data integrity: track one arbitrary write and prove it is read back in order.
    (* anyconst *) reg [7:0] f_data;
    (* anyseq *)   reg       f_pick;
    reg        f_armed;
    reg [3:0]  f_ahead;   // entries ahead of the tracked one
    initial f_armed = 1'b0;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            f_armed <= 1'b0;
            f_ahead <= 4'd0;
        end else if (flush) begin
            f_armed <= 1'b0;
        end else if (!f_armed) begin
            if (do_push && wdata == f_data && f_pick) begin
                f_armed <= 1'b1;
                f_ahead <= cnt - {3'd0, do_pop};
            end
        end else if (do_pop) begin
            if (f_ahead == 4'd0) f_armed <= 1'b0;
            else                 f_ahead <= f_ahead - 4'd1;
        end
    end
    always @(*) begin
        if (rst_n && f_armed) begin
            assert (f_ahead < cnt);
            if (f_ahead == 4'd0) assert (rdata == f_data);
        end
    end

    always @(*) cover (rst_n && full);
`endif

endmodule

`default_nettype wire
