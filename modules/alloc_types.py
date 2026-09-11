# What each kind asked for, named from the binary's own enum.
#
# What a build's memory was FOR, as against where it came from, which is
# hos_memory's. That one reads its pages back, so it sees the allocator's own
# overhead too and is the figure for whether a build fits; this one sums what
# was asked for, and the gap between them is the difference.
#
# Two figures, both off the allocation alone: what each kind asked for in all,
# and how many times it asked. Asked, not took -- a kind that is recycled asks
# many times for the same room, and a request that fails asks for nothing it
# receives.
#
# Nothing about a moment. A peak, or what was left at the end, needs the
# releases as well, and a compiler may leave the free in several bodies with
# the calls going to one nothing stands at. A running total that never comes
# down reads as a peak and is not one.
#
# The kinds are read out of the binary's debug information rather than written
# down here, so a build whose enum differs is named correctly without this file
# knowing anything about it.
import collections
import io
import sys

from elftools.elf.elffile import ELFFile


ENUM_NAME = b"AllocationType"

# Dropped from the report: it is the enum's bound, not a kind anything allocates.
COUNT_SUFFIX = "_Count"

Kind = collections.namedtuple("Kind", "name bytes allocs")


def kind_names(elf_path):
    """{value: name} for the allocation kinds this binary was built with.

    Empty where the binary carries no debug information, in which case the
    caller shows the numbers and says nothing it cannot support.
    """
    with open(elf_path, "rb") as f:
        elf = ELFFile(io.BytesIO(f.read()))
        if not elf.has_dwarf_info():
            return {}

        for cu in elf.get_dwarf_info().iter_CUs():
            for die in cu.iter_DIEs():
                if die.tag != "DW_TAG_enumeration_type":
                    continue
                named = die.attributes.get("DW_AT_name")
                if named is None or named.value != ENUM_NAME:
                    continue

                out = {}
                for child in die.iter_children():
                    if child.tag != "DW_TAG_enumerator":
                        continue
                    value = child.attributes["DW_AT_const_value"].value
                    name = child.attributes["DW_AT_name"].value.decode()
                    out[value] = name
                if out:
                    return out
    return {}


class Tally:
    """Per-kind totals, gathered by watching the one function that hands memory out.

    A watch rather than a hook: the allocation still happens, and what it cost
    is read on the way past. AllocateTracked takes (kind, size), so the kind is
    in the first argument and nothing has to be matched up with anything.

    The free is not watched, because nothing here needs it. What it would give
    -- a peak, or what a kind kept -- rests on a running total coming down, and
    a compiler that leaves the free in several bodies puts the calls where
    nothing stands. Both figures below need only the allocation.

    A failed allocation is counted as taken. It returns null and the build says
    so where it finds out, so the run ends either way and the figure is what was
    asked for -- which is what this measures.
    """

    def __init__(self, names):
        self._names = names
        self._bytes = collections.Counter()
        self._allocs = collections.Counter()

    def watch(self, guest, alloc_addr):
        def took(g):
            kind, size = g.arg(0), g.arg(1)
            self._allocs[kind] += 1
            self._bytes[kind] += size

        guest.watch(alloc_addr, took)

    def kinds(self):
        """[Kind] for every type that ever asked, most bytes asked for first."""
        out = []
        for value in sorted(self._allocs):
            name = self._names.get(value, "kind %d" % value)
            if name.endswith(COUNT_SUFFIX):
                continue
            out.append(Kind(name=name,
                            bytes=self._bytes[value],
                            allocs=self._allocs[value]))
        return sorted(out, key=lambda k: (-k.bytes, k.name))


class _Caller:
    """Stands in for the guest, so the tally can be driven without one."""

    def __init__(self):
        self._at = {}
        self._args = ()

    def watch(self, addr, fn):
        self._at[addr] = fn

    def arg(self, i):
        return self._args[i]

    def call(self, addr, *args):
        self._args = args
        self._at[addr](self)


def test_tally():
    """[(rule, holds, measured)] for a tally driven by hand.

    The kind comes off the first argument and the size off the second, which is
    the whole of it: no release to miss and no moment to catch. What is left to
    get wrong is the pair -- summing under the wrong kind, or reading one
    argument where the other is -- and two kinds at unequal sizes, one of them
    taken twice, tells either apart from a run that got it right.

    Yielded rather than asserted: an assert is stripped by -O, so a check
    written with one is absent exactly when a build is being run for speed.
    """
    ALLOC = 0x10
    tally = Tally({0: "A", 1: "B"})
    caller = _Caller()
    tally.watch(caller, ALLOC)

    caller.call(ALLOC, 0, 100)          # A, once, 100 bytes
    caller.call(ALLOC, 1, 50)           # B, twice, 120 between them
    caller.call(ALLOC, 1, 70)

    rows = tally.kinds()
    got = {k.name: k for k in rows}

    yield ("bytes summed under the kind that asked",
           got["A"].bytes == 100 and got["B"].bytes == 120,
           "A %d of 100; B %d of 120" % (got["A"].bytes, got["B"].bytes))

    yield ("one call counted per allocation",
           got["A"].allocs == 1 and got["B"].allocs == 2,
           "A %d of 1; B %d of 2" % (got["A"].allocs, got["B"].allocs))

    # Most bytes first, which is B on 120 against A on 100 -- so the order is by
    # what a kind was given and not by how often it asked.
    yield ("ordered by bytes, not by calls",
           [k.name for k in rows] == ["B", "A"],
           "%s; wanted B then A" % " then ".join(k.name for k in rows))


if __name__ == "__main__":
    broken = 0
    for rule, holds, measured in test_tally():
        print("  %-46s %6s" % (rule, "ok" if holds else "FAILED"))
        print("    %s" % measured)
        broken += not holds
    sys.exit(1 if broken else 0)
