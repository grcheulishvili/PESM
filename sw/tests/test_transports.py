"""Hardware transports against fake driver modules (no hardware needed):
checks SPI mode, CS framing, MODE/RST_N GPIO polarity and sequencing."""
import sys
import types

from pesm import hostproto as hp
from pesm.programmer import FtdiTransport, Programmer, SpidevTransport


class _FakeGpio:
    def __init__(self, log):
        self.log = log

    def set_direction(self, pins, direction):
        self.log.append(("dir", pins, direction))

    def write(self, value):
        self.log.append(("gpio", value))


class _FakePort:
    def __init__(self, log):
        self.log = log

    def exchange(self, out, duplex=False):
        assert duplex
        self.log.append(("spi", bytes(out)))
        return bytes([0x40] + [0] * (len(out) - 1))


def test_ftdi_transport(monkeypatch):
    log = []

    class FakeCtrl:
        def __init__(self, cs_count):
            assert cs_count == 1

        def configure(self, url):
            log.append(("url", url))

        def get_port(self, cs, freq, mode):
            assert (cs, mode) == (0, 0) and freq <= 6.25e6
            return _FakePort(log)

        def get_gpio(self):
            return _FakeGpio(log)

        def terminate(self):
            log.append(("close",))

    mod = types.ModuleType("pyftdi.spi")
    mod.SpiController = FakeCtrl
    monkeypatch.setitem(sys.modules, "pyftdi", types.ModuleType("pyftdi"))
    monkeypatch.setitem(sys.modules, "pyftdi.spi", mod)
    with FtdiTransport("ftdi://ftdi:232h/1", freq=1e6) as t:
        t.delay = lambda s: None
        p = Programmer(t)
        p.boot()
        p.run()
        p.status()
    assert ("dir", 0x30, 0x30) in log                 # AD4 MODE, AD5 RST_N outputs
    gpio = [v for k, v, *_ in [e + (None,) for e in log] if k == "gpio"]
    assert gpio[0] == 0x30                            # power-on: BOOT, not in reset
    assert gpio[-1] == 0x20                           # after run(): MODE low, RST_N high
    spi = [e[1] for e in log if e[0] == "spi"]
    assert spi[-1] == bytes(hp.frame_status())
    assert log[-1] == ("close",)


def test_spidev_transport_gpiod_v2(monkeypatch):
    log = []

    class FakeSpi:
        def open(self, bus, dev):
            log.append(("open", bus, dev))

        def xfer2(self, data):
            log.append(("spi", list(data)))
            return [0] * len(data)

        def close(self):
            log.append(("close",))

    spidev = types.ModuleType("spidev")
    spidev.SpiDev = FakeSpi

    class Value:
        ACTIVE, INACTIVE = 1, 0

    class Direction:
        OUTPUT = "out"

    class Req:
        def set_value(self, off, v):
            log.append(("line", off, v))

    gpiod = types.ModuleType("gpiod")
    gpiod.request_lines = lambda path, consumer, config: (log.append(("req", path, list(config))), Req())[1]
    gpiod.LineSettings = lambda direction, output_value: (direction, output_value)
    line = types.ModuleType("gpiod.line")
    line.Direction, line.Value = Direction, Value
    monkeypatch.setitem(sys.modules, "spidev", spidev)
    monkeypatch.setitem(sys.modules, "gpiod", gpiod)
    monkeypatch.setitem(sys.modules, "gpiod.line", line)
    t = SpidevTransport(0, 0, 2_000_000, "gpiochip0:17", "gpiochip0:27")
    t.delay = lambda s: None
    p = Programmer(t)
    p.boot()
    p.write_imem([0x1234], 5)
    p.run()
    t.close()
    assert ("req", "/dev/gpiochip0", [17]) in log and ("req", "/dev/gpiochip0", [27]) in log
    assert ("line", 17, 1) in log and log[-2] == ("line", 17, 0)
    assert ("spi", [0x05, 0x12, 0x34]) in log
