#!/usr/bin/env python3
"""Which service boundaries a build reaches, derived from the binary.

What stands in for the rest of Horizon during a run is a refusal installed
on everything found here, so no hand-written list has to be kept in step with a
build's call graph. Nothing is stubbed from this file -- it only says what
would leave the process.

A "boundary" is a function that the module calls, that this harness has not
given real behaviour to, and whose call subtree reaches an `svc` or dispatches
indirectly -- i.e. code that would leave the sandbox and talk to another
process. Those are exactly the calls that must be answered here:

  1. take function ranges from the symbol table
  2. scan the text once for `svc`, for call edges (`bl`, and `b` where it
     leaves the function it is in, which is a sibling call), and for indirect
     dispatch (`blr`, `br`)
  3. mark every function that transitively reaches an `svc`
  4. keep the ones called *from* the module under test, in a namespace the
     harness does not serve for real, and not already hooked

Which module is under test and which namespaces are served for real are the
caller's to know, so they arrive as arguments.

The action follows too: if the demangled signature's first parameter is a
pointer, the function fills something in for its caller, which a stub must
therefore initialise -- a caller destroys that object whether or not the call
succeeded.

There is no override list: a wrong stub means a wrong rule, and the rule is
what gets fixed.
"""
import array
import bisect


# The instructions this scan reads. svc is stated in guest.py too: what
# one looks like is a fact about ARM64, and a module that reads instructions
# should not have to import one.
#
# svc #imm16 -- matched with the immediate masked out rather than as a bare
# `svc #0`, since the syscall number lives there.
SVC_MASK, SVC_OP = 0xFFE0001F, 0xD4000001

# bl imm26 -- a direct call, and the commonest instruction in a text section,
# so the scan tests for it first. The immediate is a signed count of words.
BL_SHIFT, BL_OP = 26, 0b100101
BL_IMM_MASK, BL_IMM_SIGN, BL_IMM_SPAN = 0x03FFFFFF, 0x02000000, 0x04000000

# b imm26 -- the same shift and the same immediate, and a call whenever it
# lands outside the function it was written in: that is a sibling call, which a
# compiler emits wherever a call is the last thing a function does. Read as an
# edge for that reason, and only then -- a branch within a function is a loop
# or an if.
B_OP = 0b000101

# blr xN and br xN -- an indirect call, and its tail-call form. Service clients
# reach another process through the sf framework's virtual dispatch, which a
# scan following direct branches cannot see; the presence of indirect dispatch
# is the signal that a call leaves this process.
BLR_MASK, BLR_OP = 0xFFFFFC1F, 0xD63F0000
BR_MASK, BR_OP = 0xFFFFFC1F, 0xD61F0000


def _function_ranges(guest):
    """[(start, end, name)] sorted by address, from the symbol table.

    A symbol ends where nm says it ends. Reaching to the next symbol instead
    would hand a function whatever follows it -- alignment padding, or code the
    linker left unnamed -- and an svc or a call sitting there would be charged
    to it. Where nm stated no size there is nothing better than the next
    symbol, which is 299 of 4209 on the build this was measured against.
    """
    sizes = guest.symbol_sizes
    syms = sorted((a, n) for n, a in guest.symbols.items())
    out = []
    for i, (addr, name) in enumerate(syms):
        size = sizes.get(name)
        end = addr + size if size else (syms[i + 1][0]
                                        if i + 1 < len(syms) else addr)
        if end > addr:
            out.append((addr, end, name))
    return out


def _scan(guest, ranges):
    """(functions containing an svc, call edges) from one pass over the code."""
    starts = [r[0] for r in ranges]

    def owner(addr):
        i = bisect.bisect_right(starts, addr) - 1
        if 0 <= i < len(ranges) and ranges[i][0] <= addr < ranges[i][1]:
            return ranges[i][2]
        return None

    has_svc, has_blr, edges = set(), set(), {}
    for base, size in guest.code_ranges:
        text = guest.read(base, size)

        # One C-level pass rather than a struct.unpack per word: AArch64 is
        # fixed-width and this only ever runs little-endian, so a whole section
        # converts at once.
        words = array.array("I", text[:len(text) & ~3])

        for i, word in enumerate(words):
            if (word >> BL_SHIFT) in (BL_OP, B_OP):
                imm = word & BL_IMM_MASK
                if imm & BL_IMM_SIGN:
                    imm -= BL_IMM_SPAN
                addr = base + i * 4
                caller, callee = owner(addr), owner(addr + imm * 4)

                # Where the two differ this is a call, whichever of the two
                # instructions it was: a b that lands in another function is
                # how a compiler writes a call it does not need to return from.
                if caller and callee and caller != callee:
                    edges.setdefault(caller, set()).add(callee)
            elif (word & SVC_MASK) == SVC_OP:
                fn = owner(base + i * 4)
                if fn:
                    has_svc.add(fn)
            elif (word & BLR_MASK) == BLR_OP or (word & BR_MASK) == BR_OP:
                fn = owner(base + i * 4)
                if fn:
                    has_blr.add(fn)
    return has_svc, has_blr, edges


def _reaches_svc(has_svc, edges):
    """Every function whose call subtree reaches an svc, the edges walked
    backwards from the ones that hold one."""
    callers_of = {}
    for caller, callees in edges.items():
        for callee in callees:
            callers_of.setdefault(callee, set()).add(caller)

    reaches, work = set(has_svc), list(has_svc)
    while work:
        fn = work.pop()
        for caller in callers_of.get(fn, ()):
            if caller not in reaches:
                reaches.add(caller)
                work.append(caller)
    return reaches


def _first_param_is_pointer(demangled):
    """True when the first parameter is a pointer, i.e. the function fills in
    something its caller owns -- which the stub must therefore initialise.

    A rule about C++ signatures rather than a step of the search: it holds or
    fails on a string, with no binary in sight. Which is why the depth
    tracking is worth reading closely -- a comma inside a template argument
    is not the end of the first parameter.
    """
    if "(" not in demangled:
        return False
    args = demangled[demangled.index("(") + 1:]
    depth = 0
    first = ""
    for ch in args:
        if ch in "(<":
            depth += 1
        elif ch in ")>":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        first += ch
    return first.strip().endswith("*")


def found_in(guest, module_prefix, service_namespaces,
             already_hooked=()):
    """[(symbol, fills in its first argument)] for the boundaries this binary
    reaches.

    The second half is what a stub has to do about it: a function whose first
    parameter is a pointer writes through it, and its caller destroys what it
    points at whether the call succeeded or not.
    """

    ranges = _function_ranges(guest)
    has_svc, has_blr, edges = _scan(guest, ranges)
    reaches = _reaches_svc(has_svc, edges)

    hooked = set(already_hooked)
    boundary = set()
    for caller, callees in edges.items():
        if not caller.startswith(module_prefix):
            continue
        for callee in callees:
            if callee.startswith(module_prefix) or callee in hooked:
                continue
            if not callee.startswith(service_namespaces):
                continue  # not a service: leave it running for real

            # Either it reaches a syscall directly, or it dispatches indirectly
            # -- how an sf client reaches the process that serves it. A service
            # helper that does neither (string formatting, say) is left alone.
            if callee in reaches or callee in has_blr:
                boundary.add(callee)

    return [(sym, _first_param_is_pointer(sym)) for sym in sorted(boundary)]
