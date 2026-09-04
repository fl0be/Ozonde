#!/usr/bin/env python3
"""Signs of life on a terminal, for the stretches where nothing prints.

A large tree spends a long time with nothing to show: generating half a
million entries, then emulating a build over them. All of it goes to stderr
and only to a terminal, so a run piped to a file collects results rather than
carriage returns, and the numbers stay copy-pasteable.

Nothing here is measured or reported -- it exists purely so a run that is
working does not look like a run that has hung.
"""
import shutil
import sys

# Below this, a loop finishes before a percentage could be read.
MIN_TO_SHOW = 20000
STATUS_WIDTH = 52


def _width():
    """How wide the status line may be: what it wants, or what there is.

    A line wider than the terminal wraps, and a wrapped line cannot overwrite
    itself -- the carriage return lands at the start of the last physical row
    and everything above it stays, one row per redraw. Asked each time rather
    than once, since a window is resized while a run is going.
    """
    return min(STATUS_WIDTH, shutil.get_terminal_size().columns)


def status(text):
    if sys.stderr.isatty():
        # Cut as well as padded. clear() wipes exactly this many columns, so
        # anything wider outlives being cleared and stays on the line for the
        # rest of the run.
        width = _width()
        sys.stderr.write("\r  " + text[:width - 2].ljust(width - 2))
        sys.stderr.flush()


def clear():
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * _width() + "\r")
        sys.stderr.flush()


def ticking(label, total):
    """range(total), showing how far along it is when that is worth showing."""
    if total < MIN_TO_SHOW or not sys.stderr.isatty():
        return range(total)

    def counted():
        step = max(1, total // 100)   # one redraw per percent, no more
        try:
            for i in range(total):
                if i % step == 0:
                    status("%-28s %3d%%" % (label, 100 * i // total))
                yield i
        finally:
            # Also on the way out: an interrupt through here would otherwise
            # leave the line standing, with no newline after it.
            clear()

    return counted()
