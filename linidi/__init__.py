"""LiniDi -- a control surface for the miniDSP Flex 8 on Linux.

The program talks to the device directly over USB HID: no daemon, no vendor
binaries, nothing between this and the hardware. The modules divide along the
same seam the device does.

    protocol   framing, command codes and the two USB transports
    flash      the stored preset, read back out of the device's flash
    native     the device layer -- what a channel or a filter means
    core       device-facing logic, address maps, the DSP arithmetic
    gui        everything the user sees

Nothing here imports Qt except gui, so the lower layers can be driven, tested
and read without a display.

License: Apache-2.0
"""

__version__ = "1.0.0"
