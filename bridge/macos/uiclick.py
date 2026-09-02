#!/usr/bin/env python3
"""Real mouse click at screen coordinates, in points.

    uv run --with pyobjc-framework-Quartz python uiclick.py 1700 209

System Events' `click at {x, y}` silently does nothing on Chrome's web content
(the chrome:// pages in particular), so UI automation there needs genuine
CoreGraphics events instead.
"""

import sys
import time

from Quartz import (
    CGEventCreateMouseEvent,
    CGEventPost,
    kCGEventLeftMouseDown,
    kCGEventLeftMouseUp,
    kCGEventMouseMoved,
    kCGHIDEventTap,
    kCGMouseButtonLeft,
)


def click(x: float, y: float) -> None:
    pos = (x, y)
    # Move first: some UIs only arm a control on hover before the press lands.
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventMouseMoved, pos, kCGMouseButtonLeft))
    time.sleep(0.15)
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pos, kCGMouseButtonLeft))
    time.sleep(0.06)
    CGEventPost(kCGHIDEventTap, CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pos, kCGMouseButtonLeft))


def probe(x: int, y: int) -> tuple[int, int, int]:
    """RGB of one screen pixel, for reading a control's state before touching it.

    Toggling blind is not idempotent: a second run would switch Developer mode
    back off. Sampling the toggle's colour is the cheap way to tell on from off
    when the page itself is unreadable.
    """
    import subprocess
    import tempfile

    from Quartz import (
        CGImageGetDataProvider,
        CGDataProviderCopyData,
        CGImageGetBytesPerRow,
        CGImageGetBitsPerPixel,
    )
    from Quartz.CoreGraphics import CGDisplayCreateImageForRect, CGMainDisplayID, CGRectMake

    img = CGDisplayCreateImageForRect(CGMainDisplayID(), CGRectMake(x, y, 1, 1))
    data = CGDataProviderCopyData(CGImageGetDataProvider(img))
    b = bytes(data[:4])
    # BGRA on this platform
    return (b[2], b[1], b[0])


if __name__ == "__main__":
    if sys.argv[1] == "probe":
        r, g, bl = probe(int(sys.argv[2]), int(sys.argv[3]))
        print(f"{r},{g},{bl}")
    else:
        click(float(sys.argv[1]), float(sys.argv[2]))
        print(f"clicked {sys.argv[1]},{sys.argv[2]}")
