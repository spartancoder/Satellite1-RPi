# src/satellite1/cli/cli_xmos.py
from __future__ import annotations

import argparse
import logging
from pathlib import Path
import time

from ..sat1_hat import XMOS, LED_RING_SERVICER, LEDRing

log = logging.getLogger(__name__)


def _fmt_status(val) -> str:
    """Best-effort human-readable status."""
    try:
        # If it looks like your DeviceCntrlStatusRegister dataclass
        ds = getattr(val, "device_status", None)
        pa = getattr(val, "gpio_port_a", None)
        pb = getattr(val, "gpio_port_b", None)
        if ds is not None and pa is not None and pb is not None:
            return f"device_status=0x{ds:02X} gpio_a=0x{pa:02X} gpio_b=0x{pb:02X}"
    except Exception:
        pass
    # Fallbacks
    if isinstance(val, (bytes, bytearray)):
        return " ".join(f"{b:02X}" for b in val)
    return repr(val)


def _handle(args: argparse.Namespace) -> int:
    """Dispatch XMOS subcommands."""
    xmos = XMOS()

    # Non-SPI commands
    if args.cmd == "enable-flashing":
       xmos.set_flash_mode()
       return 0
    
    if args.cmd == "disable-flashing":
       xmos.unset_flash_mode()
       return 0
    
    if args.cmd == "reset":
        ok = xmos.reset_xmos()
        log.info("Reset: %s", ok)
        print(ok)
        return 0 if ok else 1

    if args.cmd == "flash-firmware":
        ok = xmos.flash_firmware(args.img, verify=args.verify)
        log.info("Flashed %s (verify=%s): %s", args.img, args.verify, ok)
        print(ok)
        return 0 if ok else 1
    
    
    # SPI Commands
    log.info("Init SPI")
    ok = xmos.setup()
    if args.cmd == "setup":
        log.info("XMOS setup: %s", ok)
        print(ok)
        return 0

    if args.cmd == "read-firmware":
        fw = xmos.read_firmware()
        log.info("Firmware: %s", fw)
        print(fw)
        return 0 if fw is not None else 1

    if args.cmd == "read-status":
        st = xmos.read_status()
        log.info("Status: %s", _fmt_status(st) if st is not None else "None")
        print(_fmt_status(st) if st is not None else None)
        return 0 if st is not None else 1

    if args.cmd == "set-mic-output":
        log.info(f"Set mic channels to {args.left} and {args.right}")
        xmos.set_mic_left_output( args.left )
        time.sleep(2)
        xmos.set_mic_right_output( args.right )
        return 0
    
    if args.cmd == "run-spi-test":
        log.info(f"Starting SPI Test")
        xmos.run_spi_echo_test()
        return 0

    if args.cmd == "set-led-ring":
        r, g, b = args.r, args.g, args.b
        brightness = getattr(args, 'brightness', 1.0)
        ring = xmos.led_ring()
        ring.set_brightness(brightness).set_all(r, g, b)
        ok = ring.commit()
        log.info("Set LED ring to RGB(%d, %d, %d) @ %.0f%%: %s", r, g, b, brightness * 100, "OK" if ok else "FAILED")
        return 0 if ok else 1

    if args.cmd == "led-ring-off":
        ok = xmos.set_led_ring_off()
        log.info("LED ring off: %s", "OK" if ok else "FAILED")
        return 0 if ok else 1

    if args.cmd == "set-led":
        ring = xmos.led_ring()
        ring.set_brightness(args.brightness).set_led(args.index, args.r, args.g, args.b)
        ok = ring.commit()
        log.info("Set LED %d to RGB(%d, %d, %d) @ %.0f%%: %s", args.index, args.r, args.g, args.b, args.brightness * 100, "OK" if ok else "FAILED")
        return 0 if ok else 1

    if args.cmd == "toggle-led":
        ring = xmos.led_ring()
        ring.set_brightness(args.brightness).toggle_led(args.index, args.r, args.g, args.b)
        ok = ring.commit()
        state = "ON" if ring.get_led(args.index) != (0, 0, 0) else "OFF"
        log.info("Toggled LED %d %s @ %.0f%%: %s", args.index, state, args.brightness * 100, "OK" if ok else "FAILED")
        return 0 if ok else 1
        
    return 2


def attach_to_parser(parser: argparse.ArgumentParser) -> None:
    """
    Attach ALL XMOS commands to `parser` (standalone style).
    Sets `_handler` so the top-level can just call it.
    """
    sp = parser.add_subparsers(dest="cmd", required=True)
    sp.add_parser("setup", help="Initialise SPI/GPIO")
    sp.add_parser("read-firmware", help="Read firmware version")
    sp.add_parser("read-status", help="Read status register")
    sp.add_parser("reset", help="Toggle reset pin")
    sp.add_parser("enable-flashing", help="Put XMOS in reset (flashing mode)")
    sp.add_parser("disable-flashing", help="Exit XMOS reset mode")
    sp.add_parser("run-spi-test", help="Running the SPI echo test")

    led = sp.add_parser("set-led-ring", help="Set LED ring color (RGB)")
    led.add_argument("r", type=int, help="Red (0-255)")
    led.add_argument("g", type=int, help="Green (0-255)")
    led.add_argument("b", type=int, help="Blue (0-255)")
    led.add_argument("--brightness", "-B", type=float, default=1.0, help="Brightness (0.0-1.0, default=1.0)")

    sp.add_parser("led-ring-off", help="Turn off LED ring")

    setled = sp.add_parser("set-led", help="Set individual LED color")
    setled.add_argument("index", type=int, help="LED index (0-23)")
    setled.add_argument("r", type=int, help="Red (0-255)")
    setled.add_argument("g", type=int, help="Green (0-255)")
    setled.add_argument("b", type=int, help="Blue (0-255)")
    setled.add_argument("--brightness", "-B", type=float, default=1.0, help="Brightness (0.0-1.0, default=1.0)")

    toggle = sp.add_parser("toggle-led", help="Toggle individual LED on/off")
    toggle.add_argument("index", type=int, help="LED index (0-23)")
    toggle.add_argument("r", type=int, nargs="?", default=255, help="Red when on (0-255, default=255)")
    toggle.add_argument("g", type=int, nargs="?", default=255, help="Green when on (0-255, default=255)")
    toggle.add_argument("b", type=int, nargs="?", default=255, help="Blue when on (0-255, default=255)")
    toggle.add_argument("--brightness", "-B", type=float, default=1.0, help="Brightness (0.0-1.0, default=1.0)")
    
    mo = sp.add_parser("set-mic-output", help="Set the output channels of the i2s microphone")
    mo.add_argument("left", type=int )
    mo.add_argument("right", type=int )

    f = sp.add_parser("flash-firmware", help="Flash factory image")
    f.add_argument("img", type=Path)
    f.add_argument("--verify", action="store_true", help="Verify after flashing")

    parser.set_defaults(_handler=_handle)


def register(parent: argparse._SubParsersAction, *, name: str = "xmos", help: str = "XMOS controls"):
    """
    Register the XMOS component under `parent` subparsers (hub style).
    """
    child = parent.add_parser(name, help=help)
    attach_to_parser(child)
    return child


# -------- Optional: standalone entrypoint (sat1-xmos) --------

def _configure_logging(verbosity: int) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname).1s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    log.debug("Logging configured at %s", logging.getLevelName(level))


def xmos_main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sat1-xmos", description="Satellite1 XMOS tools")
    # Keep --config at the root for symmetry with other CLIs even if XMOS ignores it today
    p.add_argument("--config", type=Path, default=None, help="TOML config (unused for XMOS for now)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity (-v, -vv)")
    attach_to_parser(p)
    args = p.parse_args(argv)
    _configure_logging(args.verbose)
    log.debug("Args: %s", vars(args))
    return int(args._handler(args) or 0)
