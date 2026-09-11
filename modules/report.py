#!/usr/bin/env python3
"""Every figure, and the words and widths around it.

What is measured is bench's; what it means to a reader is here. A number and
the words around it are one decision -- a column's width, whether a zero is
worth a row, which of two figures a reader compares first, whether a figure
belongs on the line above -- and they belong in one file rather than beside
the counters.

Nothing here works a figure out, and nothing here can reach a run. What the
run was asked for and what it came to both arrive as values; this says them.
"""
import os
import sys
import traceback


WIDTH = 96


ELLIPSIS = "[...]"


def _shown(path, width=WIDTH - 22):
    """A path as a report should carry it, disclosing nothing about the machine.

    A report is meant to be pasted, and an absolute path carries a home
    directory and a username into every one. Relative to where the command ran
    says as much and discloses nothing; failing that, the home directory
    becomes ~.

    Kept inside `width`, so a header stays a header. A relative path keeps the
    directory it starts from -- that is where the reader would have to stand
    for the rest to mean anything -- while an absolute one drops components
    from the front, having no such anchor to lose.
    """
    rel = os.path.relpath(path)
    rooted = rel.startswith(os.pardir)
    if rooted:
        full, home = os.path.abspath(path), os.path.expanduser("~")
        rel = ("~" + full[len(home):]
               if home != os.sep and (full == home
                                      or full.startswith(home + os.sep))
               else full)
    if len(rel) <= width:
        return rel

    parts = rel.split(os.sep)
    head = ELLIPSIS + os.sep

    # ~ alone says only that someone owns it, where ~/Atmosphere-stream says
    # which tree; a leading / says nothing either way and is not kept.
    lead = 2 if rel.startswith("~" + os.sep) else (0 if rooted else 1)
    if lead and len(parts) > lead + 1:
        head = os.sep.join(parts[:lead]) + os.sep + ELLIPSIS + os.sep
        parts = parts[lead:]

    tail = ""
    for _part in reversed(parts):
        step = _part + (os.sep + tail if tail else "")
        if len(head) + len(step) > width:
            break
        tail = step
    return head + tail if tail else rel[-width:]


# ----------------------------------------------------------------------------
# The page: its units, its columns and its seams
# ----------------------------------------------------------------------------

# Binary megabytes, and named as such. Everything measured here is memory or a
# file size in a format whose fields are powers of two, so 2^20 is the honest
# divisor -- and MB is 10^6, a different number by five per cent.
#
# Figures quoted from Atmosphere are written MiB too. Its _MB literal is
# 1024 * 1024, so 20_MB is twenty binary megabytes; spelling that MiB is not a
# misquote of the source, it is what the source computes.
MIB = 1048576.0
KIB = 1024.0

# One column geometry for both tables, so the RAM rows and the phase rows land
# on the same character positions and the eye can run straight down. A 26-wide
# label, then a 13-wide value column: wide enough for "32768.000 KiB", which is
# the largest pool there is. A whole romfs is longer than that and is printed in
# a sentence rather than a column. The RAM table carries a second, what the pool
# holds, after the word "of".
ROW = "  %-26s   %13s"
MEM_ROW = ROW + "  of  %13s"


# A _part of the report is broken from the one above it by a rule half the
# banner's width: the banner is the report's edge and this is a seam inside
# it, so it is derived from that width rather than picked to look right beside
# it.
SEPARATOR = "  " + "-" * (WIDTH // 2)


def _underline(heading):
    """Stars the width of the heading, colon included.

    Not the character the seam above it is drawn with: one marks a heading and
    the other marks a break, and two rules of the same stuff read as one.
    """
    return "  " + "*" * len(heading)


def _part(heading):
    """Break from the _part above, and head the one starting here."""
    print(SEPARATOR)
    print()
    print("  %s" % heading)
    print(_underline(heading))


def _mib(n):
    """Bytes as KiB, with the unit attached -- the tables align on the string.

    One unit for every size the report prints, so a column reads down rather
    than being converted row by row. Three decimals because the small figures
    are real: a romfs header is eighty bytes and should not round to nothing.
    """
    return "%.3f KiB" % (n / KIB)


def _print_list(heading, lines, width=WIDTH - 4):
    if lines:
        print("  %s:" % heading)
        for line in lines[:20]:
            print("    %s" % line[:width])


def print_header(premises):
    """What this run is, stated before it starts.

    Every line is a premise rather than a result: which build, how big the
    game is, what the mod is, what the run does to the SD. A report is meant
    to be pasted, and two of them are only comparable if both say what they
    were asked.

    The program id is not among them. It is invented and fixed, so it tells a
    reader nothing, and it still shows on every path the run writes to the SD.
    """
    print("=" * WIDTH)
    print("BUILD   : %s" % _shown(premises.build))

    # The base romfs, then what is layered over it. Named as LayeredFS names
    # it: the layer under the mod, not "the game", which would read as the
    # title rather than its size.
    print("BASE    : %d file(s)" % premises.romfs["base"])

    # Whether ncm reports an update installed, which is the whole of what
    # a patched run varies: the versions either title is at are the database's
    # business, and a build fingerprints them rather than reads them.
    update = "installed" if premises.patched else "none"
    if premises.update_hidden:
        update += "  /!\\ --patched dropped: the mod(s) override(s) every base file"
    print("UPDATE  : %s" % update)

    # Named for where each delivery sits, which is what the build reads: one
    # walks romfs/, the other opens romfs.bin. Overridden and added apart, since
    # the two behave differently. A delivery carrying nothing is not listed.
    where = {"loose": "romfs/", "bin": "romfs.bin"}
    rows = [(where[k], premises.romfs[k]["overridden"],
             premises.romfs[k]["added"])
            for k in ("loose", "bin")
            if premises.romfs[k]["overridden"] or premises.romfs[k]["added"]]

    width = max(len(name) for name, _o, _a in rows)
    for n, (name, overridden, added) in enumerate(rows):
        print("%s %-*s holds %d overridden file(s) and %d added"
              % ("MOD     :" if n == 0 else "         ", width, name, overridden, added))

    # An attempt, not a fact: a build keeping no ApplicationsWithDynamicHeap[]
    # has no table to inject the row into, and the borrowed rows below say what
    # came of it. Both halves always, since they are separate regions -- but
    # both zero reads better as a word than as two figures.
    heap = premises.heap
    print("DYNHEAP : will try to borrow %s"
          % ("%d MiB app, %d MiB sys" % (heap["a"], heap["s"])
             if heap["a"] or heap["s"] else "nothing"))

    # Where the SD is mirrored, then what the run does to it. A build that
    # finds a usable cache is indistinguishable from one that has nothing to
    # write, so the state it starts from is the only way to tell a cold from a
    # warm launch. One sentence over two lines, since the path can run long
    # enough to crowd what follows it. With its slash, as the mod rows carry
    # theirs: romfs/ is a tree and romfs.bin is a file, and this is a
    # directory.
    print("SDMC    : mirrored to %s/," % _shown(premises.sdmc))
    print("          %s"
          % ("emptied first (except the ams config file)"
             if premises.fresh_sd else "left as it is"))

    print("=" * WIDTH)


def _print_time(seconds, stubs=(), run_seconds=None):
    """What the launch cost, what the run cost, and what it stood in for.

    None of them is a result. The run is everything the process burnt, most of
    it the harness building a base romfs to run against; the figure inside it is
    the measured phases, which is what moves with the build under test.

    The whole first and the part after, because the part is inside it. Said the
    other way round the second figure needed a word for "and the rest as well",
    and every such word reads as though it were a separate quantity.
    """

    # At the top because it is not a result, which keeps it away from the ones
    # that are. Not "emulator time": that reads as a guest clock, and this is
    # host CPU burnt running one.
    if run_seconds is None or run_seconds < seconds:
        print("  Host CPU time: %.2fs (not on real hardware)" % seconds)
    else:
        print("  Host CPU time: %.2fs for the run, %.2fs of it emulating the "
              "launch (not on real hardware)" % (run_seconds, seconds))

    # Here for the same reason, and only when there are any: a call the sandbox
    # answered in the target's place is a property of the sandbox. Below the
    # line above because that one is always exactly one line and this is none
    # to many.
    if stubs:
        print()
        print("  Answered by a stub, not the real call:")
        for name, n in stubs:
            print("    %-58s x%d" % (name[:58], n))

    print()


def _print_memory(result, peak=None):
    """Where the peak sat, by whose memory each pool is."""

    # Nothing after the colon: a sentence there competes with the table it
    # introduces, and the grant is already in the borrowed row's "of" column.
    _part("RAM usage (at peak) :")

    # Which pool pays, which is what decides whether a bigger game still fits.
    # Three decimals: the rows and the total below them are one measurement,
    # so anything between them is rounding, and this is where it would show.
    print()
    for pool in result.pools:
        print(MEM_ROW % (pool.name, _mib(pool.used), _mib(pool.size)))

    # Only for a run that finished: a peak is a high-water mark over phases,
    # and a failed launch has none to quote. The rows still say where it went.
    if peak is not None:
        print()
        print(ROW % ("TOTAL", _mib(peak)))

    print()
    _print_allocations(getattr(result, "allocations", ()))


# Held first and peak second, each with its bytes and its count together: they
# are one moment described twice, and separating them by unit meant reading a
# view off every other column.
#
# Not built on ROW, which is the pools' and holds labels of twenty-five
# characters. The longest kind is sixteen, so this sizes for its own.
ALLOC_ROW = "  %-18s  %13s    %11s"


def _print_allocations(kinds):
    """The same memory the pools measured, split by the type that asked for it.

    Absent where the build does not name its allocations, which is not a
    failure -- the pools above are the measurement, and this is the split.

    The kinds do not all crest at the same moment, so the peak columns do not
    sum to anything and are not totalled. Each says the most that kind ever held
    at once, and that is all it says.

    What each still held at the end is not among them. It could only be given
    for a build whose releases can be watched, and a compiler is free to leave
    several bodies of the free -- a clone for the sites where an argument was
    constant, a part split off the rest -- with the calls going to a body
    nothing stands at. Every allocation is seen either way, so the marks hold.
    """
    if not kinds:
        return

    _part("Allocations by type :")
    print()
    # The worst moment in two units, and then something else: total calls is
    # times the allocator was asked, across the run, where the two beside it
    # count objects live at an instant. Total because of that company -- calls
    # at an instant are not a thing, but the three sit together and nothing
    # else says which of them spans the run.
    #
    # It is what a type cost in work rather than in room, it is the only column
    # that sees a buffer freed and taken again, and it counts a request that
    # failed -- which is why it is named for the call and not for what came
    # back.
    print(ALLOC_ROW % ("", "bytes asked", "total calls"))

    # All of them, biggest peak first even though peak is no longer the leading
    # column: what a type rose to is what decides whether it mattered, and
    # ordering by what is left would put the ones that cost nothing on top.
    for k in kinds:
        print(ALLOC_ROW % (k.name.replace("AllocationType_", ""),
                           _mib(k.bytes), k.allocs))

    # Nothing is totalled. The peaks are each a different moment and adding them
    # would state a sum no instant ever reached.
    print()





def _report_written(written):
    """List the files that reached the SD.

    Printed empty as well, since a launch with nothing to write is a result.
    Listed on a failed launch as much as a finished one -- where they are
    kept, and whether an SD was given at all, is bench's to decide.

    The paths arrive as they land on disk, not as the module spelled them:
    the module capitalises the program id, and this is the line someone
    compares against an ls.
    """
    print("  File(s) written to SD:%s" % ("" if written else " none"))

    for path, size in written[:20]:
        print("    %-58s %8d bytes" % (path[:58], size))


def _print_fs(result):
    """What crossed the fs boundary: where in the launch, what for, and what
    reached the SD.

    The total first, since that is the figure two runs are compared on, then
    where in the launch it fell -- a line each, since three entry points read
    as three facts rather than as a sentence. What a phase weighed is not
    among them: bytes are said once, under the kind that moved them.

    Then the kinds, the ones that carry bytes first, each followed by the
    lines it is made of. A blank line where the bytes stop being the subject,
    since a rule there would be a second kind of seam inside a _part that
    already has one.
    """

    _part("FS usage :")

    print()

    totals = result.totals

    # One width for the total and the phases under it, taken from the total --
    # they sum to it, so nothing beneath can be wider. Written down it was three,
    # which held until a build crossed the boundary four thousand times and the
    # column it was holding gave way to the figure it was meant to line up.
    width = len(str(totals.fs_calls))
    print("  %*d fs calls:" % (width, totals.fs_calls))
    for phase, calls in totals.phases:
        print("  %*d in %s" % (width, calls, phase))

    # Two blank lines: what follows is what those calls were for, rather than
    # more of them.
    print()
    print()

    carried = False
    for kind in result.kinds:
        # And again where the bytes stop being the subject.
        if carried and kind.bytes is None:
            print()
        carried = kind.bytes is not None

        # A kind whose lines are regions names the calls they came from:
        # nothing below says which entry point carried the bytes, and reads
        # arriving as a storage rather than as a file is worth knowing. The
        # other kinds name theirs on every line already.
        #
        # What it does not carry is a figure: the counts and the bytes are on
        # the lines, and a heading repeating their sum is something a reader
        # has to check rather than read.
        print("  %s%s:"
              % (kind.name,
                 " (%s)" % ", ".join(name[2:] for name, _n in kind.entries)
                 if kind.bytes is not None else ""))

        for line in kind.lines:
            head = "    %-20s %5s    " % (
                _labelled(line.label, result.contents), line.calls or "")

            # A line with nothing to name -- a kind whose whole detail is the
            # entry points it was made of -- carries only those.
            if not line.label:
                head = "    "

            if line.bytes is not None:
                print((head + _line_detail(line, kind.name,
                                           WIDTH - len(head))).rstrip())
                continue

            for row in _listed(head, ["%s %d" % (name[2:], n)
                                      for name, n in line.calls_detail]):
                print(row)
        print()

    # And below the last of them, before what is left on the SD.
    print()
    _report_written(result.written)

    # Written says how much the SD was made to take; this says whether any of
    # it was taken twice. Flash cannot rewrite in place, and a second pass is
    # invisible in the call count, which weights a 256 KB write like a 0 KB
    # one.
    #
    # Said as two figures rather than one causing the other. A build that reuses
    # a file and rewrites four bytes of it wrote four bytes and leaves whatever
    # the file already held, so what is on the card is not what these writes
    # made -- which is the ordinary case for a launch that only appends.
    #
    # The line above it belongs to it: a launch that wrote nothing has neither,
    # and a blank standing in for an absent line reads as something missing.
    on_sd = sum(size for _p, size in result.written)
    if on_sd:
        print()
        print("  Write amplification: %.2fx (%s written, %s held by the files)"
              % (totals.written / on_sd, _mib(totals.written), _mib(on_sd)))

    print()


def _listed(head, items):
    """[str] -- items as a comma list under head, broken rather than cut.

    A run naming a dozen entry points would otherwise lose the last of them
    to the rule, and the one it loses is the rarest, which is the one worth
    seeing. Continuations line up under the first item.
    """
    out, row, indent = [], head, " " * len(head)
    for n, item in enumerate(items):
        piece = item + (", " if n < len(items) - 1 else "")
        if len(row) + len(piece.rstrip()) > WIDTH and row.strip():
            out.append(row.rstrip())
            row = indent
        row += piece

    out.append(row.rstrip())
    return out


def _labelled(label, contents):
    """What to call a line: a region by its name, a file by its file.

    A file in the title's own directory is named by the file alone -- that
    directory is printed in full under what reached the SD, so repeating it
    here buys an ellipsis where the rest of it was. Anywhere else keeps as
    much of the path as the column holds, since then it is what says which
    file this is.
    """
    if label.startswith(contents + "/"):
        return label[len(contents) + 1:]
    return _shown(label, 20) if "/" in label else label


def _trimmed(n):
    """A size with no more of it than it needs, for the asides beside a figure.

    The columns keep their decimals so they line up; this is prose, and three
    zeros after a round number are three characters the clause beside it wants.
    """
    size, unit = _mib(n).split()
    size = size.rstrip("0").rstrip(".") if "." in size else size
    return "%s %s" % (size, unit)


def _chunks(smallest, largest):
    """What the calls carried, from the smallest chunk to the biggest.

    One figure rather than two, because a pair of them plus what crossed twice
    is wider than the rule -- and it is one thought anyway: chunks of a size,
    or chunks from this size up to that one.
    """
    low, high = _trimmed(smallest), _trimmed(largest)
    if low == high:
        return "%s chunks" % high

    # The unit is dropped from the low end only when it is the same one: a
    # build whose smallest write is a few hundred bytes and whose largest is a
    # megabyte would otherwise read as starting at 0.000.
    low_size, low_unit = low.split()
    return "%s-%s chunks" % (low_size if low_unit == high.split()[1] else low,
                             high)


def _line_detail(line, kind, room):
    """What a line says past its bytes, in as much as the rule leaves room for.

    Two things are worth saying: what size chunks the calls carried, and how
    many bytes crossed a second time. The first says whether a bigger window
    would help -- a row of window-sized chunks is at its ceiling, one of
    dribbles is not -- and the second says work was done twice for one result.

    Dropped from the least useful end when they do not fit, rather than
    wrapped: these are an aside to a figure, and an aside that takes two lines
    is no longer one.
    """
    clauses = []
    if line.calls > 1:
        clauses.append(_chunks(line.smallest, line.largest))
    if line.again:
        # The bytes that crossed a second time, not the calls that carried
        # them: one read of a window already fetched re-reads all of it, one
        # overlapping the tail of it re-reads only the overlap.
        clauses.append("%s %s" % (_mib(line.again),
                                  "re-written" if kind == "writes"
                                  else "re-read"))

    while clauses and len("%12s  (%s)" % ("", ", ".join(clauses))) > room:
        clauses.pop(0)

    return "%12s%s" % (_mib(line.bytes),
                       "  (%s)" % ", ".join(clauses) if clauses else "")


def print_result(result):
    """Everything a finished run came to, in the order it is read.

    One call, because which parts a report has and what order they come in is
    a question about the report. run knows when there is a result; it does not
    need to know that a romfs is described after the calls that built it.
    """
    print()
    _print_time(result.totals.seconds, result.stubs,
                getattr(result, "run_seconds", None))
    _print_memory(result, result.totals.peak)
    _print_fs(result)
    _print_romfs(result)
    _print_diagnostics(result)


class _Served:
    """One read of a whole region, and what answering it took.

    A region served in one piece crosses to the fs once; one served in many
    crosses per piece. Asked for in a single call each, so what comes back is
    the build's own shape and not how this side cuts its reads.

    fetched short of asked is the rest answered without crossing at all, which
    a count of crossings alone reads as free.
    """

    HEADS = ("asked", "fetched", "fs calls")
    ROW = "    %-17s  %13s  %13s  %8s"
    HEAD = "  %-19s  %13s  %13s  %8s"

    def __init__(self, regions):
        self._regions = regions

    def __bool__(self):
        return bool(self._regions)

    def write(self):
        print()
        print(self.HEAD % (("Serving at runtime:",) + self.HEADS))
        for r in self._regions:
            print(self.ROW % (r.name, _mib(r.asked), _mib(r.moved), r.calls))


def _print_romfs(result):
    """The romfs that came out, described rather than judged.

    Read off the result as a Description rather than passed as its four
    fields: two of them are digests and two are numbers, so a pair swapped
    on the way in would print something that looks entirely reasonable.

    A romfs no game could read is reported here rather than in place of all
    this: the build ran to the end, and what it spent is what a bad romfs is
    diagnosed from. It gets the reason it failed and nothing further -- there is
    no version to give it, and no figures worth taking off a romfs like that.
    """

    _part("Romfs result :")

    print()

    described = result.described
    if result.corrupt is not None:
        print("  /!\\ fs.mitm did not build a valid romfs /!\\")

        # The check knows which entry, in which bucket, and how it failed to be
        # reachable, and that is the whole of what a reader wants. Only one kind
        # of exception arrives here and its message is written to be read, so it
        # is printed as it is rather than named by its type.
        print()
        print("    %s" % result.corrupt)
        return

    # Which version of the game beside what came out of it, since that is what
    # it came out of: the version is read off what ncm reports installed and
    # seeds the tree, so two runs whose digests differ can differ for this
    # reason alone. Not in the header, because it is not a premise the run was
    # given.
    # The size the files come to is not among them. It is the base's, near
    # enough, whatever a build does with it -- so it says how big a game was
    # asked for rather than anything about the build that served it.
    print("  Built: %d files, for game at version %s"
          % (described.files, result.version))

    # Two tables between what was built and how it hashes, set off from both
    # by a blank of their own: what the romfs is, then what using it costs,
    # then what it comes to.
    print()
    lookups = _Lookups(described)
    if lookups:
        lookups.write()
    served = _Served(getattr(result, "fetched", ()))
    if served:
        served.write()
    print()

    # Both name their fields, because "Digest" alone reads as a hash of the
    # romfs image and neither is that -- no byte of the container is hashed by
    # either, only the entry list walked out of the tables. The second drops
    # the offsets: a build that recompacts the file partition and one that
    # inherits the base romfs's layout serve the same filesystem while
    # differing on every offset, so that is the line two builds must agree on.
    # What it holds, then how it hashes: a break where the subject changes,
    # and none between the digests themselves, which are one thought said twice.
    print()
    print("  Digest (sha-256: paths, sizes, and offsets):")
    print("    %s" % described.digest)
    print("  Digest (sha-256: paths and sizes):")
    print("    %s" % described.content_digest)


def _print_diagnostics(result):
    """What the build said on the way, and what it reached for and did not get.

    Each list prints only when it has entries: a healthy launch says nothing
    here, and a heading with nothing under it reads as something missing.
    """
    _print_list("assertions that fired", result.assertions)
    _print_list("debug output from the build", result.debug)

    # Last, whether or not any of them had anything to say: keeps the shell
    # prompt off the report.
    print()


def _failing_module(e):
    """Which module could not do what it was asked.

    The innermost of ours: one that raised inside a library it called answers
    for it, having chosen the call. Everything outside passed the object on.
    """
    ours = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules")
    name = "run"
    for frame in traceback.extract_tb(e.__traceback__):
        if os.path.dirname(os.path.abspath(frame.filename)) == ours:
            name = os.path.basename(frame.filename)[:-len(".py")]
    return name


def print_module_failure(e, reason=None):
    """A module refusing: which one, what it raised, and what it said.

    A reason is passed where the caller knows what was being attempted;
    otherwise the exception speaks for itself.

    The traceback follows the summary rather than replacing it: the three
    lines say what happened, and the frames say where to go and read.
    """
    print()
    _part("FAILED :")
    print()
    print("  Module : %s" % _failing_module(e))
    print("  Error  : %s" % e.__class__.__name__)
    print("  Reason : %s" % (reason if reason is not None else e))
    print()

    # To stdout, so a redirected run keeps it with the report it belongs to.
    traceback.print_exc(file=sys.stdout)
    print()


def print_bug(e):
    """Something that is ours rather than an answer about the build.

    The traceback first and the statement under it: the frames are what there
    is to act on, and the heading says which of the two kinds of failure this
    is. stdout is block-buffered when redirected, so without the flush the
    frames land above the header they follow.
    """
    sys.stdout.flush()
    traceback.print_exc(file=sys.stdout)

    print()
    _part("FAILED :")
    print()
    print("  %s" % e)
    print()


def print_failure(result, e):
    """A launch that stopped early: what it said, and where things stood.

    The sections a finished run gets, less the ones a failure cannot fill --
    there are no calls to break down and no romfs to describe.

    What the build said is kept, and it is the reason to read this at all: an
    assertion that fired names what the build refused to do, where the abort
    only says where it gave up.
    """
    print()
    _part("FAILED :")
    print()
    print("  %s" % e)

    print()
    _print_memory(result)
    _report_written(result.written)
    print()
    _print_diagnostics(result)


class _Lookups:
    """What finding an entry by name costs, a row per kind of entry.

    A name is hashed into a bucket and the chain from it followed, so what a
    lookup pays is the entries it looks at, and how many share a bucket decides
    that. One per lookup is a table sized for what it holds.

    The rows under files are the same files split by what the mod did to each:
    left alone, replaced, or new. Where a build puts an entry it is given is
    its own decision, and the three rows are where that decision shows.
    """

    # count, not entries: entries is the unit the third column is in, and one
    # word cannot be both the population and the unit.
    HEADS = ("count", "buckets", "entries per lookup (avg)")

    # Where a rate's digits stop inside its column. Its heading is long and the
    # figure short, so ranged to the far edge it sits a column from its row.
    RATE_AT = 11

    # A name two in and fourteen wide, or four in and twelve, comes to the same
    # sixteen -- so the headings stand over the columns they head. A split row
    # goes two further in and two narrower, and keeps the column.
    HEAD = "  %%-14s  %%%ds  %%%ds  %%s"
    ROW = "    %%-12s  %%%ds  %%%ds  %%s"
    SIDE = "      %%-10s  %%%ds  %%%ds  %%s"

    # A sentence rather than a row: every figure in the table is per lookup and
    # this is per file, so a fourth row would put a different denominator in the
    # same column. By path because it is not the only way in -- an entry reached
    # by id costs no lookups, one reached by walking a directory costs no hash.
    OPEN = "    opening one file by path: %s entries examined on average"

    # As the checker gives them, so an empty one keeps its place and the names
    # stay against their rows. Printed the other way round: what was there, what
    # was changed, what was put in.
    SIDE_NAMES = ("added", "overridden", "base")

    def __init__(self, described):
        self._rows = [("files", described.file_lookups),
                      ("directories", described.dir_lookups)]
        self._sides = getattr(described, "sides", None) or ()
        self._opens = getattr(described, "opens", None)

    def __bool__(self):
        return any(look and look.entries for _name, look in self._rows)

    def _formats(self):
        """The three formats, sized from every figure that will be printed."""
        shown = [look for _name, look in self._rows if look and look.entries]
        entries = max([len(str(look.entries)) for look in shown]
                      + [len(str(s.entries)) for s in self._sides
                         if s and s.entries])
        buckets = max(len(str(look.buckets)) for look in shown)

        # Each column at least as wide as its own heading.
        e = max(entries, len(self.HEADS[0]))
        b = max(buckets, len(self.HEADS[1]))
        return (self.HEAD % (e, b), self.ROW % (e, b), self.SIDE % (e, b))

    def _rate(self, look):
        return ("%.2f" % look.each()).rjust(self.RATE_AT)

    def write(self):
        head, row, side = self._formats()

        print()
        print(head % (("Lookups:",) + self.HEADS))
        for name, look in self._rows:
            if look is None or not look.entries:
                continue
            print(row % (name, look.entries, look.buckets, self._rate(look)))
            if name == "files":
                self._write_sides(side)

        # One blank after the kinds: the split already steps in, and what needs
        # setting off is the figure below, which is not per lookup at all.
        print()
        if self._opens and self._opens.files:
            print(self.OPEN % ("%.2f" % self._opens.each()))

    def _write_sides(self, side):
        for name, look in reversed(list(zip(self.SIDE_NAMES, self._sides))):
            if look and look.entries:
                # No buckets: a side shares the row's above rather than having
                # any, and the same figure four times reads as four findings.
                print(side % (name, look.entries, "", self._rate(look)))
