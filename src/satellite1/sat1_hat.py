
from dataclasses import dataclass, field, InitVar, replace
from typing import Callable, ClassVar
from pathlib import Path
import time
import random

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:  # ImportError on macOS, etc.
    GPIO = None

import logging

from .components.pcm5122 import PCM5122, PCM5122Config, PCM5122GPIOPin
from .components.xmos_device_cntrl import (
    DeviceCntrlConfig,
    XMOSDeviceCntrl,
    DeviceCntrlStatusRegister as StatusRegister,
    DFU_SERVICER,
    MAIN_SERVICER,
    AUDIO_CFG_SERVICER,
    SPI_ECHO_SERVICER,
    LED_RING_SERVICER,
)
from pydantic import BaseModel, ConfigDict,Field, computed_field

log = logging.getLogger(__name__)


class LEDRing:
    """Manages WS2812 LED ring with brightness control and individual LED state.

    Maintains internal state of all LED colors and applies brightness scaling
    when committing changes to hardware. State is persisted to disk to survive
    across CLI invocations.
    """

    _STATE_FILE = Path("/tmp/sat1_led_ring_state.json")

    def __init__(self, xmos: "XMOS", num_leds: int = 24):
        self._xmos = xmos
        self._num_leds = num_leds
        self._brightness: float = 1.0  # 0.0 to 1.0
        # Internal buffer: list of (r, g, b) tuples
        self._leds: list[tuple[int, int, int]] = [(0, 0, 0)] * num_leds
        self._load_state()

    @property
    def num_leds(self) -> int:
        """Number of LEDs in the ring."""
        return self._num_leds

    @property
    def brightness(self) -> float:
        """Current brightness level (0.0 to 1.0)."""
        return self._brightness

    @brightness.setter
    def brightness(self, value: float) -> None:
        """Set brightness level (0.0 to 1.0)."""
        if not 0.0 <= value <= 1.0:
            raise ValueError("Brightness must be between 0.0 and 1.0")
        self._brightness = value

    def set_brightness(self, value: float) -> "LEDRing":
        """Set brightness and return self for chaining."""
        self.brightness = value
        return self

    def set_led(self, index: int, r: int, g: int, b: int) -> "LEDRing":
        """Set color of a single LED (does not commit to hardware).

        Args:
            index: LED index (0 to num_leds-1)
            r, g, b: Color values (0-255)

        Returns:
            self for method chaining
        """
        if not 0 <= index < self._num_leds:
            raise ValueError(f"LED index must be 0-{self._num_leds - 1}")
        if not all(0 <= c <= 255 for c in (r, g, b)):
            raise ValueError("RGB values must be 0-255")
        self._leds[index] = (r, g, b)
        return self

    def set_led_on(self, index: int, r: int = 255, g: int = 255, b: int = 255) -> "LEDRing":
        """Turn on a single LED with optional color (default white)."""
        return self.set_led(index, r, g, b)

    def set_led_off(self, index: int) -> "LEDRing":
        """Turn off a single LED."""
        return self.set_led(index, 0, 0, 0)

    def toggle_led(self, index: int, r: int = 255, g: int = 255, b: int = 255) -> "LEDRing":
        """Toggle a single LED on/off.

        Args:
            index: LED index
            r, g, b: Color to use when turning on (default white)

        Returns:
            self for method chaining
        """
        if self._leds[index] == (0, 0, 0):
            return self.set_led(index, r, g, b)
        else:
            return self.set_led_off(index)

    def set_all(self, r: int, g: int, b: int) -> "LEDRing":
        """Set all LEDs to the same color (does not commit)."""
        self._leds = [(r, g, b)] * self._num_leds
        return self

    def clear(self) -> "LEDRing":
        """Turn off all LEDs (does not commit)."""
        return self.set_all(0, 0, 0)

    def _apply_brightness(self, r: int, g: int, b: int) -> tuple[int, int, int]:
        """Apply brightness scaling to RGB values."""
        return (
            int(r * self._brightness),
            int(g * self._brightness),
            int(b * self._brightness),
        )

    def _load_state(self) -> None:
        """Load LED state from disk if available."""
        import json
        try:
            if self._STATE_FILE.exists():
                data = json.loads(self._STATE_FILE.read_text())
                self._leds = [tuple(led) for led in data.get("leds", [])]
                if len(self._leds) != self._num_leds:
                    self._leds = [(0, 0, 0)] * self._num_leds
                self._brightness = data.get("brightness", 1.0)
        except Exception:
            log.debug("Could not load LED state, using defaults")

    def _save_state(self) -> None:
        """Persist LED state to disk."""
        import json
        try:
            data = {"leds": self._leds, "brightness": self._brightness}
            self._STATE_FILE.write_text(json.dumps(data))
        except Exception as e:
            log.warning("Could not save LED state: %s", e)

    def commit(self) -> bool:
        """Send current LED state to hardware with brightness applied.

        Returns:
            True if successful, False otherwise.
        """
        # Build 72-byte buffer with brightness-scaled values
        # WS2812 expects GRB format, not RGB
        grb_bytes = bytearray()
        for r, g, b in self._leds:
            sr, sg, sb = self._apply_brightness(r, g, b)
            grb_bytes.extend([sg, sr, sb])  # GRB order

        ok = self._xmos.set_led_ring(bytes(grb_bytes))
        if ok:
            self._save_state()
        return ok

    def get_led(self, index: int) -> tuple[int, int, int]:
        """Get the color of a single LED (unscaled)."""
        return self._leds[index]

    def get_leds(self) -> list[tuple[int, int, int]]:
        """Get all LED colors (unscaled)."""
        return self._leds.copy()


def _func_name(code: int) -> str:
    # Helpful when debugging
    names = {
        GPIO.IN: "IN",
        GPIO.OUT: "OUT",
        getattr(GPIO, "SPI", -1): "SPI",
        getattr(GPIO, "I2C", -1): "I2C",
        getattr(GPIO, "HARD_PWM", -1): "HARD_PWM",
        getattr(GPIO, "SERIAL", -1): "SERIAL",
        getattr(GPIO, "UNKNOWN", -1): "UNKNOWN",
    }
    return names.get(code, str(code))


class XMOS():
    CNTRL_STATUS_LENGTH = 4

    def __init__(self) -> None:
        cntrl_cfg = DeviceCntrlConfig(
            bus = 0,
            dev = 0,
            max_speed_hz = 8_000_000,
            mode = 3,
            bits_per_word = 8,
            status_reg_len = XMOS.CNTRL_STATUS_LENGTH
        )
        
        self._cntrl = XMOSDeviceCntrl(cntrl_cfg)
        self._reset_bcm_pin = 5 # RPi Header 29
        self._status = None
        self._firmware: str | None = None
        self._led_ring: LEDRing | None = None
    
    def setup(self, init_spi:bool = True) -> None:
        self._cntrl.open()
        
    def read_firmware(self) -> str | None:
        ok, data = self._cntrl.send_cmd( DFU_SERVICER.CMD_GET_VERSION )
        if ok and len(data) == 5:
            self._firmware = self._fw_from_bytes(data)
            return self._firmware
        return None
    
    def read_status(self) -> StatusRegister | None :
        ok, data = self._cntrl.send_cmd( MAIN_SERVICER.CMD_NO_OP )
        if ok and data is not None and len(data) == XMOS.CNTRL_STATUS_LENGTH:
            self._status = data
            return self._status
        return None
    
    def reset_xmos(self) -> bool:
        self._ensure_gpio_setup()
        GPIO.output(self._reset_bcm_pin, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(self._reset_bcm_pin, GPIO.LOW)
        time.sleep(0.1)
        self._status = "DETACHED"

    def subscribe_status_changes(cb: Callable[[StatusRegister],None] ) -> None:
        pass
    
    def _poll(self) -> None :
        if self._status == "DETACHED":
            if self.read_firmware():
                self._state = "CNTRL_MODE"
        elif self._status == "CNTRL_MODE":
            self.read_status()
        
    def set_led_ring(self, rgb_data: bytes, brightness: float = 1.0) -> bool:
        """Set LED ring colors via WS2812.

        Args:
            rgb_data: 72 bytes (3 × 24 LEDs), RGB values for each LED.
                      Format: [R0, G0, B0, R1, G1, B1, ..., R23, G23, B23]
            brightness: Brightness level (0.0-1.0, default=1.0)

        Returns:
            True if successful, False otherwise.
        """
        if len(rgb_data) != 72:
            raise ValueError(f"Expected 72 bytes (3×24 LEDs), got {len(rgb_data)}")
        if brightness != 1.0:
            rgb_data = self._apply_brightness_to_bytes(rgb_data, brightness)
        ok, _ = self._cntrl.send_cmd(LED_RING_SERVICER.CMD_WRITE_RAW, rgb_data)
        return ok

    def _apply_brightness_to_bytes(self, rgb_data: bytes, brightness: float) -> bytes:
        """Apply brightness scaling to raw RGB bytes."""
        result = bytearray()
        for i in range(0, len(rgb_data), 3):
            r, g, b = rgb_data[i], rgb_data[i+1], rgb_data[i+2]
            result.extend([
                int(r * brightness),
                int(g * brightness),
                int(b * brightness)
            ])
        return bytes(result)

    def set_led_ring_color(self, r: int, g: int, b: int, brightness: float = 1.0) -> bool:
        """Set all LEDs in the ring to the same color.

        Args:
            r: Red value (0-255)
            g: Green value (0-255)
            b: Blue value (0-255)
            brightness: Brightness level (0.0-1.0, default=1.0)

        Returns:
            True if successful, False otherwise.
        """
        # WS2812 expects GRB format, not RGB
        grb_data = bytes([g, r, b] * LED_RING_SERVICER.NUM_LEDS)
        return self.set_led_ring(grb_data, brightness)

    def set_led_ring_off(self) -> bool:
        """Turn off all LEDs in the ring."""
        return self.set_led_ring_color(0, 0, 0)

    def led_ring(self) -> LEDRing:
        """Get an LEDRing controller for brightness and individual LED control.

        Returns:
            LEDRing instance that can manage LED state and brightness.
            The instance is cached, so state persists between calls.

        Example:
            ring = xmos.led_ring()
            ring.set_brightness(0.5).set_led(0, 255, 0, 0).commit()  # 50% bright red LED 0
        """
        if self._led_ring is None:
            self._led_ring = LEDRing(self, LED_RING_SERVICER.NUM_LEDS)
        return self._led_ring

    def _ensure_gpio_setup(self) -> None:
        """Idempotent, strict, and self-validating setup for a BCM pin."""
        # 1) Enforce BCM numbering; fail fast if something else chose BOARD
        mode = GPIO.getmode()
        if mode is None:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            log.debug("GPIO.setmode(BCM)")
        elif mode != GPIO.BCM:
            raise RuntimeError("GPIO mode is BOARD; expected BCM (pin value is BCM index)")

        # 2) Always (re)configure the pin as OUT (cheap and safe)
        GPIO.setup(self._reset_bcm_pin, GPIO.OUT, initial=GPIO.LOW)

        # 3) Validate immediately
        func = GPIO.gpio_function(self._reset_bcm_pin)
        if func != GPIO.OUT:
            raise RuntimeError(
                f"Failed to set GPIO {self._reset_bcm_pin} as OUT (func={_func_name(func)})"
            )
        log.debug("GPIO %d configured as OUT", self._reset_bcm_pin)

    
    def set_flash_mode(self) -> None:
        self._ensure_gpio_setup()
        log.info( f"Enabling flashing mode (XMOS in reset state)" )
        GPIO.output(self._reset_bcm_pin, GPIO.HIGH)

    def unset_flash_mode(self) -> None:
        self._ensure_gpio_setup()
        log.info( f"Disabling flashing mode (re-init XMOS)" )
        GPIO.output(self._reset_bcm_pin, GPIO.LOW)
        
    
    def flash_firmware(self, img: Path, verify: bool = False) -> None:
        from .components.flashrom_wrapper import Flashrom
        self.set_flash_mode()
        time.sleep(.5)
        flasher = Flashrom.for_rpi_w25q64jv(timeout=600)
        if not flasher.confirm_chip():
            raise SystemExit("Flash chip not found or not accessible")

        if not img.exists():
            raise ValueError(f"Image-file not found {img}")
        
        log.info(f"Starting flashing of {img}")
        flasher.write_image(img, verify=verify)

        self.unset_flash_mode()
        self._status = "DETACHED"

    def run_spi_echo_test(self):
        for step in range(10):
            rnd_bytes = random.randbytes(128)
            ok, data = self._cntrl.send_cmd( SPI_ECHO_SERVICER.CMD_SET, rnd_bytes)
            if not ok:
                print( "sending failed")
                continue
            ok, data = self._cntrl.send_cmd(SPI_ECHO_SERVICER.CMD_GET)
            if not ok or data != rnd_bytes:
                print( f"step {step} failed:\n  sent: {rnd_bytes}\n  recv: {data}")
                continue
            
            print( f"step: {step} passed")    
    
    def set_mic_left_output(self, out_select:int) -> None:
        if 0 <= out_select <= 7 :
             ok, data = self._cntrl.send_cmd( AUDIO_CFG_SERVICER.CMD_MIC_LEFT_SELECT, [out_select] )
    
    def set_mic_right_output(self, out_select:int) -> None:
        if 0 <= out_select <= 7 :
             ok, data = self._cntrl.send_cmd( AUDIO_CFG_SERVICER.CMD_MIC_RIGHT_SELECT, [out_select] )

    def _prerelease_str(idx: int) -> str:
        return {1: "alpha", 2: "beta", 3: "rc", 4: "dev"}.get(idx, "")
    
    def _fw_from_bytes(self, data: bytes):
        if len(data) != 5:
            raise ValueError(f"expected 5 bytes, got {len(data)}")
        maj, mi, pa, pre, pre_n = data
        pre_s = "-" + XMOS._prerelease_str(pre) if pre else ""
        pre_i = f".{pre_n}" if pre and pre_n else ""
        return f"v{maj}.{mi}.{pa}{pre_s}{pre_i}"       



def init() -> None:
    pass

