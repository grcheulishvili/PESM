/*
 * Copyright (c) 2026 PESM contributors
 * SPDX-License-Identifier: Apache-2.0
 *
 * pesm_host: SPI mode-0 slave (oversampled in the clk domain) that owns
 * the instruction memory, the configuration registers and the host side
 * of both FIFOs.
 *
 * Frame: CS_N low, byte 0 = command, then data bytes, MSB first.
 * MISO returns STATUS0 during the command byte.
 *
 *   cmd[7:5]  name        data phase
 *   000 aaaaa WRITE_IMEM  {hi, lo} pairs -> imem[a++]        (BOOT only)
 *   001 aaaaa READ_IMEM   MISO: hi, lo of imem[a++] ...
 *   010 -aaaa WRITE_CFG   bytes -> cfg[a++]                  (BOOT only)
 *   011 -aaaa READ_CFG    MISO: cfg[a++] ...
 *   100 ----- WRITE_TX    bytes -> TX FIFO (full: dropped, TX_OVF)
 *   101 ----- READ_RX     MISO: RX FIFO bytes (empty: 0x00, RX_UNF)
 *   110 ----- READ_STAT   MISO: STATUS0, STATUS1, PC, X, Y, 0...
 *   111 fffff CONTROL     f0 FLUSH_TX, f1 FLUSH_RX, f2 HFLAG_SET,
 *                         f3 HFLAG_CLR, f4 CLR_FLAGS (no data phase)
 *
 * SCK must satisfy t_high, t_low >= 4 clk periods (f_SCK <= f_clk/8).
 * A pop of the RX FIFO is committed on the first SCK rising edge of the
 * byte that carries it, so de-asserting CS_N early never loses data.
 */

`default_nettype none

module pesm_host (
    input  wire        clk,
    input  wire        rst_n,

    input  wire        sck,          // synchronized
    input  wire        mosi,         // synchronized
    input  wire        csn,          // synchronized
    output wire        miso,

    input  wire        boot,

    input  wire [4:0]  core_pc,      // architectural pc (status only)
    input  wire [4:0]  fetch_pc,     // core prefetch address
    output wire [15:0] core_instr,   // imem[fetch_pc], with write bypass

    output wire [15:0] cfg_div_int,
    output wire [7:0]  cfg_div_frac,
    output wire        cfg_out_right,
    output wire        cfg_in_right,
    output wire        cfg_autopull,
    output wire        cfg_autopush,
    output wire [4:0]  cfg_pull_thresh,
    output wire [4:0]  cfg_push_thresh,
    output wire [2:0]  cfg_side_count,
    output wire        cfg_side_pindir,
    output wire [3:0]  cfg_side_base,
    output wire [4:0]  cfg_wrap_top,
    output wire [4:0]  cfg_wrap_bot,
    output wire [4:0]  cfg_entry,
    output wire [7:0]  cfg_od_mask,
    output wire [15:0] cfg_crc_poly,
    output wire [7:0]  cfg_init_bio_out,
    output wire [7:0]  cfg_init_bio_oe,
    output wire [6:0]  cfg_init_tout,

    output reg         tx_push,
    output reg  [7:0]  tx_wdata,
    input  wire        tx_full,
    input  wire [3:0]  tx_level,
    output reg         rx_pop,
    input  wire        rx_empty,
    input  wire [7:0]  rx_head,
    input  wire [3:0]  rx_level,

    output reg         flush_tx,
    output reg         flush_rx,
    output reg         hflag_set,
    output reg         hflag_clr,
    output reg         clr_flags,

    input  wire        st_running,
    input  wire        st_halted,
    input  wire        st_irq,
    input  wire        st_err,
    input  wire        st_rx_ovf,
    input  wire        st_hflag,
    input  wire [7:0]  st_x,
    input  wire [7:0]  st_y
);

    // ------------------------------------------------------------------
    // Instruction memory (32 x 16 flop array). Reset content = HALT.
    // ------------------------------------------------------------------
    localparam [15:0] INSTR_HALT = 16'h0100;

    reg [15:0] imem [0:31];
    wire        imem_we;
    wire [4:0]  imem_wa;
    wire [15:0] imem_wd;
    // bypass: a write landing in the same cycle as the core's (BOOT-time)
    // prefetch of that address must be seen by the prefetch register
    assign core_instr = (imem_we && imem_wa == fetch_pc) ? imem_wd : imem[fetch_pc];

    // ------------------------------------------------------------------
    // Configuration registers
    // ------------------------------------------------------------------
    reg [7:0] r_div_l, r_div_h, r_frac, r_side;
    reg [3:0] r_shiftctl;
    reg [4:0] r_pull, r_push;
    reg [4:0] r_wrap_top, r_wrap_bot, r_entry;
    reg [7:0] r_od, r_crc_l, r_crc_h, r_init_out, r_init_oe;
    reg [6:0] r_init_tout;

    assign cfg_div_int      = {r_div_h, r_div_l};
    assign cfg_div_frac     = r_frac;
    assign cfg_out_right    = r_shiftctl[0];
    assign cfg_in_right     = r_shiftctl[1];
    assign cfg_autopull     = r_shiftctl[2];
    assign cfg_autopush     = r_shiftctl[3];
    assign cfg_pull_thresh  = r_pull;
    assign cfg_push_thresh  = r_push;
    assign cfg_side_count   = r_side[2:0];
    assign cfg_side_pindir  = r_side[3];
    assign cfg_side_base    = r_side[7:4];
    assign cfg_wrap_top     = r_wrap_top;
    assign cfg_wrap_bot     = r_wrap_bot;
    assign cfg_entry        = r_entry;
    assign cfg_od_mask      = r_od;
    assign cfg_crc_poly     = {r_crc_h, r_crc_l};
    assign cfg_init_bio_out = r_init_out;
    assign cfg_init_bio_oe  = r_init_oe;
    assign cfg_init_tout    = r_init_tout;

    reg [7:0] cfg_rdata;
    reg [3:0] cfg_raddr;
    always @(*) begin
        case (cfg_raddr)
            4'd0:    cfg_rdata = r_div_l;
            4'd1:    cfg_rdata = r_div_h;
            4'd2:    cfg_rdata = r_frac;
            4'd3:    cfg_rdata = {4'd0, r_shiftctl};
            4'd4:    cfg_rdata = {3'd0, r_pull};
            4'd5:    cfg_rdata = {3'd0, r_push};
            4'd6:    cfg_rdata = r_side;
            4'd7:    cfg_rdata = {3'd0, r_wrap_top};
            4'd8:    cfg_rdata = {3'd0, r_wrap_bot};
            4'd9:    cfg_rdata = {3'd0, r_entry};
            4'd10:   cfg_rdata = r_od;
            4'd11:   cfg_rdata = r_crc_l;
            4'd12:   cfg_rdata = r_crc_h;
            4'd13:   cfg_rdata = r_init_out;
            4'd14:   cfg_rdata = r_init_oe;
            default: cfg_rdata = {1'b0, r_init_tout};
        endcase
    end

    // ------------------------------------------------------------------
    // Sticky host-side flags
    // ------------------------------------------------------------------
    reg tx_ovf;
    reg rx_unf;
    reg wr_err;     // imem/cfg write attempted while running

    wire [7:0] status0 = {st_running, st_halted, st_irq, st_err | wr_err,
                          tx_ovf, st_rx_ovf, rx_unf, st_hflag};
    wire [7:0] status1 = {tx_level, rx_level};

    // ------------------------------------------------------------------
    // SPI engine
    // ------------------------------------------------------------------
    reg        sck_q;
    reg [2:0]  bitcnt;
    reg [6:0]  rx_sr;
    reg [7:0]  miso_sr;
    reg        have_cmd;
    reg [2:0]  cmd_op;
    reg [4:0]  addr;
    reg        phase;
    reg [7:0]  hold;
    reg [2:0]  sidx;
    reg        pend_pop;
    reg        pend_unf;

    assign miso = miso_sr[7];

    wire       sck_rise  = sck & ~sck_q;
    wire       sck_fall  = ~sck & sck_q;
    wire [7:0] rx_byte   = {rx_sr, mosi};
    wire       byte_done = sck_rise & (bitcnt == 3'd7);
    assign imem_we = ~csn & byte_done & have_cmd & (cmd_op == 3'b000) & phase & boot;
    assign imem_wa = addr;
    assign imem_wd = {hold, rx_byte};

    // Byte presented on MISO for the next data byte
    reg  [7:0] tx_byte;
    wire [15:0] imem_rd = imem[addr];
    always @(*) begin
        cfg_raddr = addr[3:0];
        case (cmd_op)
            3'b001:  tx_byte = phase ? imem_rd[7:0] : imem_rd[15:8];
            3'b011:  tx_byte = cfg_rdata;
            3'b101:  tx_byte = rx_empty ? 8'h00 : rx_head;
            3'b110: begin
                case (sidx)
                    3'd0:    tx_byte = status0;
                    3'd1:    tx_byte = status1;
                    3'd2:    tx_byte = {3'd0, core_pc};
                    3'd3:    tx_byte = st_x;
                    3'd4:    tx_byte = st_y;
                    default: tx_byte = 8'h00;
                endcase
            end
            default: tx_byte = status0;
        endcase
    end

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            sck_q     <= 1'b0;
            bitcnt    <= 3'd0;
            rx_sr     <= 7'd0;
            miso_sr   <= 8'd0;
            have_cmd  <= 1'b0;
            cmd_op    <= 3'd0;
            addr      <= 5'd0;
            phase     <= 1'b0;
            hold      <= 8'd0;
            sidx      <= 3'd0;
            pend_pop  <= 1'b0;
            pend_unf  <= 1'b0;
            tx_ovf    <= 1'b0;
            rx_unf    <= 1'b0;
            wr_err    <= 1'b0;
            tx_push   <= 1'b0;
            tx_wdata  <= 8'd0;
            rx_pop    <= 1'b0;
            flush_tx  <= 1'b0;
            flush_rx  <= 1'b0;
            hflag_set <= 1'b0;
            hflag_clr <= 1'b0;
            clr_flags <= 1'b0;
            r_div_l     <= 8'd0;
            r_div_h     <= 8'd0;
            r_frac      <= 8'd0;
            r_shiftctl  <= 4'h3;
            r_pull      <= 5'd8;
            r_push      <= 5'd8;
            r_side      <= 8'd0;
            r_wrap_top  <= 5'd31;
            r_wrap_bot  <= 5'd0;
            r_entry     <= 5'd0;
            r_od        <= 8'd0;
            r_crc_l     <= 8'd0;
            r_crc_h     <= 8'd0;
            r_init_out  <= 8'd0;
            r_init_oe   <= 8'd0;
            r_init_tout <= 7'd0;
            for (i = 0; i < 32; i = i + 1) imem[i] <= INSTR_HALT;
        end else begin
            sck_q     <= sck;
            tx_push   <= 1'b0;
            rx_pop    <= 1'b0;
            flush_tx  <= 1'b0;
            flush_rx  <= 1'b0;
            hflag_set <= 1'b0;
            hflag_clr <= 1'b0;
            clr_flags <= 1'b0;

            if (clr_flags) begin
                tx_ovf <= 1'b0;
                rx_unf <= 1'b0;
                wr_err <= 1'b0;
            end

            if (csn) begin
                bitcnt   <= 3'd0;
                have_cmd <= 1'b0;
                phase    <= 1'b0;
                sidx     <= 3'd0;
                pend_pop <= 1'b0;
                pend_unf <= 1'b0;
                miso_sr  <= status0;
            end else begin
                // ---------------- rising edge: sample MOSI ----------------
                if (sck_rise) begin
                    rx_sr  <= rx_byte[6:0];
                    bitcnt <= bitcnt + 3'd1;
                    if (pend_pop) rx_pop <= 1'b1;
                    if (pend_unf) rx_unf <= 1'b1;
                    pend_pop <= 1'b0;
                    pend_unf <= 1'b0;
                end

                if (byte_done) begin
                    if (!have_cmd) begin
                        have_cmd <= 1'b1;
                        cmd_op   <= rx_byte[7:5];
                        addr     <= rx_byte[4:0];
                        phase    <= 1'b0;
                        sidx     <= 3'd0;
                        if (rx_byte[7:5] == 3'b111) begin
                            flush_tx  <= rx_byte[0];
                            flush_rx  <= rx_byte[1];
                            hflag_set <= rx_byte[2];
                            hflag_clr <= rx_byte[3];
                            clr_flags <= rx_byte[4];
                        end
                    end else begin
                        case (cmd_op)
                            3'b000: begin
                                if (!phase) begin
                                    hold  <= rx_byte;
                                    phase <= 1'b1;
                                end else begin
                                    if (imem_we) imem[imem_wa] <= imem_wd;
                                    else         wr_err        <= 1'b1;
                                    addr  <= addr + 5'd1;
                                    phase <= 1'b0;
                                end
                            end
                            3'b010: begin
                                if (boot) begin
                                    case (addr[3:0])
                                        4'd0:  r_div_l     <= rx_byte;
                                        4'd1:  r_div_h     <= rx_byte;
                                        4'd2:  r_frac      <= rx_byte;
                                        4'd3:  r_shiftctl  <= rx_byte[3:0];
                                        4'd4:  r_pull      <= rx_byte[4:0];
                                        4'd5:  r_push      <= rx_byte[4:0];
                                        4'd6:  r_side      <= rx_byte;
                                        4'd7:  r_wrap_top  <= rx_byte[4:0];
                                        4'd8:  r_wrap_bot  <= rx_byte[4:0];
                                        4'd9:  r_entry     <= rx_byte[4:0];
                                        4'd10: r_od        <= rx_byte;
                                        4'd11: r_crc_l     <= rx_byte;
                                        4'd12: r_crc_h     <= rx_byte;
                                        4'd13: r_init_out  <= rx_byte;
                                        4'd14: r_init_oe   <= rx_byte;
                                        default: r_init_tout <= rx_byte[6:0];
                                    endcase
                                end else begin
                                    wr_err <= 1'b1;
                                end
                                addr <= {1'b0, addr[3:0] + 4'd1};
                            end
                            3'b100: begin
                                if (!tx_full) begin
                                    tx_push  <= 1'b1;
                                    tx_wdata <= rx_byte;
                                end else begin
                                    tx_ovf <= 1'b1;
                                end
                            end
                            default: ;
                        endcase
                    end
                end

                // ---------------- falling edge: drive MISO ----------------
                if (sck_fall) begin
                    if (bitcnt == 3'd0) begin
                        miso_sr <= tx_byte;
                        case (cmd_op)
                            3'b001: begin
                                if (phase) addr <= addr + 5'd1;
                                phase <= ~phase;
                            end
                            3'b011: addr <= {1'b0, addr[3:0] + 4'd1};
                            3'b101: begin
                                pend_pop <= ~rx_empty;
                                pend_unf <=  rx_empty;
                            end
                            3'b110: if (sidx != 3'd7) sidx <= sidx + 3'd1;
                            default: ;
                        endcase
                    end else begin
                        miso_sr <= {miso_sr[6:0], 1'b0};
                    end
                end
            end
        end
    end

`ifdef FORMAL
    reg f_past_valid = 1'b0;
    always @(posedge clk) f_past_valid <= 1'b1;
    always @(*) if (!f_past_valid) assume (!rst_n);

    // imem and cfg never change while the core runs
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(boot)) begin
            assert ($stable(r_div_l));
            assert ($stable(r_div_h));
            assert ($stable(r_frac));
            assert ($stable(r_shiftctl));
            assert ($stable(r_side));
            assert ($stable(r_pull));
            assert ($stable(r_push));
            assert ($stable(r_wrap_top));
            assert ($stable(r_entry));
            assert ($stable(r_wrap_bot));
            assert ($stable(r_crc_l));
            assert ($stable(r_crc_h));
            assert ($stable(r_od));
        end
    end

    // imem: pick an arbitrary word, it is stable while running
    (* anyconst *) reg [4:0] f_a;
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(boot))
            assert (imem[f_a] == $past(imem[f_a]));
    end

    // bit counter only moves on SCK rising edges inside a frame
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && !$past(csn) && !$past(sck_rise))
            assert (bitcnt == $past(bitcnt));
        if (f_past_valid && rst_n && $past(rst_n) && $past(csn))
            assert (bitcnt == 3'd0 && !have_cmd);
    end

    always @(*) cover (rst_n && byte_done && have_cmd && cmd_op == 3'b000 && !phase);
`endif

endmodule

`default_nettype wire
