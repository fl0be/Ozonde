#!/usr/bin/env python3
"""The flight recorder: what the build says, and how it dies.

Both halves, like the real thing -- the voice recorder for anything the build
tried to tell someone, the data recorder for the moment it stopped. Nothing
here is invented: every entry point below belongs to the target, and this
file only listens, because left alone they either say nothing a sandbox can
hear or hang the run outright.

Two build-agnostic things that belong together:

  * output -- a sysmodule has no stdout (newlib stdio is not even linked), so
    svcOutputDebugString is the only way a build can say anything. It is caught
    at the syscall level, which means it needs no symbol and works on any ELF.

  * aborts -- AMS_ABORT, AMS_ABORT_UNLESS and R_ABORT_UNLESS. Catching these is
    what turns "the run died somewhere" into "this check failed, with this
    Result". Nothing here is specific to fs.mitm; it is the shape of
    ams::diag, so it applies to any Atmosphere build.

Release builds compile the assertion strings out (AMS_ENABLE_DETAILED_ASSERTIONS
follows AMS_BUILD_FOR_DEBUGGING), so expr/func/file/line are usually empty and
the caller address is the identifying detail. Build with detailed assertions and
the same hooks print the source location too. AMS_ASSERT is absent entirely from
a release build -- it compiles to a no-op -- so there is nothing to catch.
"""


class GuestAbort(RuntimeError):
    """The target reached AMS_ABORT / a failed AMS_ABORT_UNLESS."""


# Horizon's debug print: svcOutputDebugString(const char *str, u64 len).
SVC_OUTPUT_DEBUG_STRING = 0x27

# svcBreak(reason, address, size) -- how libnx aborts, and the only abort route
# that is not ams::diag's. diagAbortWithResult puts the Result on its stack and
# passes the address, so a 4-byte payload is one.
SVC_BREAK = 0x26
BREAK_RESULT_SIZE = 4

# How every abort actually dies. AbortWithValue does not use svcBreak -- it
# provokes a deliberate data abort by storing a magic value to a magic address,
# with the Result left in x0.
STD_ABORT_MAGIC_ADDRESS = 0x8
STD_ABORT_MAGIC_VALUE = 0xA55AF00DDEADCAFE

# The abort funnel, outermost first. Only the 4-arg AbortImpl is exported; the
# overloads taking a Result or a format string are inlined into their callers
# and reach VAbortImpl directly, so hooking AbortImpl alone would miss every
# R_ABORT_UNLESS. InvokeAbortObserver is where they all converge with a
# filled-in AbortInfo.
ABORT_SYMBOLS = (
    ("ams::diag::AbortImpl(char const*, char const*, char const*, int)", "args4"),

    # Exported only by a debug build; in release it is inlined into its callers.
    # Worth hooking directly because debug builds also inline
    # InvokeAbortObserver away, which would otherwise leave only the far less
    # informative AbortWithValue report for a formatted AMS_ABORT.
    ("ams::diag::AbortImpl(char const*, char const*, char const*, int, char const*, ...)",
     "args4fmt"),
    ("ams::impl::UnexpectedDefaultImpl(char const*, char const*, int)", "args3"),
    ("ams::diag::impl::InvokeAbortObserver(ams::diag::AbortInfo const&)", "abort_info"),
    ("ams::diag::(anonymous namespace)::AbortWithValue(unsigned long)", "value"),

    # The route a failed R_ABORT_UNLESS can take instead: it carries the Result
    # that failed, in X0, which is what the "value" kind already reads. Hooked
    # because nothing else here catches it -- it is outside the module prefix
    # but reaches a syscall, so nothing derives a stub for it, and left
    # alone it surfaces as an unhandled svc: a missing service, rather than a
    # build that stopped deliberately and said why.
    ("ams::diag::impl::FatalErrorByResultForNx(ams::Result)", "value"),
    ("ams::AbortImpl()", "bare"),
)

# AbortInfo { AbortReason reason; const LogMessage *message;
#             const char *expr, *func, *file; int line; }
_INFO_REASON = 0x00
_INFO_MESSAGE, _INFO_EXPR, _INFO_FUNC, _INFO_FILE, _INFO_LINE = 0x08, 0x10, 0x18, 0x20, 0x28

# AbortReason. A debug build (AMS_BUILD_FOR_DEBUGGING) turns AMS_ASSERT back on,
# and a failing assert reaches the same observer as an abort -- only this field
# tells them apart, so a debug run says "assertion failed" instead of "aborted".
ABORT_REASONS = {
    0: "failed an audit check",
    1: "failed an assertion",
    2: "aborted",
    3: "reached an unreachable default case",
}


def install(guest):
    """Install every abort catch on a freshly-loaded Guest.

    Once per guest, and a second call returns having done nothing. Left to
    happen twice it would hook every abort symbol again, observe every
    assertion twice and print every log line twice -- and none of that
    fails, it just doubles what the recorder says the build did.

    Marked on the guest, because whether one is being listened to is the
    guest's own fact and stops being true when it is gone.
    """
    if getattr(guest, "blackbox_installed", False):
        return
    guest.blackbox_installed = True

    for name, kind in ABORT_SYMBOLS:
        if name in guest.symbols:
            guest.hook_at(guest.symbols[name],
                          lambda guest, k=kind, n=name: _on_abort(guest, n, k),
                          label=name)

    _install_assertion_observer(guest)
    _install_log_hooks(guest)

    # An abort dies by storing a magic value to a magic address, and that has
    # to be caught however the module was built: AbortWithValue is `inline`, so
    # the copy that runs is often folded into its caller and never reaches the
    # exported symbol hooked above.
    #
    # How it is caught depends on where that address landed. Unmapped or
    # read-only, the store faults and arrives here for free; only a writable
    # one needs a hook, and that costs a third of the run.
    writable = (guest.load_lo <= STD_ABORT_MAGIC_ADDRESS < guest.load_hi
                and not any(lo <= STD_ABORT_MAGIC_ADDRESS < lo + size
                            for lo, size in guest.protected))
    if writable:
        guest.watch_writes(STD_ABORT_MAGIC_ADDRESS,
                           STD_ABORT_MAGIC_ADDRESS + 7, on_abort_store)


# ---- how it dies ----

def _on_abort(guest, name, kind):
    lr = guest.lr()
    detail = ""
    what = "aborted"

    if kind in ("args4", "args4fmt"):   # AbortImpl(expr, func, file, line[, fmt, ...])
        detail = _site(_cstr(guest, 0), _cstr(guest, 1),
                       _cstr(guest, 2), guest.arg(3))
        fmt = _cstr(guest, 4) if kind == "args4fmt" else ""
        if fmt:
            detail += ' -- message "%s"' % fmt.rstrip("\n")
    elif kind == "args3":   # UnexpectedDefaultImpl(func, file, line)
        what = ABORT_REASONS[3]
        detail = _site("unreachable default case", _cstr(guest, 0),
                       _cstr(guest, 1), guest.arg(2))
    elif kind == "abort_info":
        info = guest.arg(0)
        expr, func, file = (guest.read_cstr(guest.u64(info + off)) if guest.u64(info + off) else ""
                            for off in (_INFO_EXPR, _INFO_FUNC, _INFO_FILE))
        detail = _site(expr, func, file, guest.u32(info + _INFO_LINE))
        reason = ABORT_REASONS.get(guest.u32(info + _INFO_REASON))
        if reason:
            what = reason

        # LogMessage { const char *fmt; va_list *vl; } -- the text passed to
        # AMS_ABORT("..."). Printed raw: the arguments live in a va_list we
        # cannot walk, so "%d" stays "%d" rather than becoming a wrong number.
        msg = guest.u64(info + _INFO_MESSAGE)
        fmt = guest.read_cstr(guest.u64(msg)) if msg and guest.u64(msg) else ""
        if fmt:
            detail += ' -- message "%s"' % fmt.rstrip("\n")
    elif kind == "value":
        detail = " -- Result %s" % _format_result(guest.arg(0))

    raise GuestAbort("the build %s in %s, raised by %s%s"
                     % (what, name, guest.nearest_symbol(lr), detail))


def is_abort_store(address, value):
    """True if this access is the abort magic store, arriving as a fault."""
    return address == STD_ABORT_MAGIC_ADDRESS and _u64(value) == STD_ABORT_MAGIC_VALUE


def on_abort_store(guest, value):
    # Unicorn hands the written value to hooks as a *signed* 64-bit int.
    if _u64(value) != STD_ABORT_MAGIC_VALUE:
        return True
    guest.fail(GuestAbort(
        "the build aborted (std abort store) at %s -- Result %s"
        % (guest.nearest_symbol(guest.pc()),
           _format_result(guest.arg(0)))))
    return False


def try_handle_svc(guest, svc_number):
    """Capture svcOutputDebugString and svcBreak. True if it was handled."""
    if svc_number == SVC_BREAK:
        result = None
        address, size = guest.arg(1), guest.arg(2)
        if size == BREAK_RESULT_SIZE:
            try:
                result = guest.u32(address)
            except Exception:
                result = None
        guest.stop()
        raise GuestAbort(
            "the build called svcBreak%s"
            % ("" if result is None else " with %s" % _format_result(result)))

    if svc_number != SVC_OUTPUT_DEBUG_STRING:
        return False
    text = guest.read(guest.arg(0), guest.arg(1)).decode("utf-8", "replace")
    guest.debug_output.append(text)
    guest.ret(0)                       # Result: success
    return True


# ---- what it says ----

# AMS_LOG / AMS_VLOG, live only in a debug or auditing build. Both take a
# LogMetaData by reference and a format string.
LOG_SYMBOLS = (
    "ams::diag::impl::LogImpl(ams::diag::LogMetaData const&, char const*, ...)",
    "ams::diag::impl::VLogImpl(ams::diag::LogMetaData const&, char const*, std::__va_list)",
)

# LogMetaData { SourceInfo{ int line; const char *file; const char *func; };
#               const char *module; LogSeverity severity; int verbosity; ... }
_META_LINE, _META_FILE, _META_FUNC, _META_MODULE, _META_SEVERITY = 0x00, 0x08, 0x10, 0x18, 0x20

LOG_SEVERITIES = {0: "trace", 1: "info", 2: "warn", 3: "error", 4: "fatal"}


def _install_log_hooks(guest):
    """Capture AMS_LOG -- debug builds only; release compiles it to nothing.

    These are *replaced*, not observed. Left to run, LogImpl formats the
    message and hands it to the log observers, which take the observer
    manager's reader-writer lock -- and that lock was never initialized,
    because ams's startup is never run here. A release build cannot
    tell, but a debug build asserts and the run dies inside the logging call.
    Replacing captures what the build wanted to say and skips machinery that
    is not what anyone is here to benchmark.

    The cost is the same one the abort messages already pay: the format string
    is recorded raw, since its arguments live in varargs we cannot walk.
    """
    for name in LOG_SYMBOLS:
        if name in guest.symbols:
            guest.hook_at(guest.symbols[name], _on_log, label=name)


def _on_log(guest):
    meta = guest.arg(0)

    def at(off):
        p = guest.u64(meta + off)
        return guest.read_cstr(p) if p else ""

    fmt = guest.arg(1)
    guest.debug_output.append("[%s]%s %s:%d %s: %s" % (
        LOG_SEVERITIES.get(guest.u32(meta + _META_SEVERITY), "?"),
        (" " + at(_META_MODULE)) if at(_META_MODULE) else "",
        at(_META_FILE), guest.u32(meta + _META_LINE), at(_META_FUNC),
        (guest.read_cstr(fmt) if fmt else "").rstrip("\n")))
    guest.ret(None)


# ---- failing assertions ----

ASSERTION_TYPES = {0: "audit", 1: "assert"}


def _install_assertion_observer(guest):
    """Record failing AMS_ASSERTs -- debug builds only.

    A release build compiles AMS_ASSERT to a no-op, so this finds nothing. A
    debug build (AMS_BUILD_FOR_DEBUGGING) makes it live, and then a failing
    assert only becomes visible here: the default handler aborts, which the
    abort hooks catch, but the handler is replaceable and may return
    AssertionFailureOperation_Continue -- a survivable assert that would
    otherwise pass silently.

    This *observes* rather than replaces: the hook records and returns without
    touching PC, so the real body runs and the handler still decides. Aborting
    here would turn a survivable assert into a failed run.

    Only one of the two overloads is hooked. The 5-argument one tail-calls the
    variadic, so hooking both would record every assertion twice; prefer the
    variadic and fall back to whatever exists if a build inlined it away.
    """
    found = guest.find("ams::diag::OnAssertionFailure")
    if not found:
        return
    variadic = [n for n in found if n.endswith("...)")]
    name = variadic[0] if variadic else sorted(found)[0]
    guest.hook_at(guest.symbols[name], _on_assertion, label=name)


def _on_assertion(guest):
    # OnAssertionFailure(type, expr, func, file, line[, format, ...])
    kind = ASSERTION_TYPES.get(guest.arg(0), "assert")
    fmt = _cstr(guest, 5)
    guest.assertions.append("%s failed: %s%s" % (
        kind,
        _site(_cstr(guest, 1), _cstr(guest, 2),
              _cstr(guest, 3), guest.arg(4)).lstrip(" -"),
        ' -- message "%s"' % fmt.rstrip("\n") if fmt else ""))

    # No ret(): fall through into the real body so the handler still runs.


# ---- spelling the details ----

def _format_result(value):
    """ams::Result as Nintendo prints it: module 2 desc 1 -> 2002-0001."""
    if not value:
        return "success (0)"
    return "%04d-%04d (0x%x)" % (2000 + (value & 0x1FF), (value >> 9) & 0x1FFF, value)


def _cstr(guest, at):
    """The string a register points at, or empty if it points nowhere.

    A release build compiles its assertion strings out, so every one of these
    is usually null -- which is the case worth returning empty for rather than
    guarding at each call.
    """
    p = guest.arg(at)
    return guest.read_cstr(p) if p else ""


def _u64(v):
    return v & 0xFFFFFFFFFFFFFFFF


def _site(expr, func, file, line):
    if not (expr or file):
        return ""   # release build: strings compiled out
    return " -- %s at %s:%d in %s" % (expr or "abort", file, line, func)
