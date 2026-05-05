"""Opt this Qt window into KWin's Blur compositor effect.

KWin's Blur effect does **not** automatically apply to layer-shell surfaces
(nor to translucent windows in general) — a client has to opt in via the
``org_kde_kwin_blur`` Wayland protocol, or set the
``_KDE_NET_WM_BLUR_BEHIND_REGION`` atom on X11. ``KWindowEffects::enableBlur
Behind`` in KWindowSystem speaks both, but PySide6 ships no Python bindings,
so we reach the symbol via ctypes against ``libKF6WindowSystem.so.6`` and
extract the underlying C++ pointers with ``shiboken6.getCppPointer``.

Best-effort: if the library is missing or anything fails we return False and
the OSD degrades to plain translucency.
"""
from __future__ import annotations

import ctypes
import logging

log = logging.getLogger(__name__)

# KWindowEffects::enableBlurBehind(QWindow*, bool, const QRegion&)
_ENABLE_BLUR_SYMBOL = "_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion"
_LIBNAME = "libKF6WindowSystem.so.6"


def enable_blur_behind(window, region=None) -> bool:
    """Ask KWin to blur whatever is behind ``window``. Returns True on
    success, False on any failure (logged at debug level).

    ``region`` is an optional ``QRegion`` describing the area to blur, in
    window-local coordinates. ``None`` means "blur the whole window
    rectangle" — which leaks blur past rounded corners. Pass a region
    matching the visible shape (see ``rounded_rect_region``) to keep
    KWin's blur strictly under the pill.
    """
    try:
        from PySide6.QtGui import QRegion
        from shiboken6 import getCppPointer
    except Exception as exc:  # pragma: no cover — PySide6 is a hard dep
        log.debug("PySide6/shiboken6 unavailable for blur hint: %s", exc)
        return False

    try:
        lib = ctypes.CDLL(_LIBNAME)
    except OSError as exc:
        log.debug("%s not available, OSD blur disabled: %s", _LIBNAME, exc)
        return False

    try:
        fn = getattr(lib, _ENABLE_BLUR_SYMBOL)
    except AttributeError:
        log.debug("KWindowEffects::enableBlurBehind symbol not found in %s",
                  _LIBNAME)
        return False

    fn.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p)
    fn.restype = None

    if region is None:
        region = QRegion()
    try:
        win_ptr = getCppPointer(window)[0]
        region_ptr = getCppPointer(region)[0]
    except Exception as exc:
        log.debug("Could not extract Qt cpp pointers for blur: %s", exc)
        return False

    fn(ctypes.c_void_p(win_ptr), True, ctypes.c_void_p(region_ptr))
    return True


def rounded_rect_region(width: int, height: int, radius: int):
    """Build a ``QRegion`` covering a rounded rect of size ``width`` x
    ``height`` with corner ``radius``, in window-local coordinates. Used to
    shape KWin's blur region to match the OSD pill's rounded body so blur
    doesn't leak past the corners."""
    from PySide6.QtGui import QPainterPath, QRegion

    path = QPainterPath()
    path.addRoundedRect(0, 0, width, height, radius, radius)
    # toFillPolygon discretises the rounded arcs into a polygon; KWin
    # downsamples heavily for blur so the polygonal approximation isn't
    # visible at the few-pixel level it produces.
    return QRegion(path.toFillPolygon().toPolygon())
