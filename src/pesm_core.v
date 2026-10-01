/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_core: single-issue, single-cycle protocol engine.
 *
 *  - One instruction per clk. No fetch state: imem is a flop array read
 *    combinationally at pc.
 *  - Bit timing comes from the tick grid (pesm_clkdiv). Only the 4-bit
 *    pre-delay field, DLY in tick units and WAIT ... SYNC interact with
 *    the grid; everything else runs at clk rate.
 *  - Pre-delay d>0: the instruction executes on the d-th tick pulse
 *    counted from (and including) the cycle it reaches issue. Every
 *    grid-aligned instruction therefore executes exactly on a tick cycle:
 *    zero jitter relative to the grid.
 *  - Side-set is applied on every cycle the instruction executes
 *    (including stall cycles of WAIT/PULL/PUSH/OUT/IN), main op wins on
 *    pin conflicts.
 *
 * See docs/ISA.md for the encoding.
 */

`default_nettype none

module pesm_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        boot,

    output wire [4:0]  pc_o,         // architectural pc (status)
    output wire [4:0]  fetch_pc,     // imem read address = next pc
    input  wire [15:0] fetch_instr,  // imem[fetch_pc]

    input  wire [7:0]  bio_in,
    input  wire [3:0]  tin,

    input  wire [15:0] cfg_div_int,
    input  wire [7:0]  cfg_div_frac,
    input  wire        cfg_out_right,
    input  wire        cfg_in_right,
    input  wire        cfg_autopull,
    input  wire        cfg_autopush,
    input  wire [4:0]  cfg_pull_thresh,
    input  wire [4:0]  cfg_push_thresh,
    input  wire [2:0]  cfg_side_count,
    input  wire        cfg_side_pindir,
    input  wire [3:0]  cfg_side_base,
    input  wire [4:0]  cfg_wrap_top,
    input  wire [4:0]  cfg_wrap_bot,
    input  wire [4:0]  cfg_entry,
    input  wire [15:0] cfg_crc_poly,
    input  wire [7:0]  cfg_init_bio_out,
    input  wire [7:0]  cfg_init_bio_oe,
    input  wire [6:0]  cfg_init_tout,

    input  wire        tx_empty,
    input  wire [7:0]  tx_head,
    output reg         tx_pop,
    input  wire        rx_full,
    input  wire [3:0]  rx_level,
    output reg         rx_push,      // registered write port
    output reg  [7:0]  rx_wdata,

    input  wire        hflag,
    output reg         hflag_clr,
    input  wire        clr_flags,

    output reg  [14:0] out_reg,
    output reg  [7:0]  oe_reg,

    output wire        running,
    output reg         halted,
    output reg         irq,
    output reg         err,
    output reg         rx_ovf,
    output reg  [7:0]  x,
    output reg  [7:0]  y
);

    // ------------------------------------------------------------------
    // Opcodes
    // ------------------------------------------------------------------
    localparam [3:0] OP_CTL  = 4'h0;
    localparam [3:0] OP_JMP  = 4'h1;
    localparam [3:0] OP_JPIN = 4'h2;
    localparam [3:0] OP_WAIT = 4'h3;
    localparam [3:0] OP_IN   = 4'h4;
    localparam [3:0] OP_OUT  = 4'h5;
    localparam [3:0] OP_SETP = 4'h6;
    localparam [3:0] OP_MOV  = 4'h7;
    localparam [3:0] OP_ALU  = 4'h8;
    localparam [3:0] OP_DLY  = 4'h9;

    // ------------------------------------------------------------------
    // Architectural state
    // ------------------------------------------------------------------
    reg [4:0]  pc;
    reg [31:0] osr;
    reg [31:0] isr;
    reg [5:0]  osr_cnt;     // bits shifted out since last (auto)pull, 0..32
    reg [5:0]  isr_cnt;     // bits shifted in since last (auto)push, 0..32
    reg        lastbit;
    reg        pd_active;   // pre-delay countdown in progress
    reg        pd_done;     // pre-delay finished, instruction stalled after issue
    reg [3:0]  pd_cnt;
    reg        dl_active;
    reg [15:0] dl_cnt;
    reg [15:0] in_prev;
    reg [15:0] instr;       // prefetched imem[pc]

    assign pc_o = pc;

    wire run = ~boot & ~halted;
    assign running = run;

    // ------------------------------------------------------------------
    // Tick generator
    // ------------------------------------------------------------------
    reg  div_sync;
    reg  div_half;
    wire tick;

    pesm_clkdiv u_div (
        .clk     (clk),
        .rst_n   (rst_n),
        .en      (~boot),
        .sync    (div_sync),
        .half    (div_half),
        .div_int (cfg_div_int),
        .div_frac(cfg_div_frac),
        .tick    (tick)
    );

    // ------------------------------------------------------------------
    // Input vector (index = input pin number)
    //   0..7  BIO (uio pads, synchronized)
    //   8..11 TIN (ui_in[7:4], synchronized)
    //   12    TX FIFO not empty
    //   13    RX FIFO not full
    //   14    host flag
    //   15    0
    // ------------------------------------------------------------------
    // The RX write port is registered: a push issued in cycle t lands in the
    // FIFO at the end of t+1. The core's notion of "full" counts that pending
    // write, so it can never overfill the FIFO.
    wire        rx_full_c = rx_full | (rx_push & (rx_level == 4'd7));
    wire [15:0] in_vec = {1'b0, hflag, ~rx_full_c, ~tx_empty, tin, bio_in};

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------
    // rotate left by s (pin index arithmetic is modulo 16)
    function [15:0] rotl16;
        input [15:0] v;
        input [3:0]  s;
        begin
            rotl16 = (v << s) | (v >> (5'd16 - {1'b0, s}));
        end
    endfunction

    // low byte of v rotated right by s: bit k = v[(s+k) mod 16]
    function [7:0] rotr16_lo8;
        input [15:0] v;
        input [3:0]  s;
        reg   [15:0] t;
        integer k;
        begin
            t = (v >> s) | (v << (5'd16 - {1'b0, s}));
            for (k = 0; k < 8; k = k + 1) rotr16_lo8[k] = t[k];
        end
    endfunction

    function [7:0] mask8;
        input [5:0] n;   // 1..32
        begin
            if (n >= 6'd8) mask8 = 8'hFF;
            else           mask8 = ~(8'hFF << n[2:0]);
        end
    endfunction

    function [31:0] mask32;
        input [5:0] n;   // 1..32
        begin
            if (n >= 6'd32) mask32 = 32'hFFFF_FFFF;
            else            mask32 = (32'd1 << n) - 32'd1;
        end
    endfunction

    function [5:0] sat32;
        input [5:0] a;   // 0..32
        input [5:0] b;   // 0..32
        reg   [6:0] s;
        begin
            s = {1'b0, a} + {1'b0, b};
            sat32 = (s > 7'd32) ? 6'd32 : s[5:0];
        end
    endfunction

    function [7:0] rev8;
        input [7:0] v;
        integer k;
        begin
            for (k = 0; k < 8; k = k + 1) rev8[k] = v[7 - k];
        end
    endfunction

    // ------------------------------------------------------------------
    // Decode
    // ------------------------------------------------------------------
    wire [3:0] op      = instr[15:12];
    // class A (side-set + pre-delay tail): CTL JMP WAIT IN OUT SETP MOV
    wire       class_a = ~op[3] & (op != OP_JPIN);
    wire [3:0] tail    = instr[3:0];

    wire [2:0] sc = (cfg_side_count > 3'd4) ? 3'd4 : cfg_side_count;

    reg [3:0] side_val;
    reg [3:0] side_msk;
    reg [3:0] dly_field;
    always @(*) begin
        case (sc)
            3'd0:    begin side_val = 4'd0;            side_msk = 4'b0000; dly_field = tail;                 end
            3'd1:    begin side_val = {3'd0, tail[3]};   side_msk = 4'b0001; dly_field = {1'b0, tail[2:0]};  end
            3'd2:    begin side_val = {2'd0, tail[3:2]}; side_msk = 4'b0011; dly_field = {2'b0, tail[1:0]};  end
            3'd3:    begin side_val = {1'd0, tail[3:1]}; side_msk = 4'b0111; dly_field = {3'b0, tail[0]};    end
            default: begin side_val = tail;            side_msk = 4'b1111; dly_field = 4'd0;                 end
        endcase
    end

    wire [3:0]  dly_pre   = class_a ? dly_field : 4'd0;
    /* verilator lint_off UNUSEDSIGNAL */
    // bit 15 is the non-existent output pin 15
    wire [15:0] side_m16  = class_a ? rotl16({12'd0, side_msk}, cfg_side_base) : 16'd0;
    wire [15:0] side_v16  = rotl16({12'd0, side_val & side_msk}, cfg_side_base);
    /* verilator lint_on UNUSEDSIGNAL */

    wire [3:0] pd_rem = pd_active ? pd_cnt : dly_pre;
    wire       exec   = run & ((dly_pre == 4'd0) | pd_done | (tick & (pd_rem == 4'd1)));

    wire [5:0] pull_th = (cfg_pull_thresh == 5'd0) ? 6'd32 : {1'b0, cfg_pull_thresh};
    wire [5:0] push_th = (cfg_push_thresh == 5'd0) ? 6'd32 : {1'b0, cfg_push_thresh};

    wire [7:0] isr_byte = cfg_in_right  ? isr[31:24] : isr[7:0];
    wire [7:0] osr_byte = cfg_out_right ? osr[7:0]   : osr[31:24];

    wire [4:0] seq_pc = (pc == cfg_wrap_top) ? cfg_wrap_bot : (pc + 5'd1);
    wire [4:0] pc_next;
    assign fetch_pc = pc_next;

    // ------------------------------------------------------------------
    // Execute (combinational next-state)
    // ------------------------------------------------------------------
    reg [7:0]  x_n, y_n;
    reg [31:0] isr_n, osr_n;
    reg [5:0]  isr_cnt_n, osr_cnt_n;
    reg        lb_n;
    reg [14:0] out_n;
    reg [7:0]  oe_n;
    reg        stall;
    reg        taken;
    reg        rx_push_c;
    reg [7:0]  rx_wdata_c;
    reg [4:0]  tgt;
    reg        set_halt, set_irq, set_err, set_rxovf;
    reg        dl_active_n;
    reg [15:0] dl_cnt_n;

    // scratch
    reg        cond;
    reg [5:0]  n6;
    reg [31:0] dsrc;
    reg [31:0] dm;
    reg [31:0] osr_e;
    reg [5:0]  osr_cnt_e;
    reg [7:0]  od;
    reg [31:0] sh;
    reg [5:0]  cnt_new;
    reg [7:0]  m8;
    /* verilator lint_off UNUSEDSIGNAL */
    reg [15:0] wm16;
    reg [15:0] wv16;
    reg [31:0] od_full;
    /* verilator lint_on UNUSEDSIGNAL */
    reg [7:0]  v8;
    reg [7:0]  r8;
    reg [15:0] crc;
    reg        fb;
    reg [15:0] n16;
    reg [3:0]  pin;
    reg        sat;

    always @(*) begin
        x_n         = x;
        y_n         = y;
        isr_n       = isr;
        osr_n       = osr;
        isr_cnt_n   = isr_cnt;
        osr_cnt_n   = osr_cnt;
        lb_n        = lastbit;
        out_n       = out_reg;
        oe_n        = oe_reg;
        stall       = 1'b0;
        taken       = 1'b0;
        tgt         = 5'd0;
        tx_pop      = 1'b0;
        rx_push_c   = 1'b0;
        rx_wdata_c  = 8'd0;
        div_sync    = 1'b0;
        div_half    = 1'b0;
        set_halt    = 1'b0;
        set_irq     = 1'b0;
        set_err     = 1'b0;
        set_rxovf   = 1'b0;
        hflag_clr   = 1'b0;
        dl_active_n = dl_active;
        dl_cnt_n    = dl_cnt;

        cond      = 1'b0;
        n6        = 6'd1;
        dsrc      = 32'd0;
        dm        = 32'd0;
        osr_e     = osr;
        osr_cnt_e = osr_cnt;
        od        = 8'd0;
        od_full   = 32'd0;
        sh        = 32'd0;
        cnt_new   = 6'd0;
        m8        = 8'd0;
        wm16      = 16'd0;
        wv16      = 16'd0;
        v8        = 8'd0;
        r8        = 8'd0;
        crc       = 16'd0;
        fb        = 1'b0;
        n16       = 16'd0;
        pin       = 4'd0;
        sat       = 1'b0;

        if (exec) begin
            // ---------------- side-set ----------------
            if (cfg_side_pindir)
                oe_n = (oe_reg & ~side_m16[7:0]) | (side_v16[7:0] & side_m16[7:0]);
            else
                out_n = (out_reg & ~side_m16[14:0]) | (side_v16[14:0] & side_m16[14:0]);

            case (op)
                // ============================================================
                OP_CTL: begin
                    case (instr[11:8])
                        4'h1: begin                       // HALT
                            set_halt = 1'b1;
                            stall    = 1'b1;
                        end
                        4'h2: set_irq = 1'b1;              // IRQ
                        4'h3: begin                       // PUSH [iffull] [block]
                            if (!instr[5] || (isr_cnt >= push_th)) begin
                                if (rx_full_c) begin
                                    if (instr[4]) stall = 1'b1;
                                    else begin
                                        set_rxovf = 1'b1;
                                        isr_n     = 32'd0;
                                        isr_cnt_n = 6'd0;
                                    end
                                end else begin
                                    rx_push_c = 1'b1;
                                    rx_wdata_c = isr_byte;
                                    isr_n     = 32'd0;
                                    isr_cnt_n = 6'd0;
                                end
                            end
                        end
                        4'h4: begin                       // PULL [ifempty] [block]
                            if (!instr[5] || (osr_cnt >= pull_th)) begin
                                if (tx_empty) begin
                                    if (instr[4]) stall = 1'b1;
                                    else begin
                                        osr_n     = cfg_out_right ? {24'd0, x} : {x, 24'd0};
                                        osr_cnt_n = 6'd0;
                                    end
                                end else begin
                                    tx_pop    = 1'b1;
                                    osr_n     = cfg_out_right ? {24'd0, tx_head} : {tx_head, 24'd0};
                                    osr_cnt_n = 6'd0;
                                end
                            end
                        end
                        4'h5: begin                       // SYNC [half]
                            div_sync = 1'b1;
                            div_half = instr[4];
                        end
                        4'h6: hflag_clr = 1'b1;            // HCLR
                        4'h7: begin                       // CLR isr/osr
                            if (instr[4]) begin
                                isr_n     = 32'd0;
                                isr_cnt_n = 6'd0;
                            end
                            if (instr[5]) begin
                                osr_n     = 32'd0;
                                osr_cnt_n = 6'd32;
                            end
                        end
                        default: ;                        // NOP / reserved
                    endcase
                end

                // ============================================================
                OP_JMP: begin
                    tgt = instr[8:4];
                    case (instr[11:9])
                        3'd0: cond = 1'b1;
                        3'd1: cond = (x == 8'd0);
                        3'd2: begin cond = (x != 8'd0); x_n = x - 8'd1; end
                        3'd3: cond = (y == 8'd0);
                        3'd4: begin cond = (y != 8'd0); y_n = y - 8'd1; end
                        3'd5: cond = (x != y);
                        3'd6: cond = (osr_cnt < pull_th);
                        default: cond = lastbit;
                    endcase
                    taken = cond;
                end

                // ============================================================
                OP_JPIN: begin
                    tgt   = instr[4:0];
                    taken = (in_vec[instr[11:8]] == instr[7]);
                end

                // ============================================================
                OP_WAIT: begin
                    pin = instr[11:8];
                    if (instr[6]) begin
                        if (instr[7]) sat = in_vec[pin] & ~in_prev[pin];
                        else          sat = ~in_vec[pin] & in_prev[pin];
                    end else begin
                        sat = (in_vec[pin] == instr[7]);
                    end
                    if (!sat) stall = 1'b1;
                    else if (instr[5]) begin
                        div_sync = 1'b1;
                        div_half = instr[4];
                    end
                end

                // ============================================================
                OP_IN: begin
                    if (!instr[11]) begin
                        n6   = {3'd0, instr[6:4]} + 6'd1;
                        dsrc = {24'd0, rotr16_lo8(in_vec, instr[10:7])};
                    end else begin
                        n6 = (instr[8:4] == 5'd0) ? 6'd32 : {1'b0, instr[8:4]};
                        case (instr[10:9])
                            2'd0:    dsrc = {24'd0, x};
                            2'd1:    dsrc = {24'd0, y};
                            2'd2:    dsrc = 32'd0;
                            default: dsrc = osr;
                        endcase
                    end
                    dm = dsrc & mask32(n6);
                    if (cfg_in_right)
                        sh = (n6 == 6'd32) ? dm : ((isr >> n6) | (dm << (6'd32 - n6)));
                    else
                        sh = (n6 == 6'd32) ? dm : ((isr << n6) | dm);
                    cnt_new = sat32(isr_cnt, n6);
                    if (cfg_autopush && (cnt_new >= push_th)) begin
                        if (rx_full_c) stall = 1'b1;
                        else begin
                            rx_push_c = 1'b1;
                            rx_wdata_c = cfg_in_right ? sh[31:24] : sh[7:0];
                            isr_n     = 32'd0;
                            isr_cnt_n = 6'd0;
                            lb_n      = dm[0];
                        end
                    end else begin
                        isr_n     = sh;
                        isr_cnt_n = cnt_new;
                        lb_n      = dm[0];
                    end
                end

                // ============================================================
                OP_OUT: begin
                    if (!instr[11]) n6 = {3'd0, instr[6:4]} + 6'd1;
                    else            n6 = (instr[8:4] == 5'd0) ? 6'd32 : {1'b0, instr[8:4]};

                    if (cfg_autopull && (osr_cnt >= pull_th)) begin
                        if (tx_empty) stall = 1'b1;
                        else begin
                            tx_pop    = 1'b1;
                            osr_e     = cfg_out_right ? {24'd0, tx_head} : {tx_head, 24'd0};
                            osr_cnt_e = 6'd0;
                        end
                    end

                    if (!stall) begin
                        if (cfg_out_right) begin
                            od_full = osr_e;
                            osr_n   = (n6 == 6'd32) ? 32'd0 : (osr_e >> n6);
                        end else begin
                            od_full = (n6 == 6'd32) ? osr_e : (osr_e >> (6'd32 - n6));
                            osr_n   = (n6 == 6'd32) ? 32'd0 : (osr_e << n6);
                        end
                        od = od_full[7:0] & mask8(n6);
                        osr_cnt_n = sat32(osr_cnt_e, n6);
                        lb_n      = od[0];

                        if (!instr[11]) begin
                            m8    = mask8(n6);
                            wm16  = rotl16({8'd0, m8}, instr[10:7]);
                            wv16  = rotl16({8'd0, od}, instr[10:7]);
                            out_n = (out_n & ~wm16[14:0]) | wv16[14:0];
                        end else begin
                            case (instr[10:9])
                                2'd0: x_n = od;
                                2'd1: y_n = od;
                                2'd2: ;                     // NULL
                                default: begin              // PINDIRS (BIO 0..n-1)
                                    m8   = mask8(n6);
                                    oe_n = (oe_n & ~m8) | od;
                                end
                            endcase
                        end
                    end
                end

                // ============================================================
                OP_SETP: begin
                    pin = instr[11:8];
                    case (instr[7:6])
                        2'd0: if (pin != 4'd15) out_n[pin] = instr[5];
                        2'd1: if (!pin[3])      oe_n[pin[2:0]] = instr[5];
                        2'd2: if (pin != 4'd15) out_n[pin] = ~out_reg[pin];
                        default: begin
                            if (pin != 4'd15) out_n[pin] = instr[5];
                            if (!pin[3])      oe_n[pin[2:0]] = 1'b1;
                        end
                    endcase
                end

                // ============================================================
                OP_MOV: begin
                    case (instr[6:4])
                        3'd0:    v8 = x;
                        3'd1:    v8 = y;
                        3'd2:    v8 = isr_byte;
                        3'd3:    v8 = osr_byte;
                        3'd4:    v8 = in_vec[7:0];
                        3'd5:    v8 = in_vec[15:8];
                        3'd6:    v8 = 8'd0;
                        default: v8 = {7'd0, lastbit};
                    endcase
                    case (instr[8:7])
                        2'd0:    r8 = v8;
                        2'd1:    r8 = ~v8;
                        2'd2:    r8 = rev8(v8);
                        default: r8 = {7'd0, ^v8};
                    endcase
                    case (instr[11:9])
                        3'd0: x_n = r8;
                        3'd1: y_n = r8;
                        3'd2: begin
                            isr_n     = cfg_in_right ? {r8, 24'd0} : {24'd0, r8};
                            isr_cnt_n = 6'd8;
                        end
                        3'd3: begin
                            osr_n     = cfg_out_right ? {24'd0, r8} : {r8, 24'd0};
                            osr_cnt_n = 6'd0;
                        end
                        3'd4: out_n[7:0]  = r8;
                        3'd5: oe_n        = r8;
                        3'd6: out_n[14:8] = r8[6:0];
                        default: begin
                            taken = 1'b1;
                            tgt   = r8[4:0];
                        end
                    endcase
                end

                // ============================================================
                OP_ALU: begin
                    v8 = instr[11] ? y : x;
                    case (instr[10:8])
                        3'd0: r8 = instr[7:0];
                        3'd1: r8 = v8 & instr[7:0];
                        3'd2: r8 = v8 | instr[7:0];
                        3'd3: r8 = v8 ^ instr[7:0];
                        3'd4: r8 = v8 - 8'd1;
                        3'd5: r8 = v8 + 8'd1;
                        default: r8 = v8;
                    endcase
                    if (instr[10:8] == 3'd6) begin
                        // CRC step on {Y,X} with cfg poly, input bit = lastbit
                        crc = {y, x};
                        if (instr[0]) begin
                            fb  = crc[15] ^ lastbit;
                            crc = {crc[14:0], 1'b0} ^ (fb ? cfg_crc_poly : 16'd0);
                        end else begin
                            fb  = crc[0] ^ lastbit;
                            crc = {1'b0, crc[15:1]} ^ (fb ? cfg_crc_poly : 16'd0);
                        end
                        x_n = crc[7:0];
                        y_n = crc[15:8];
                    end else if (instr[10:8] != 3'd7) begin
                        if (instr[11]) y_n = r8;
                        else           x_n = r8;
                    end
                end

                // ============================================================
                OP_DLY: begin
                    n16 = instr[10] ? {x, y} : {6'd0, instr[9:0]};
                    if (!dl_active) begin
                        if (n16 != 16'd0) begin
                            if (instr[11]) begin
                                if (!(tick && (n16 == 16'd1))) begin
                                    stall       = 1'b1;
                                    dl_active_n = 1'b1;
                                    dl_cnt_n    = n16 - {15'd0, tick};
                                end
                            end else begin
                                stall       = 1'b1;
                                dl_active_n = 1'b1;
                                dl_cnt_n    = n16;
                            end
                        end
                    end else begin
                        if (instr[11] && !tick) begin
                            stall = 1'b1;
                        end else if (dl_cnt != 16'd1) begin
                            stall    = 1'b1;
                            dl_cnt_n = dl_cnt - 16'd1;
                        end
                    end
                end

                // ============================================================
                default: begin                             // illegal opcode
                    set_err  = 1'b1;
                    set_halt = 1'b1;
                    stall    = 1'b1;
                end
            endcase
        end
    end

    assign pc_next = boot ? cfg_entry :
                     (exec & ~stall) ? (taken ? tgt : seq_pc) : pc;

    // ------------------------------------------------------------------
    // State update
    // ------------------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pc        <= 5'd0;
            x         <= 8'd0;
            y         <= 8'd0;
            osr       <= 32'd0;
            isr       <= 32'd0;
            osr_cnt   <= 6'd32;
            isr_cnt   <= 6'd0;
            lastbit   <= 1'b0;
            pd_active <= 1'b0;
            pd_done   <= 1'b0;
            pd_cnt    <= 4'd0;
            dl_active <= 1'b0;
            dl_cnt    <= 16'd0;
            in_prev   <= 16'd0;
            instr     <= 16'h0100;
            rx_push   <= 1'b0;
            rx_wdata  <= 8'd0;
            out_reg   <= 15'd0;
            oe_reg    <= 8'd0;
            halted    <= 1'b0;
            irq       <= 1'b0;
            err       <= 1'b0;
            rx_ovf    <= 1'b0;
        end else begin
            in_prev  <= in_vec;
            instr    <= fetch_instr;
            pc       <= pc_next;
            rx_push  <= rx_push_c;
            rx_wdata <= rx_wdata_c;

            if (clr_flags) begin
                irq    <= 1'b0;
                err    <= 1'b0;
                rx_ovf <= 1'b0;
            end

            if (boot) begin
                x         <= 8'd0;
                y         <= 8'd0;
                osr       <= 32'd0;
                isr       <= 32'd0;
                osr_cnt   <= 6'd32;
                isr_cnt   <= 6'd0;
                lastbit   <= 1'b0;
                pd_active <= 1'b0;
                pd_done   <= 1'b0;
                pd_cnt    <= 4'd0;
                dl_active <= 1'b0;
                dl_cnt    <= 16'd0;
                halted    <= 1'b0;
                out_reg   <= {cfg_init_tout, cfg_init_bio_out};
                oe_reg    <= cfg_init_bio_oe;
            end else if (exec) begin
                x       <= x_n;
                y       <= y_n;
                isr     <= isr_n;
                osr     <= osr_n;
                isr_cnt <= isr_cnt_n;
                osr_cnt <= osr_cnt_n;
                lastbit <= lb_n;
                out_reg <= out_n;
                oe_reg  <= oe_n;
                pd_active <= 1'b0;
                if (stall) begin
                    pd_done   <= (dly_pre != 4'd0);
                    dl_active <= dl_active_n;
                    dl_cnt    <= dl_cnt_n;
                end else begin
                    pd_done   <= 1'b0;
                    dl_active <= 1'b0;
                end
                if (set_halt)  halted <= 1'b1;
                if (set_irq)   irq    <= 1'b1;
                if (set_err)   err    <= 1'b1;
                if (set_rxovf) rx_ovf <= 1'b1;
            end else if (run) begin
                // pre-delay countdown on the tick grid
                pd_active <= 1'b1;
                pd_cnt    <= pd_rem - {3'd0, tick};
            end
        end
    end

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume (!rst_n);

    // Configuration is only written while boot == 1 (enforced by pesm_host).
    always @(posedge clk) begin
        if (f_past_valid && !boot && !$past(boot)) begin
            assume ($stable(cfg_div_int));
            assume ($stable(cfg_div_frac));
            assume ($stable(cfg_side_count));
            assume ($stable(cfg_out_right));
            assume ($stable(cfg_in_right));
            assume ($stable(cfg_pull_thresh));
            assume ($stable(cfg_push_thresh));
            assume ($stable(cfg_autopull));
            assume ($stable(cfg_autopush));
            assume ($stable(cfg_side_pindir));
            assume ($stable(cfg_side_base));
            assume ($stable(cfg_wrap_top));
            assume ($stable(cfg_wrap_bot));
            assume ($stable(cfg_crc_poly));
        end
    end

    // imem content (write-protected while running) modelled as a constant
    (* anyconst *) reg [511:0] f_imem;
    always @(*) assume (fetch_instr == f_imem[{fetch_pc, 4'd0} +: 16]);
    // prefetch consistency: the executing instruction is always imem[pc]
    // (the chip's MODE synchronizer resets to BOOT, so the first cycle after reset is boot)
    always @(posedge clk) if (f_past_valid && !$past(rst_n)) assume (boot);
    always @(*) if (rst_n && f_past_valid && !boot) assert (instr == f_imem[{pc, 4'd0} +: 16]);

    // ---- FIFO handshake safety ----
    always @(*) begin
        if (rst_n) begin
            assert (!(rx_push_c && rx_full_c));
            assert (!(tx_pop && tx_empty));
            if (!run) begin
                assert (!rx_push_c);
                assert (!tx_pop);
            end
        end
    end

    // ---- shift counters stay in range ----
    always @(*) begin
        if (rst_n) begin
            assert (osr_cnt <= 6'd32);
            assert (isr_cnt <= 6'd32);
        end
    end

    // ---- pre-delay: a delayed instruction executes only on a tick ----
    always @(*) begin
        if (rst_n && exec && dly_pre != 4'd0 && !pd_done) assert (tick);
        if (rst_n && !boot && pd_active) assert (pd_cnt != 4'd0 && pd_cnt <= dly_pre);
    end

    // ---- a stalled instruction never advances pc ----
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(boot) && $past(exec) && $past(stall))
            assert (pc == $past(pc));
        if (f_past_valid && rst_n && $past(rst_n) && !$past(boot) && !$past(exec))
            assert (pc == $past(pc));
    end

    // ---- halted core is frozen (outputs do not change) ----
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && $past(halted) && !$past(boot)) begin
            assert ($stable(out_reg));
            assert ($stable(oe_reg));
            assert ($stable(pc));
            assert (halted);
        end
    end

    // ---- side-set: pins outside side mask and not targeted by the op are untouched ----
    // For NOP-class CTL with side-set, only side pins may change.
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(boot) && $past(exec) &&
            $past(op) == OP_CTL && $past(instr[11:8]) == 4'h0 && !$past(cfg_side_pindir)) begin
            assert ((out_reg & ~$past(side_m16[14:0])) == ($past(out_reg) & ~$past(side_m16[14:0])));
            assert ((out_reg &  $past(side_m16[14:0])) == ($past(side_v16[14:0]) & $past(side_m16[14:0])));
            assert (oe_reg == $past(oe_reg));
        end
    end

    // ---- edge WAIT never releases without an edge ----
    always @(*) begin
        if (rst_n && exec && op == OP_WAIT && instr[6] && !stall)
            assert (in_vec[instr[11:8]] != in_prev[instr[11:8]]);
    end

    // ---- autopush pushes exactly at threshold ----
    always @(*) begin
        if (rst_n && exec && op == OP_IN && rx_push_c)
            assert (cfg_autopush && (sat32(isr_cnt, n6) >= push_th));
    end

    // ---- covers ----
    always @(*) begin
        cover (rst_n && exec && op == OP_OUT && tx_pop);
        cover (rst_n && exec && op == OP_WAIT && instr[6] && !stall);
        cover (rst_n && halted);
    end
`endif

endmodule

`default_nettype wire
