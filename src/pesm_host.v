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
 *   cmd        name        data phase
 *   00 aaaaaa  WRITE_IMEM  {hi, lo} pairs -> imem[a++]        (BOOT only)
 *   01 aaaaaa  READ_IMEM   MISO: hi, lo of imem[a++] ...             (BOOT only)
 *   100 aaaaa  WRITE_CFG   bytes -> cfg[a++]                  (BOOT only)
 *   101 aaaaa  READ_CFG    MISO: cfg[a++] ...
 *   110 00---  WRITE_TX    bytes -> TX FIFO (full: dropped, TX_OVF)
 *   110 01---  READ_RX     MISO: RX FIFO bytes (empty: 0x00, RX_UNF)
 *   110 10---  READ_STAT   MISO: STATUS0, STATUS1, PC, X, Y, ID, 0...
 *   110 11---  reserved    (ignored)
 *   111 fffff  CONTROL     f0 FLUSH_TX, f1 FLUSH_RX, f2 HFLAG_SET,
 *                          f3 HFLAG_CLR, f4 CLR_FLAGS (no data phase)
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

    input  wire        boot,         // core is held in BOOT this cycle
    input  wire        boot_pre,     // value boot takes in the next cycle

    input  wire [5:0]  core_pc,      // architectural pc (status only)
    input  wire [5:0]  rd_pc,        // core prefetch address
    output wire [15:0] rd_instr,     // imem[rd_pc] (for the core: valid when !boot or !boot_pre)

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
    output wire [5:0]  cfg_wrap_top,
    output wire [5:0]  cfg_wrap_bot,
    output wire [5:0]  cfg_entry,
    output wire [7:0]  cfg_od_mask,
    output wire [15:0] cfg_crc_poly,
    output wire [7:0]  cfg_init_bio_out,
    output wire [7:0]  cfg_init_bio_oe,
    output wire [6:0]  cfg_init_tout,
    output wire [7:0]  cfg_pat_mask,
    output wire [7:0]  cfg_pat_val,
    output wire [3:0]  cfg_bg_pin,
    output wire        cfg_bg_en,
    output wire        cfg_bg_auto,
    output wire        cfg_bg_idle,
    output wire [7:0]  cfg_bg_div,

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
    // Instruction memory (64 x 16 flop array). Reset content = HALT.
    // One read port, shared in time:
    //   running, and the last BOOT cycle : core prefetch (rd_pc)
    //   BOOT                             : host readback (addr)
    // ------------------------------------------------------------------
    localparam [15:0] INSTR_HALT = 16'h0100;
    localparam [7:0]  CHIP_ID    = 8'h30;      // PESM ISA 3.0

    reg [15:0] imem [0:63];
    wire        imem_we;
    wire [5:0]  imem_wa;
    wire [15:0] imem_wd;

    // 64:1 read mux as an explicit three-level tree of 4:1 muxes that select
    // on the address bits directly (see pesm_mux4.v).
    wire [5:0]  rd_addr;
    wire [15:0] rd_l1 [0:15];
    wire [15:0] rd_l2 [0:3];
    genvar gb, gw;
    generate
        for (gb = 0; gb < 16; gb = gb + 1) begin : g_rd
            for (gw = 0; gw < 16; gw = gw + 1) begin : g_l1
                pesm_mux4 u_m (
                    .d({imem[4*gw+3][gb], imem[4*gw+2][gb], imem[4*gw+1][gb], imem[4*gw][gb]}),
                    .s(rd_addr[1:0]), .y(rd_l1[gw][gb])
                );
            end
            for (gw = 0; gw < 4; gw = gw + 1) begin : g_l2
                pesm_mux4 u_m (
                    .d({rd_l1[4*gw+3][gb], rd_l1[4*gw+2][gb], rd_l1[4*gw+1][gb], rd_l1[4*gw][gb]}),
                    .s(rd_addr[3:2]), .y(rd_l2[gw][gb])
                );
            end
            pesm_mux4 u_l3 (
                .d({rd_l2[3][gb], rd_l2[2][gb], rd_l2[1][gb], rd_l2[0][gb]}),
                .s(rd_addr[5:4]), .y(rd_instr[gb])
            );
        end
    endgenerate

    // Writes to imem/cfg are accepted only while the core is in BOOT and
    // stays there for one more cycle. Everything the core loads in BOOT
    // (entry word, entry pc, initial pin state, background clock state) is
    // therefore stable in its last BOOT cycle.
    wire wr_ok = boot & boot_pre;

    // ------------------------------------------------------------------
    // Configuration registers
    // ------------------------------------------------------------------
    reg [7:0] r_div_l, r_div_h, r_frac, r_side;
    reg [3:0] r_shiftctl;
    reg [4:0] r_pull, r_push;
    reg [5:0] r_wrap_top, r_wrap_bot, r_entry;
    reg [7:0] r_od, r_crc_l, r_crc_h, r_init_out, r_init_oe;
    reg [6:0] r_init_tout;
    reg [7:0] r_pat_mask, r_pat_val, r_bg_div;
    reg [6:0] r_bg_ctl;

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
    assign cfg_pat_mask     = r_pat_mask;
    assign cfg_pat_val      = r_pat_val;
    assign cfg_bg_pin       = r_bg_ctl[3:0];
    assign cfg_bg_en        = r_bg_ctl[4];
    assign cfg_bg_auto      = r_bg_ctl[5];
    assign cfg_bg_idle      = r_bg_ctl[6];
    assign cfg_bg_div       = r_bg_div;

    reg [7:0] cfg_rdata;
    reg [4:0] cfg_raddr;
    always @(*) begin
        case (cfg_raddr)
            5'd0:    cfg_rdata = r_div_l;
            5'd1:    cfg_rdata = r_div_h;
            5'd2:    cfg_rdata = r_frac;
            5'd3:    cfg_rdata = {4'd0, r_shiftctl};
            5'd4:    cfg_rdata = {3'd0, r_pull};
            5'd5:    cfg_rdata = {3'd0, r_push};
            5'd6:    cfg_rdata = r_side;
            5'd7:    cfg_rdata = {2'd0, r_wrap_top};
            5'd8:    cfg_rdata = {2'd0, r_wrap_bot};
            5'd9:    cfg_rdata = {2'd0, r_entry};
            5'd10:   cfg_rdata = r_od;
            5'd11:   cfg_rdata = r_crc_l;
            5'd12:   cfg_rdata = r_crc_h;
            5'd13:   cfg_rdata = r_init_out;
            5'd14:   cfg_rdata = r_init_oe;
            5'd15:   cfg_rdata = {1'b0, r_init_tout};
            5'd16:   cfg_rdata = r_pat_mask;
            5'd17:   cfg_rdata = r_pat_val;
            5'd18:   cfg_rdata = {1'b0, r_bg_ctl};
            5'd19:   cfg_rdata = r_bg_div;
            default: cfg_rdata = 8'h00;
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
    reg [5:0]  addr;
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
    assign imem_we = ~csn & byte_done & have_cmd & (cmd_op == 3'b000) & phase & wr_ok;
    assign imem_wa = addr;
    assign imem_wd = {hold, rx_byte};

    // Command decode (first byte of a frame) -> internal op
    //   000 WRITE_IMEM 001 READ_IMEM 010 WRITE_CFG 011 READ_CFG
    //   100 WRITE_TX   101 READ_RX   110 READ_STAT 111 CONTROL / reserved
    reg [2:0] dec_op;
    reg [5:0] dec_addr;
    always @(*) begin
        dec_addr = rx_byte[5:0];
        casez (rx_byte[7:5])
            3'b00?:  dec_op = 3'b000;
            3'b01?:  dec_op = 3'b001;
            3'b100:  begin dec_op = 3'b010; dec_addr = {1'b0, rx_byte[4:0]}; end
            3'b101:  begin dec_op = 3'b011; dec_addr = {1'b0, rx_byte[4:0]}; end
            3'b110:  dec_op = {1'b1, rx_byte[4:3]};
            default: dec_op = 3'b111;
        endcase
    end

    // Byte presented on MISO for the next data byte
    reg  [7:0] tx_byte;
    assign rd_addr = (boot & boot_pre) ? addr : rd_pc;
    // Host readback is registered: the SPI side has several clk between an
    // address change and the next MISO byte load, and the register keeps the
    // core's prefetch path (instr -> branch decision -> read mux) out of the
    // MISO shift register.
    reg [15:0] imem_rd;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) imem_rd <= 16'd0;
        else        imem_rd <= rd_instr;
    end
    always @(*) begin
        cfg_raddr = addr[4:0];
        case (cmd_op)
            3'b001:  tx_byte = phase ? imem_rd[7:0] : imem_rd[15:8];
            3'b011:  tx_byte = cfg_rdata;
            3'b101:  tx_byte = rx_empty ? 8'h00 : rx_head;
            3'b110: begin
                case (sidx)
                    3'd0:    tx_byte = status0;
                    3'd1:    tx_byte = status1;
                    3'd2:    tx_byte = {2'd0, core_pc};
                    3'd3:    tx_byte = st_x;
                    3'd4:    tx_byte = st_y;
                    3'd5:    tx_byte = CHIP_ID;
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
            addr      <= 6'd0;
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
            r_wrap_top  <= 6'd63;
            r_wrap_bot  <= 6'd0;
            r_entry     <= 6'd0;
            r_od        <= 8'd0;
            r_crc_l     <= 8'd0;
            r_crc_h     <= 8'd0;
            r_init_out  <= 8'd0;
            r_init_oe   <= 8'd0;
            r_init_tout <= 7'd0;
            r_pat_mask  <= 8'd0;
            r_pat_val   <= 8'd0;
            r_bg_ctl    <= 7'd0;
            r_bg_div    <= 8'd0;
            for (i = 0; i < 64; i = i + 1) imem[i] <= INSTR_HALT;
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
                        cmd_op   <= dec_op;
                        addr     <= dec_addr;
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
                                    addr  <= addr + 6'd1;
                                    phase <= 1'b0;
                                end
                            end
                            3'b010: begin
                                if (wr_ok) begin
                                    case (addr[4:0])
                                        5'd0:  r_div_l     <= rx_byte;
                                        5'd1:  r_div_h     <= rx_byte;
                                        5'd2:  r_frac      <= rx_byte;
                                        5'd3:  r_shiftctl  <= rx_byte[3:0];
                                        5'd4:  r_pull      <= rx_byte[4:0];
                                        5'd5:  r_push      <= rx_byte[4:0];
                                        5'd6:  r_side      <= rx_byte;
                                        5'd7:  r_wrap_top  <= rx_byte[5:0];
                                        5'd8:  r_wrap_bot  <= rx_byte[5:0];
                                        5'd9:  r_entry     <= rx_byte[5:0];
                                        5'd10: r_od        <= rx_byte;
                                        5'd11: r_crc_l     <= rx_byte;
                                        5'd12: r_crc_h     <= rx_byte;
                                        5'd13: r_init_out  <= rx_byte;
                                        5'd14: r_init_oe   <= rx_byte;
                                        5'd15: r_init_tout <= rx_byte[6:0];
                                        5'd16: r_pat_mask  <= rx_byte;
                                        5'd17: r_pat_val   <= rx_byte;
                                        5'd18: r_bg_ctl    <= rx_byte[6:0];
                                        5'd19: r_bg_div    <= rx_byte;
                                        default: ;
                                    endcase
                                end else begin
                                    wr_err <= 1'b1;
                                end
                                addr <= {1'b0, addr[4:0] + 5'd1};
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
                                if (phase) addr <= addr + 6'd1;
                                phase <= ~phase;
                            end
                            3'b011: addr <= {1'b0, addr[4:0] + 5'd1};
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

    // boot is boot_pre delayed by one cycle (top level)
    always @(posedge clk) if (f_past_valid && $past(rst_n)) assume (boot == $past(boot_pre));

    // the read port belongs to the core while it runs and in its last BOOT cycle
    always @(*) if (rst_n && (!boot || !boot_pre)) assert (rd_instr == imem[rd_pc]);

    // imem and cfg never change while the core runs, nor at the edge that
    // ends BOOT (the core's last BOOT cycle sees the final values)
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && (!$past(boot) || !boot)) begin
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
            assert ($stable(r_init_out));
            assert ($stable(r_init_oe));
            assert ($stable(r_init_tout));
            assert ($stable(r_pat_mask));
            assert ($stable(r_pat_val));
            assert ($stable(r_bg_ctl));
            assert ($stable(r_bg_div));
        end
    end

    // imem: pick an arbitrary word, same guarantee
    (* anyconst *) reg [5:0] f_a;
    always @(posedge clk) begin
        if (f_past_valid && rst_n && $past(rst_n) && (!$past(boot) || !boot))
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
