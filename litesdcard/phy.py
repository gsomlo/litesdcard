from litex.gen import *
from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *

from litesdcard.common import *


def _sdpads():
    sdpads = Record([
        ("data", [
            ("i", 4, DIR_S_TO_M),
            ("o", 4, DIR_M_TO_S),
            ("oe", 1, DIR_M_TO_S)
        ]),
        ("cmd", [
            ("i", 1, DIR_S_TO_M),
            ("o", 1, DIR_M_TO_S),
            ("oe", 1, DIR_M_TO_S)
        ]),
        ("clk", 1, DIR_M_TO_S)
    ])
    sdpads.cmd.o.reset = 1
    sdpads.cmd.oe.reset = 1
    return sdpads


class SDPHYCFG(Module):
    def __init__(self):
        # Data timeout
        self.cfgdtimeout = Signal(32)
        # Command timeout
        self.cfgctimeout = Signal(32)
        # Blocksize
        self.cfgblksize = Signal(16)
        # Voltage config: 0: 3.3v, 1: 1.8v
        self.cfgvoltage = Signal()

        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])

        # # #

        mode = Signal(6)

        cfgcases = {} # PHY configuration
        for i in range(4):
            cfgcases[SDCARD_STREAM_CFG_TIMEOUT_DATA_HH + i] = (
                self.cfgdtimeout[24-(8*i):32-(8*i)].eq(sink.data))
            cfgcases[SDCARD_STREAM_CFG_TIMEOUT_CMD_HH + i] = (
                self.cfgctimeout[24-(8*i):32-(8*i)].eq(sink.data))
        for i in range(2):
            cfgcases[SDCARD_STREAM_CFG_BLKSIZE_H + i] = (
                self.cfgblksize[8-(8*i):16-(8*i)].eq(sink.data))
        cfgcases[SDCARD_STREAM_CFG_VOLTAGE] = self.cfgvoltage.eq(sink.data[0])

        self.comb += [
            mode.eq(sink.ctrl[2:8]),
            sink.ready.eq(sink.valid)
        ]

        self.sync += \
            If(sink.valid,
               Case(mode, cfgcases)
            )


class SDPHYRFB(Module):
    def __init__(self, idata, enable):
        self.source = source = stream.Endpoint([("data", 8)])

        # # #

        n = 8//len(idata)
        sel = Signal(max=n)
        data = Signal(8)

        self.submodules.fsm = fsm = ResetInserter()(FSM())
        self.comb += fsm.reset.eq(~enable)

        fsm.act("IDLE",
            NextValue(sel, 0),
            NextState("READSTART")
        )

        fsm.act("READSTART",
            If(idata == 0,
                NextValue(sel, sel + 1),
                NextState("READ")
            )
        )

        fsm.act("READ",
            If(sel == (n-1),
                source.valid.eq(1),
                source.data.eq(Cat(idata, data)),
                NextValue(sel, 0)
            ).Else(
                NextValue(data, Cat(idata, data)),
                NextValue(sel, sel + 1)
            )
        )


class SDPHYCMDR(Module):
    def __init__(self, cfg):
        self.pads = _sdpads()
        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])
        self.source = source = stream.Endpoint([("data", 8), ("ctrl", 8)])

        # # #

        enable = Signal()

        self.submodules.cmdrfb = ClockDomainsRenamer("sd_rx")(SDPHYRFB(self.pads.cmd.i, enable))
        self.submodules.fifo = ClockDomainsRenamer({"write": "sd_rx", "read": "sd_tx"})(
            stream.AsyncFIFO(self.cmdrfb.source.description, 4)
        )
        self.comb += self.cmdrfb.source.connect(self.fifo.sink)

        ctimeout = Signal(32)

        cread = Signal(8)
        ctoread = Signal(8)
        cnt = Signal(8)

        status = Signal(4)
        self.comb += source.ctrl.eq(Cat(SDCARD_STREAM_CMD, status))

        self.submodules.fsm = fsm = FSM()

        fsm.act("IDLE",
            If(sink.valid,
                NextValue(ctimeout, 0),
                NextValue(cread, 0),
                NextValue(ctoread, sink.data),
                NextState("CMD_READSTART")
            )
        )

        fsm.act("CMD_READSTART",
            enable.eq(1),
            self.pads.cmd.oe.eq(0),
            self.pads.clk.eq(1),
            NextValue(ctimeout, ctimeout + 1),
            If(self.fifo.source.valid,
                NextState("CMD_READ")
            ).Elif(ctimeout > cfg.cfgctimeout,
                NextState("TIMEOUT")
            )
        )

        fsm.act("CMD_READ",
            enable.eq(1),
            self.pads.cmd.oe.eq(0),
            self.pads.clk.eq(1),

            source.valid.eq(self.fifo.source.valid),
            source.data.eq(self.fifo.source.data),
            status.eq(SDCARD_STREAM_STATUS_OK),
            source.last.eq(cread == ctoread),
            self.fifo.source.ready.eq(source.ready),
            If(source.valid & source.ready,
                NextValue(cread, cread + 1),
                If(cread == ctoread,
                    If(sink.last,
                        NextState("CMD_CLK8")
                    ).Else(
                        sink.ready.eq(1),
                        NextState("IDLE")
                    )
                )
            )
        )

        fsm.act("CMD_CLK8",
            If(cnt < 7,
                NextValue(cnt, cnt + 1),
                self.pads.clk.eq(1)
            ).Else(
                NextValue(cnt, 0),
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )

        fsm.act("TIMEOUT",
            source.valid.eq(1),
            source.data.eq(0),
            status.eq(SDCARD_STREAM_STATUS_TIMEOUT),
            source.last.eq(1),
            If(source.valid & source.ready,
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )


class SDPHYCMDW(Module):
    def __init__(self):
        self.pads = _sdpads()
        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])

        # # #

        isinit = Signal()
        cntinit = Signal(8)
        cnt = Signal(8)
        wrsel = Signal(3)
        wrtmpdata = Signal(8)

        wrcases = {} # For command write
        for i in range(8):
            wrcases[i] =  self.pads.cmd.o.eq(wrtmpdata[7-i])

        self.submodules.fsm = fsm = FSM()

        fsm.act("IDLE",
            If(sink.valid,
                If(~isinit,
                    NextState("INIT")
                ).Else(
                    self.pads.clk.eq(1),
                    self.pads.cmd.o.eq(sink.data[7]),
                    NextValue(wrtmpdata, sink.data),
                    NextValue(wrsel, 1),
                    NextState("CMD_WRITE")
                )
            )
        )

        fsm.act("INIT",
            # Initialize sdcard with 80 clock cycles
            self.pads.clk.eq(1),
            If(cntinit < 80,
                NextValue(cntinit, cntinit + 1),
                NextValue(self.pads.data.oe, 1),
                NextValue(self.pads.data.o, 0xf)
            ).Else(
                NextValue(cntinit, 0),
                NextValue(isinit, 1),
                NextValue(self.pads.data.oe, 0),
                NextState("IDLE")
            )
        )

        fsm.act("CMD_WRITE",
            Case(wrsel, wrcases),
            NextValue(wrsel, wrsel + 1),
            If(wrsel == 0,
                If(sink.last,
                    self.pads.clk.eq(1),
                    NextState("CMD_CLK8")
                ).Else(
                    sink.ready.eq(1),
                    NextState("IDLE")
                )
            ).Else(
                self.pads.clk.eq(1)
            )
        )

        fsm.act("CMD_CLK8",
            If(cnt < 7,
                NextValue(cnt, cnt + 1),
                self.pads.clk.eq(1)
            ).Else(
                NextValue(cnt, 0),
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )


class SDPHYDATAR(Module):
    def __init__(self, cfg):
        self.pads = _sdpads()
        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])
        self.source = source = stream.Endpoint([("data", 8), ("ctrl", 8)])

        # # #

        enable = Signal()

        self.submodules.datarfb = ClockDomainsRenamer("sd_rx")(SDPHYRFB(self.pads.data.i, enable))
        self.submodules.fifo = ClockDomainsRenamer({"write": "sd_rx", "read": "sd_tx"})(
            stream.AsyncFIFO(self.datarfb.source.description, 4)
        )
        self.comb += self.datarfb.source.connect(self.fifo.sink)

        dtimeout = Signal(32)

        read = Signal(8)
        toread = Signal(8)
        cnt = Signal(8)

        status = Signal(4)
        self.comb += source.ctrl.eq(Cat(SDCARD_STREAM_DATA, status))

        self.submodules.fsm = fsm = FSM()

        fsm.act("IDLE",
            If(sink.valid,
                NextValue(dtimeout, 0),
                NextValue(read, 0),
                # Read 1 block + 8*8 == 64 bits CRC
                NextValue(toread, cfg.cfgblksize + 8),
                NextState("DATA_READSTART")
            )
        )

        fsm.act("DATA_READSTART",
            enable.eq(1),
            self.pads.data.oe.eq(0),
            self.pads.clk.eq(1),
            NextValue(dtimeout, dtimeout + 1),
            If(self.fifo.source.valid,
                NextState("DATA_READ")
            ).Elif(dtimeout > (cfg.cfgdtimeout),
                NextState("TIMEOUT")
            )
        )

        fsm.act("DATA_READ",
            enable.eq(1),
            self.pads.data.oe.eq(0),
            self.pads.clk.eq(1),

            source.valid.eq(self.fifo.source.valid),
            source.data.eq(self.fifo.source.data),
            status.eq(SDCARD_STREAM_STATUS_OK),
            source.last.eq(read == toread),
            self.fifo.source.ready.eq(source.ready),

            If(source.valid & source.ready,
                NextValue(read, read + 1),
                If(read == toread,
                    If(sink.last,
                        NextState("DATA_CLK40")
                    ).Else(
                        sink.ready.eq(1),
                        NextState("IDLE")
                    )
                )
            )
        )

        fsm.act("DATA_CLK40",
            self.pads.data.oe.eq(1),
            self.pads.data.o.eq(0xf),
            If(cnt < 40,
                NextValue(cnt, cnt + 1),
                self.pads.clk.eq(1)
            ).Else(
                NextValue(cnt, 0),
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )

        fsm.act("TIMEOUT",
            source.valid.eq(1),
            source.data.eq(0),
            status.eq(SDCARD_STREAM_STATUS_TIMEOUT),
            source.last.eq(1),
            If(source.valid & source.ready,
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )


class SDPHYDATAW(Module):
    def __init__(self):
        self.pads = _sdpads()
        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])

        # # #

        wrstarted = Signal()

        self.submodules.fsm = fsm = FSM()

        fsm.act("IDLE",
            If(sink.valid,
                self.pads.clk.eq(1),
                self.pads.data.oe.eq(1),
                If(wrstarted,
                    self.pads.data.o.eq(sink.data[4:8]),
                    NextState("DATA_WRITE")
                ).Else(
                    self.pads.data.o.eq(0),
                    NextState("DATA_WRITESTART")
                )
            )
        )

        fsm.act("DATA_WRITESTART",
            self.pads.clk.eq(1),
            self.pads.data.oe.eq(1),
            self.pads.data.o.eq(sink.data[4:8]),
            NextValue(wrstarted, 1),
            NextState("DATA_WRITE")
        )

        fsm.act("DATA_WRITE",
            self.pads.clk.eq(1),
            self.pads.data.oe.eq(1),
            self.pads.data.o.eq(sink.data[0:4]),
            If(sink.last,
                NextState("DATA_WRITESTOP")
            ).Else(
                sink.ready.eq(1),
                NextState("IDLE")
            )
        )

        fsm.act("DATA_WRITESTOP",
            self.pads.clk.eq(1),
            self.pads.data.oe.eq(1),
            self.pads.data.o.eq(0xf),
            NextValue(wrstarted, 0),
            NextState("IDLE") # XXX not implemented
        )


class SDPHYIOS6(Module):
    def __init__(self, sdpads, pads):
        # Data tristate
        self.data_t = TSTriple(4)
        self.specials += self.data_t.get_tristate(pads.data)

        # Cmd tristate
        self.cmd_t = TSTriple()
        self.specials += self.cmd_t.get_tristate(pads.cmd)

        # Clk domain feedback
        if hasattr(pads, "clkfb"):
            self.specials += Instance("IBUFG", i_I=pads.clkfb, o_O=ClockSignal("sd_rx"))

        # Clk output
        self.specials += Instance("ODDR2", p_DDR_ALIGNMENT="NONE",
            p_INIT=1, p_SRTYPE="SYNC",
            i_D0=0, i_D1=sdpads.clk, i_S=0, i_R=0, i_CE=1,
            i_C0=ClockSignal("sd_tx"), i_C1=~ClockSignal("sd_tx"),
            o_Q=pads.clk
        )

        # Cmd input DDR
        self.specials += Instance("IDDR2",
            p_DDR_ALIGNMENT="C1", p_INIT_Q0=0, p_INIT_Q1=0, p_SRTYPE="ASYNC",
            i_C0=ClockSignal("sd_rx"), i_C1=~ClockSignal("sd_rx"),
            i_CE=1, i_S=0, i_R=0,
            i_D=self.cmd_t.i, o_Q0=Signal(), o_Q1=sdpads.cmd.i
        )

        # Data input DDR
        for i in range(4):
            self.specials += Instance("IDDR2",
                p_DDR_ALIGNMENT="C0", p_INIT_Q0=0, p_INIT_Q1=0, p_SRTYPE="ASYNC",
                i_C0=ClockSignal("sd_rx"), i_C1=~ClockSignal("sd_rx"),
                i_CE=1, i_S=0, i_R=0,
                i_D=self.data_t.i[i], o_Q0=Signal(), o_Q1=sdpads.data.i[i]
            )


class SDPHYIOS7(Module):
    def __init__(self, sdpads, pads):
        # Data tristate
        self.data_t = TSTriple(4)
        self.specials += self.data_t.get_tristate(pads.data)

        # Cmd tristate
        self.cmd_t = TSTriple()
        self.specials += self.cmd_t.get_tristate(pads.cmd)

        # Clk domain feedback
        if hasattr(pads, "clkfb"):
            self.specials += Instance("IBUFG", i_I=pads.clkfb, o_O=ClockSignal("sd_rx"))

        # Clk output
        self.specials += Instance("ODDR",
            p_DDR_CLK_EDGE="SAME_EDGE",
            i_C=ClockSignal("sd_tx"), i_CE=1, i_S=0, i_R=0,
            i_D1=0, i_D2=sdpads.clk, o_Q=pads.clk
        )

        # Cmd input DDR
        self.specials += Instance("IDDR",
            p_DDR_CLK_EDGE="SAME_EDGE_PIPELINED",
            i_C=ClockSignal("sd_rx"), i_CE=1, i_S=0, i_R=0,
            i_D=self.cmd_t.i, o_Q1=Signal(), o_Q2=sdpads.cmd.i
        )

        # Data input DDR
        for i in range(4):
            self.specials += Instance("IDDR",
                p_DDR_CLK_EDGE="SAME_EDGE_PIPELINED",
                i_C=ClockSignal("sd_rx"), i_CE=1, i_S=0, i_R=0,
                i_D=self.data_t.i[i], o_Q1=Signal(), o_Q2=sdpads.data.i[i],
            )


class SDPHY(Module, AutoCSR):
    def __init__(self, pads, device):
        self.sink = sink = stream.Endpoint([("data", 8), ("ctrl", 8)])
        self.source = source = stream.Endpoint([("data", 8), ("ctrl", 8)])
        if hasattr(pads, "sel"):
            self.voltage_sel = CSRStorage()
            self.comb += pads.sel.eq(self.voltage_sel.storage)

        # # #

        self.sdpads = sdpads = _sdpads()

        cmddata = Signal()
        rdwr = Signal()
        mode = Signal(6)

        # IOs (device specific)
        if not hasattr(pads, "clkfb"):
            self.comb += [
                ClockSignal("sd_rx").eq(ClockSignal("sd_tx")),
                ResetSignal("sd_rx").eq(ResetSignal("sd_tx"))
            ]
        if hasattr(pads, "cmd_t") and hasattr(pads, "dat_t"):
            # emulator phy
            self.comb += [
                pads.cmd_i.eq(1),
                If(sdpads.cmd.oe, pads.cmd_i.eq(sdpads.cmd.o)),
                sdpads.cmd.i.eq(1),
                If(~pads.cmd_t, sdpads.cmd.i.eq(pads.cmd_i)),

                pads.dat_i.eq(0b1111),
                If(sdpads.data.oe, pads.dat_i.eq(sdpads.data.o)),
                sdpads.data.i.eq(0b1111),
                If(~pads.dat_t[0], sdpads.data.i[0].eq(pads.dat_i[0])),
                If(~pads.dat_t[1], sdpads.data.i[1].eq(pads.dat_i[1])),
                If(~pads.dat_t[2], sdpads.data.i[2].eq(pads.dat_i[2])),
                If(~pads.dat_t[3], sdpads.data.i[3].eq(pads.dat_i[3]))
            ]
        else:
            # real phy
            if device[:3] == "xc6":
                self.submodules.io = SDPHYIOS6(sdpads, pads)
            elif device[:3] == "xc7":
                self.submodules.io = SDPHYIOS7(sdpads, pads)
            else:
                raise NotImplementedError
            self.comb += [
                self.io.cmd_t.oe.eq(sdpads.cmd.oe),
                self.io.cmd_t.o.eq(sdpads.cmd.o),

                self.io.data_t.oe.eq(sdpads.data.oe),
                self.io.data_t.o.eq(sdpads.data.o)
            ]

        # Stream ctrl bits
        self.comb += [
            cmddata.eq(sink.ctrl[0]),
            rdwr.eq(sink.ctrl[1]),
            mode.eq(sink.ctrl[2:8])
        ]

        # PHY submodules
        self.submodules.cfg = SDPHYCFG()
        self.submodules.cmdw = SDPHYCMDW()
        self.submodules.cmdr = SDPHYCMDR(self.cfg)
        self.submodules.dataw = SDPHYDATAW()
        self.submodules.datar = SDPHYDATAR(self.cfg)

        self.comb += \
            If(sink.valid,
                # Configuration mode
                If(mode != SDCARD_STREAM_XFER,
                    sink.connect(self.cfg.sink)
                # Command mode
                ).Elif(cmddata == SDCARD_STREAM_CMD,
                    # Write command
                    If(rdwr == SDCARD_STREAM_WRITE,
                        sink.connect(self.cmdw.sink),
                        self.cmdw.pads.connect(sdpads)
                    # Read response
                    ).Elif(rdwr == SDCARD_STREAM_READ,
                        sink.connect(self.cmdr.sink),
                        self.cmdr.pads.connect(sdpads),
                        self.cmdr.source.connect(source)
                    )
                # Data mode
                ).Elif(cmddata == SDCARD_STREAM_DATA,
                    # Write data
                    If(rdwr == SDCARD_STREAM_WRITE,
                        sink.connect(self.dataw.sink),
                        self.dataw.pads.connect(sdpads)
                    # Read data
                    ).Elif(rdwr == SDCARD_STREAM_READ,
                        sink.connect(self.datar.sink),
                        self.datar.pads.connect(sdpads),
                        self.datar.source.connect(source)
                    )
                )
            )
