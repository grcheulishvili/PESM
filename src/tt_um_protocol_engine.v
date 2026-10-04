/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Protocol Engine State Machine (PESM) v3 - Tiny Tapeout IHP CMOS5L top.
 *
 * Pin map
 *   ui_in[0]   HOST_SCK     host SPI clock (mode 0)
 *   ui_in[1]   HOST_MOSI    host SPI data in
 *   ui_in[2]   HOST_CS_N    host SPI chip select, active low
 *   ui_in[3]   MODE         1 = BOOT (core held, imem/cfg writable), 0 = RUN
 *   ui_in[7:4] TIN[3:0]     target inputs  -> core input pins 8..11
 *   uo_out[0]  HOST_MISO    host SPI data out
 *   uo_out[7:1] TOUT[6:0]   target outputs -> core output pins 8..14
 *   uio[7:0]   BIO[7:0]     target bidirectional -> core pins 0..7
 *                           per-pin output enable, optional open-drain
 *
 * One output pin (0..14) can be handed to the background clock generator
 * (cfg BGCLK_CTL); its level then replaces OUT[pin] at the pad.
 */

`default_nettype none

module tt_um_protocol_engine (
    input  wire [7:0] ui_in,
    output wire [7:0] uo_out,
    input  wire [7:0] uio_in,
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire       ena,
    input  wire       clk,
    input  wire       rst_n
);

    // ------------------------------------------------------------------
    // Reset: asynchronous assert, synchronous de-assert
    // ------------------------------------------------------------------
    reg [1:0] rst_pipe;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) rst_pipe <= 2'b00;
        else        rst_pipe <= {rst_pipe[0], 1'b1};
    end
    wire rstn_i = rst_pipe[1];

    // ------------------------------------------------------------------
    // Input synchronizers
    // ------------------------------------------------------------------
    wire [3:0] host_s;     // {mode, csn, mosi, sck}
    wire [3:0] tin_s;
    wire [7:0] bio_s;

    pesm_sync #(.W(4), .RESET_VAL(4'b1100)) u_sync_host (
        .clk(clk), .rst_n(rstn_i), .d(ui_in[3:0]), .q(host_s)
    );
    pesm_sync #(.W(4), .RESET_VAL(4'b0000)) u_sync_tin (
        .clk(clk), .rst_n(rstn_i), .d(ui_in[7:4]), .q(tin_s)
    );
    pesm_sync #(.W(8), .RESET_VAL(8'h00)) u_sync_bio (
        .clk(clk), .rst_n(rstn_i), .d(uio_in), .q(bio_s)
    );

    wire sck_s    = host_s[0];
    wire mosi_s   = host_s[1];
    wire csn_s    = host_s[2];
    wire boot_pre = host_s[3];       // synchronized MODE

    // The core (and the imem/cfg write gate) see MODE one cycle later, so the
    // host block knows one cycle ahead that BOOT is about to end.
    reg boot;
    always @(posedge clk or negedge rstn_i) begin
        if (!rstn_i) boot <= 1'b1;
        else         boot <= boot_pre;
    end

    // ------------------------------------------------------------------
    // Interconnect
    // ------------------------------------------------------------------
    wire [5:0]  pc;
    wire [5:0]  rd_pc;
    wire [15:0] rd_instr;

    wire [15:0] cfg_div_int;
    wire [7:0]  cfg_div_frac;
    wire        cfg_out_right, cfg_in_right, cfg_autopull, cfg_autopush;
    wire [4:0]  cfg_pull_thresh, cfg_push_thresh;
    wire [2:0]  cfg_side_count;
    wire        cfg_side_pindir;
    wire [3:0]  cfg_side_base;
    wire [5:0]  cfg_wrap_top, cfg_wrap_bot, cfg_entry;
    wire [7:0]  cfg_od_mask;
    wire [15:0] cfg_crc_poly;
    wire [7:0]  cfg_init_bio_out, cfg_init_bio_oe;
    wire [6:0]  cfg_init_tout;
    wire [7:0]  cfg_pat_mask, cfg_pat_val, cfg_bg_div;
    wire [3:0]  cfg_bg_pin;
    wire        cfg_bg_en, cfg_bg_auto, cfg_bg_idle;
    wire        bg_q;

    wire        tx_push, tx_pop, tx_empty, tx_full;
    wire [7:0]  tx_wdata, tx_head;
    wire [3:0]  tx_level;
    wire        rx_push, rx_pop, rx_empty, rx_full;
    wire [7:0]  rx_wdata, rx_head;
    wire [3:0]  rx_level;

    wire        flush_tx, flush_rx, hflag_set, hflag_clr_host, hflag_clr_core, clr_flags;
    wire        st_running, st_halted, st_irq, st_err, st_rx_ovf;
    wire [7:0]  st_x, st_y;
    wire [14:0] out_reg;
    wire [7:0]  oe_reg;
    wire        miso;

    // ------------------------------------------------------------------
    // Host flag (host -> core semaphore, input pin 14)
    // ------------------------------------------------------------------
    reg hflag;
    always @(posedge clk or negedge rstn_i) begin
        if (!rstn_i)                          hflag <= 1'b0;
        else if (hflag_set)                   hflag <= 1'b1;
        else if (hflag_clr_host | hflag_clr_core) hflag <= 1'b0;
    end

    // ------------------------------------------------------------------
    // Blocks
    // ------------------------------------------------------------------
    pesm_host u_host (
        .clk(clk), .rst_n(rstn_i),
        .sck(sck_s), .mosi(mosi_s), .csn(csn_s), .miso(miso),
        .boot(boot), .boot_pre(boot_pre),
        .core_pc(pc), .rd_pc(rd_pc), .rd_instr(rd_instr),
        .cfg_div_int(cfg_div_int), .cfg_div_frac(cfg_div_frac),
        .cfg_out_right(cfg_out_right), .cfg_in_right(cfg_in_right),
        .cfg_autopull(cfg_autopull), .cfg_autopush(cfg_autopush),
        .cfg_pull_thresh(cfg_pull_thresh), .cfg_push_thresh(cfg_push_thresh),
        .cfg_side_count(cfg_side_count), .cfg_side_pindir(cfg_side_pindir),
        .cfg_side_base(cfg_side_base),
        .cfg_wrap_top(cfg_wrap_top), .cfg_wrap_bot(cfg_wrap_bot), .cfg_entry(cfg_entry),
        .cfg_od_mask(cfg_od_mask), .cfg_crc_poly(cfg_crc_poly),
        .cfg_init_bio_out(cfg_init_bio_out), .cfg_init_bio_oe(cfg_init_bio_oe),
        .cfg_init_tout(cfg_init_tout),
        .cfg_pat_mask(cfg_pat_mask), .cfg_pat_val(cfg_pat_val),
        .cfg_bg_pin(cfg_bg_pin), .cfg_bg_en(cfg_bg_en), .cfg_bg_auto(cfg_bg_auto),
        .cfg_bg_idle(cfg_bg_idle), .cfg_bg_div(cfg_bg_div),
        .tx_push(tx_push), .tx_wdata(tx_wdata), .tx_full(tx_full), .tx_level(tx_level),
        .rx_pop(rx_pop), .rx_empty(rx_empty), .rx_head(rx_head), .rx_level(rx_level),
        .flush_tx(flush_tx), .flush_rx(flush_rx),
        .hflag_set(hflag_set), .hflag_clr(hflag_clr_host), .clr_flags(clr_flags),
        .st_running(st_running), .st_halted(st_halted), .st_irq(st_irq),
        .st_err(st_err), .st_rx_ovf(st_rx_ovf), .st_hflag(hflag),
        .st_x(st_x), .st_y(st_y)
    );

    pesm_fifo u_txf (
        .clk(clk), .rst_n(rstn_i), .flush(flush_tx),
        .push(tx_push), .wdata(tx_wdata),
        .pop(tx_pop), .rdata(tx_head),
        .empty(tx_empty), .full(tx_full), .level(tx_level)
    );

    pesm_fifo u_rxf (
        .clk(clk), .rst_n(rstn_i), .flush(flush_rx),
        .push(rx_push), .wdata(rx_wdata),
        .pop(rx_pop), .rdata(rx_head),
        .empty(rx_empty), .full(rx_full), .level(rx_level)
    );

    pesm_core u_core (
        .clk(clk), .rst_n(rstn_i), .boot(boot),
        .pc_o(pc), .rd_pc(rd_pc), .rd_instr(rd_instr),
        .bio_in(bio_s), .tin(tin_s),
        .cfg_div_int(cfg_div_int), .cfg_div_frac(cfg_div_frac),
        .cfg_out_right(cfg_out_right), .cfg_in_right(cfg_in_right),
        .cfg_autopull(cfg_autopull), .cfg_autopush(cfg_autopush),
        .cfg_pull_thresh(cfg_pull_thresh), .cfg_push_thresh(cfg_push_thresh),
        .cfg_side_count(cfg_side_count), .cfg_side_pindir(cfg_side_pindir),
        .cfg_side_base(cfg_side_base),
        .cfg_wrap_top(cfg_wrap_top), .cfg_wrap_bot(cfg_wrap_bot), .cfg_entry(cfg_entry),
        .cfg_crc_poly(cfg_crc_poly),
        .cfg_init_bio_out(cfg_init_bio_out), .cfg_init_bio_oe(cfg_init_bio_oe),
        .cfg_init_tout(cfg_init_tout),
        .cfg_pat_mask(cfg_pat_mask), .cfg_pat_val(cfg_pat_val),
        .cfg_bg_auto(cfg_bg_auto), .cfg_bg_idle(cfg_bg_idle), .cfg_bg_div(cfg_bg_div),
        .tx_empty(tx_empty), .tx_head(tx_head), .tx_pop(tx_pop),
        .rx_full(rx_full), .rx_level(rx_level), .rx_push(rx_push), .rx_wdata(rx_wdata),
        .hflag(hflag), .hflag_clr(hflag_clr_core), .clr_flags(clr_flags),
        .out_reg(out_reg), .oe_reg(oe_reg), .bg_q(bg_q),
        .running(st_running), .halted(st_halted), .irq(st_irq), .err(st_err),
        .rx_ovf(st_rx_ovf), .x(st_x), .y(st_y)
    );

    // ------------------------------------------------------------------
    // Pads
    //   open-drain BIO pin: never drives 1; drives 0 when out=0 and oe=1
    // ------------------------------------------------------------------
    reg [14:0] out_pad;
    always @(*) begin
        out_pad = out_reg;
        if (cfg_bg_en && cfg_bg_pin != 4'd15) out_pad[cfg_bg_pin] = bg_q;
    end

    assign uo_out  = {out_pad[14:8], miso};
    assign uio_out = out_pad[7:0] & ~cfg_od_mask;
    assign uio_oe  = oe_reg & ~(cfg_od_mask & out_pad[7:0]);

    wire _unused = &{ena, 1'b0};

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume (!rst_n);
    always @(*) if (f_past_valid) assume (rst_n);

    // FIFO handshakes across blocks
    always @(*) begin
        if (rstn_i) begin
            assert (!(tx_push && tx_full && !tx_pop));
            assert (!(rx_pop && rx_empty));
            assert (!(rx_push && rx_full));
            assert (!(tx_pop && tx_empty));
        end
    end

    // The write gate and the read-port hand-over in pesm_host rely on this
    always @(posedge clk) begin
        if (f_past_valid && rstn_i && $past(rstn_i)) assert (boot == $past(boot_pre));
    end

    // Open-drain pins never drive high
    always @(*) begin
        if (rstn_i) assert (((uio_out & uio_oe) & cfg_od_mask) == 8'h00);
    end

    // Background clock owns exactly its pin; every other pad follows OUT
    genvar gi;
    generate
        for (gi = 0; gi < 15; gi = gi + 1) begin : g_pad
            always @(*) begin
                if (rstn_i)
                    assert (out_pad[gi] == ((cfg_bg_en && cfg_bg_pin == gi) ? bg_q : out_reg[gi]));
            end
        end
    endgenerate
`endif

endmodule

`default_nettype wire
