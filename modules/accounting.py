#!/usr/bin/env python3
"""What a launch cost, charged to what it was spent on: by kind, by region,
by pool.

Arithmetic over what a launch recorded, and nothing else -- no guest, no
hooks, no ELF. bench hands these what it kept; what they hand back is what a
report says.

A crossing is in exactly one line of one kind, so a kind and its lines are the
same calls said twice -- which is what makes the pair worth reading. The one
exception is a byte-moving call spanning two regions: it is counted in both,
since it did land in both.

What a line names depends on what the kind can say: a region for the calls
that carry bytes, a file for the calls that take a handle on one, and the
entry points themselves where neither can be said.
"""
import collections


# What a call does, rather than what it is called: the question is almost
# always which kind of work a build did, and the kinds cost differently -- a
# read costs an SD nothing, a write costs it a program cycle, a directory
# walk crosses the boundary once per entry. Anything unnamed still appears
# under other, so a call the harness has never seen cannot go missing.
#
# reads and writes are the calls that carry bytes, and only those, so every
# line under them is somewhere those bytes landed. What makes a file, sizes
# it, flushes it, removes it or asks how big it is keeps a file rather than
# moving anything, and is its own kind -- a build can stop doing those
# without serving one byte less.
FS_GROUPS = (
    ("reads", ("fsStorageRead", "fsFileRead")),
    ("writes", ("fsFileWrite",)),
    ("handles", ("fsOpenSdCardFileSystem", "fsFsOpenFile", "fsFileClose",
                 "fsFsClose", "fsStorageClose")),
    ("walks", ("fsFsOpenDirectory", "fsDirRead", "fsDirGetEntryCount", "fsDirClose")),
    ("upkeep", ("fsFsCreateFile", "fsFileSetSize", "fsFileFlush",
                "fsFsDeleteFile", "fsFsCreateDirectory",
                "fsFileGetSize", "fsStorageGetSize")),
)

# The three calls that record where they moved bytes. Everything else crosses
# the boundary without carrying any, which is why it is accounted for by name
# rather than by region.
BYTE_CALLS = ("fsStorageRead", "fsFileRead", "fsFileWrite")

# Where a crossing lands when no layout covers it, and the file it went to is
# not known either.
ELSEWHERE = "elsewhere"


# What a run's phases add up to. A peak rather than a sum for the memory: the
# pools are reused between phases, so adding them would count the same bytes
# once per phase.
Totals = collections.namedtuple(
    "Totals", "fs_calls read written seconds peak calls phases")

# One place the module can put a byte, how much of it was written to, and how
# much there was. Named in the order a reader wants them: what it took, then
# what it had.
Pool = collections.namedtuple("Pool", "name used size")


# What a kind of call cost, and the lines that make it up. A line is a region
# or a file where that can be said, and a bare list of entry points where it
# cannot; bytes and again are None for a line that moved none.
Kind = collections.namedtuple("Kind", "name calls bytes lines entries")
Line = collections.namedtuple(
    "Line", "label calls bytes again smallest largest calls_detail")

# One line of a breakdown: a region, how many calls landed in it, what they
# moved, and how much of that was ground already covered.
Row = collections.namedtuple(
    "Row", "name calls bytes again smallest largest")


def totals(phases):
    """What a run's phases add up to.

    Counted here rather than by the report, for the reason everything else in
    this module is: a report should be walking figures, not making them.
    """
    calls = {}
    for p in phases:
        for name, n in p.calls.items():
            calls[name] = calls.get(name, 0) + n

    return Totals(fs_calls=sum(p.fs_calls for p in phases),
                  read=sum(p.read for p in phases),
                  written=sum(p.written for p in phases),
                  seconds=sum(p.seconds for p in phases),
                  peak=max((p.touched for p in phases), default=0),
                  calls=calls,

                  # Where in the launch they fell. A phase that crossed the
                  # boundary at all, since one that did not is not a place
                  # the cost went.
                  phases=[(p.name, p.fs_calls) for p in phases if p.fs_calls])


def by_pool(regions):
    """[Pool] -- what the module took, per place it can put a byte.

    The kernel's own answer, turned round: it says (label, size, touched)
    because it is describing a region, and this says used before size because
    a reader compares what was taken against what there was.
    """
    return [Pool(label, used, size) for label, size, used in regions]


def by_kind(run_totals):
    """[(kind, calls, [(entry point, calls)])], by what the call does.

    Anything the kinds do not name is gathered under other, so a call this
    has never seen cannot go missing from a total.
    """
    out, listed = [], set()

    for heading, members in FS_GROUPS:
        calls = sorted(((m, run_totals.calls[m]) for m in members
                        if run_totals.calls.get(m)),
                       key=lambda kv: (-kv[1], kv[0]))
        listed.update(m for m, _n in calls)
        if calls:
            out.append((heading, sum(n for _m, n in calls), calls))

    rest = sorted((k, v) for k, v in run_totals.calls.items()
                  if k not in listed and v)
    if rest:
        out.append(("other", sum(v for _k, v in rest), rest))
    return out


def by_what_for(run_totals, region_rows, touches):
    """[Kind] -- every crossing, once, under what it was for.

    Each kind says what it cost and is followed by its own detail: the
    regions bytes landed in, the files handles were taken on, or the entry
    points themselves where neither can be said. A call is in exactly one
    line, so a kind's total and its lines are the same crossings twice --
    which is what makes the pair worth reading.

    The one exception is a byte-moving call that spans two regions: it is
    counted in both, since it did land in both. Rare enough to say here
    rather than to hide by halving it.
    """
    out = []
    for name, calls, entries in by_kind(run_totals):
        moved = [(m, n) for m, n in entries if m in BYTE_CALLS]
        still = [(m, n) for m, n in entries if m not in BYTE_CALLS]

        lines = []
        if moved:
            lines += [Line(row.name, row.calls, row.bytes, row.again,
                           row.smallest, row.largest, ())
                      for row in region_rows(name == "writes")]
        elif name == "handles":
            lines += _file_lines(touches, entries)
            still = []

        if still:
            # Named rather than placed. Under reads and writes this is a
            # guard rather than a row: those kinds hold only the calls that
            # record where they went, so a line appears here if one is ever
            # added that does not.
            seen = sum(n for _m, n in still)
            lines.append(Line("(moved nothing)" if moved else "", seen,
                              None, None, None, None, still))

        out.append(Kind(name, calls,
                        sum(row.bytes for row in lines
                            if row.bytes is not None) if moved else None,
                        lines, entries))
    return out


def _file_lines(touches, entries):
    """[Line] per file, for the calls of one kind that name one.

    entries is that kind's own (entry point, calls), so what is left over
    is counted against the crossings the kind was counted from -- not
    against every call the run ever made, which would charge a phase for
    what the verifier did afterwards.

    A call of this kind that names no file -- a filesystem opened, a
    session closed -- is gathered into one line, so the lines still add up
    to what the kind crossed.
    """
    members = dict(entries)
    per_file, loose = {}, collections.Counter()
    for call in touches:
        if call.call in members:
            per_file.setdefault(call.path,
                                collections.Counter())[call.call] += 1

    counted = collections.Counter()
    for kinds in per_file.values():
        counted.update(kinds)
    for name, n in members.items():
        if n - counted[name] > 0:
            loose[name] = n - counted[name]

    lines = [Line(path, sum(kinds.values()), None, None, None, None,
                  sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
             for path, kinds in sorted(
                 per_file.items(),
                 key=lambda kv: (-sum(kv[1].values()), kv[0]))]

    if loose:
        lines.append(Line("(not on a file)", sum(loose.values()), None, None,
                          None, None,
                          sorted(loose.items(),
                                 key=lambda kv: (-kv[1], kv[0]))))
    return lines


def by_region(ranges, layouts, written):
    """[Row] for the ranges of that half, by region, busiest first.

    layouts is {target: [Region]}. A target with no layout, or a range past
    every region of one, is charged to ELSEWHERE rather than dropped -- a
    report that quietly loses bytes is worse than one that says it does not
    know where they went.

    Covered bytes are tracked per region, so a range overlapping one already
    read is counted as read again. The tracking is a list of merged spans,
    which is small: a build covers a table in order, so the spans collapse as
    fast as they are added.
    """
    calls = collections.Counter()
    moved = collections.Counter()
    again = collections.Counter()
    smallest, largest = {}, collections.Counter()
    covered = {}

    for r in ranges:
        if r.written != written:
            continue
        accounted = 0
        for name, first, last in _regions_hit(r, layouts.get(r.target)):
            start = max(r.offset, first)
            end = min(r.offset + r.size, last)
            moved_now = end - start

            calls[name] += 1
            moved[name] += moved_now
            again[name] += _cover(covered.setdefault(name, []), start, end)
            largest[name] = max(largest[name], moved_now)
            smallest[name] = min(smallest.get(name, moved_now), moved_now)
            accounted += moved_now

        # Whatever the regions did not account for still crossed, so it is
        # named rather than dropped.
        if accounted < r.size:
            calls[ELSEWHERE] += 1
            moved[ELSEWHERE] += r.size - accounted
            largest[ELSEWHERE] = max(largest[ELSEWHERE], r.size - accounted)
            smallest[ELSEWHERE] = min(smallest.get(ELSEWHERE,
                                                   r.size - accounted),
                                      r.size - accounted)

    return sorted((Row(name, calls[name], moved[name], again[name],
                       smallest.get(name, 0), largest[name])
                   for name in calls),
                  key=lambda row: (-row.bytes, row.name))


def _regions_hit(r, regions):
    """Every (name, first, last) that range touches.

    A target nothing is known about is charged to the file itself, which is a
    name and the range's own bounds: which file the bytes went to is worth
    saying even when where in it is not.
    """
    hit = [(name, first, last) for name, first, last in regions or ()
           if r.offset < last and r.offset + r.size > first]
    return hit or [(_name_of(r.target), r.offset, r.offset + r.size)]


def _name_of(target):
    return target.rsplit("/", 1)[-1]


def _cover(spans, start, end):
    """Add [start, end) to spans; return how much of it was already there."""
    overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in spans)

    merged, at = [], (start, end)
    for span in sorted(spans + [at]):
        if merged and span[0] <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], span[1]))
        else:
            merged.append(span)
    spans[:] = merged
    return overlap
