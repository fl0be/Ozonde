#!/usr/bin/env python3
"""What an ams.mitm build costs to serve a modded game its romfs: fs* calls,
bytes read and written, write amplification, and peak memory.

--help starts here.

Usage: <python> run.py <ams_mitm.elf> <x> [--patched]
       [--loose o=N,a=N] [--bin o=N,a=N] [--dyn-heap a=N,s=N] [--fresh-sd]

Arguments:
  <ams_mitm.elf>          the build to measure.

  <x>                     how many files the base romfs holds.
                          Range: 1 to <x max>.

Options:
  --patched               the game has a fixed update installed.
                          The generated romfs changes slightly with it.

  --loose o=N,a=N         a mod, as loose files under romfs/.
                          o is how many files it overrides, a how many it adds.
                          Range: 0 to <x max> for both.
                          Default o=1,a=0, or o=0,a=0 if --bin is named.

  --bin o=N,a=N           a mod, packed into romfs.bin.
                          o is how many files it overrides, a how many it adds.
                          Range: 0 to <x max> for both. Default o=0,a=0.

  Given both, the two take different files: what one overrides the other does
  not, and what one adds the other does not, so their counts add up. Between
  them they cannot override more than x.

  One overridden file is the least that still makes fs.mitm build.

  --dyn-heap a=N,s=N      the dynamic heap a build may take.
                          a is how many MiB it takes from the application
                          pool, s how many from the system pool.
                          Range: 0 to <heap max> for both. Default a=<heap default>,s=0.

  --fresh-sd              empty sdmc/ first (except the ams config file), for
                          a genuinely cold run. Without it sdmc/ persists
                          between runs, as a real SD does.

"""
import collections
import os
import sys

# Python 3.8 or newer, checked here rather than left to fail somewhere odd.
# What the code genuinely needs is lower than that: dicts only guarantee
# insertion order from 3.7, which the generated game's determinism rests on,
# and Unicorn 2 has no Python 2 build at all -- Python 2 would not merely fail,
# since bytes(0x10) is sixteen zero bytes here and the string "16" there, which
# a stub would write into guest memory without complaining. 3.8 is where this
# is actually run and tested, so it is the floor that gets stated.
if sys.version_info < (3, 8):
    raise SystemExit("Python 3.8 or newer is required; this is %s"
                     % sys.version.split()[0])

# The harness lives in modules/ and run.py is the only program, so the import
# path is set here rather than in each of them. Absolute, from this file rather
# than the working directory, so a run from anywhere finds the same modules --
# and first on the path, so a stray bench.py elsewhere cannot win.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "modules"))

import bench
import report


# The docstring is two documents in one: why the harness is built this way, for
# anyone opening the file, and how to drive it, for anyone typing --help. Only
# the second is worth a terminal, so it is marked off rather than split into a
# second copy that would drift from this one.
HELP_MARK = "--help starts here.\n"


def usage():
    """Everything from the mark on -- what --help is for.

    The default tree size is substituted rather than written into the text,
    for the same reason it is derived from the ceiling: one edit to BASE_MAX
    moves the number, and nothing is left quoting the old one.
    """
    text = __doc__.split(HELP_MARK, 1)[-1].strip("\n")
    for mark, value in (("<x max>", BASE_MAX),
                        ("<heap default>", bench.HEAP_GRANT_DEFAULT_MB),
                        ("<heap max>", bench.HEAP_GRANT_MAX_MB)):
        text = text.replace(mark, str(value))
    return text


# ----------------------------------------------------------------------------
# The command line
# ----------------------------------------------------------------------------

# Upper bound on the generated tree, and on any count derived from it, so a
# mistyped argument fails at once rather than long into a run.
BASE_MAX = 500000

MOD_KEYS = {"o": "overridden", "a": "added"}

# The two ways a mod reaches the build, each with its own option. They coexist:
# fs.mitm adds the loose SD tree, then romfs.bin, then the game, so a path in
# more than one is served by the earliest. In that order, which is the order a
# refusal names them in.
MOD_DELIVERIES = ("--loose", "--bin")

# What a run means by "a mod" when it is not told: one overridden file, loose.
# A build costs what the game contains, not what the mod does, so a bigger
# default buys no coverage. It applies only when neither delivery is named:
# --bin alone means the mod is the packed one, and a loose file nobody asked
# for would put a second source live.
MOD_DEFAULT = (1, 0)
NO_MOD = (0, 0)

# Every option that takes a value, which is what the argument loop must step
# over. One set rather than a test per option: a flag listed here, or a valued
# option left out, is how an option comes to swallow the one after it.
VALUED = MOD_DELIVERIES + ("--dyn-heap",)

# Asking for help is not a mistake, so it prints to stdout and exits 0, and it
# means the same wherever it appears -- including as the value of an option,
# where it would otherwise be swallowed.
HELP_FLAGS = ("--help", "-h")


def refuse(flag, text, why):
    """A mistake, said in one line: what was written, and what is wrong with it."""
    raise SystemExit("%s %r is not valid: %s" % (flag, text, why))


def whole(flag, text, lo, hi):
    """A whole number inside a range, or one refusal covering every way to miss.

    Not a word, not a fraction, not out of range: all the same answer, because
    the reader needs what to type next rather than a diagnosis of what they
    typed. One rule in one place, so every option accepts and prints its range
    the same way.
    """
    try:
        n = int(text.strip(), 0)
    except ValueError:
        n = None
    if n is None or not lo <= n <= hi:
        refuse(flag, text, "must be an integer between %d and %d" % (lo, hi))
    return n


def parse_keys(flag, text, allowed, lo, hi, form):
    """{key: int} from `k=N,k=N`, or the one refusal that covers mistyping it.

    Every key, once each, each an integer in range: a wrong key, a missing one,
    a repeated one and a value out of range all get the same answer, which is
    the correct form -- which is what helps on the mistake nobody can see,
    since o=5,a=5,o=6 looks fine.
    """
    out = {}

    for part in text.split(","):
        key, _, value = part.partition("=")
        key = key.strip()

        try:
            number = int(value.strip(), 0)
        except ValueError:
            number = None

        if (key not in allowed or key in out or number is None
                or not lo <= number <= hi):
            refuse(flag, text, "must be %s, each an integer between %d and %d"
                   % (form, lo, hi))
        out[key] = number

    # Counted rather than checked key by key: a missing one leaves it short.
    if len(out) != len(allowed):
        refuse(flag, text, "must be %s, each an integer between %d and %d"
               % (form, lo, hi))
    return out


def resolve_dyn_heap(text):
    """Megabytes the injected table row grants, application and system.

    Capped a half at a time, since the pools are separate and neither bounds
    the other. What each half is, and why 0,0 is a real answer, is under
    --dyn-heap above.
    """
    if text is None:
        return {"a": bench.HEAP_GRANT_DEFAULT_MB, "s": 0}
    return parse_keys("--dyn-heap", text, ("a", "s"), 0,
                      bench.HEAP_GRANT_MAX_MB, "a=N,s=N")


def resolve_base(text):
    """How many files the base romfs holds, parsed and checked."""
    return whole("<x>", text, 1, BASE_MAX)


def resolve_mod(flag, text, default):
    """One delivery's counts: parsed, defaulted and range-checked.

    Left out entirely, a delivery carries what `default` says -- which the
    caller decides, because what a missing --loose means depends on whether
    --bin was named. That is the only rule here a delivery cannot state alone.
    """
    if text is None:
        return {"overridden": default[0], "added": default[1]}

    # Both keys or neither, and since the option itself is optional, neither
    # means not writing it. One key alone would have to mean "and none of the
    # other", which reads like it was simply not mentioned -- so parse_keys
    # refuses a partial spelling along with every other way of mistyping one.
    return dict((MOD_KEYS[short], number) for short, number
                in parse_keys(flag, text, MOD_KEYS, 0, BASE_MAX,
                              "o=N,a=N").items())


def check_mods(base, mods, named):
    """The rules the two deliveries share, which neither can check alone."""
    # Summed, not each alone: the deliveries override disjoint files, so
    # between them they cannot shadow more than the game holds.
    total_over = sum(m["overridden"] for m in mods.values())

    if total_over > base:
        flag = named[0] if named else "--loose"
        refuse(flag, "o=%d" % mods[flag]["overridden"],
               "an overridden file shadows one the game has, and the two "
               "deliveries override %d files between them, more than x (%d)"
               % (total_over, base))
    if sum(m["overridden"] + m["added"] for m in mods.values()) == 0:
        # Blamed on an option that was actually typed. Reaching zero takes at
        # least one of them, since naming neither is a mod of one file -- and
        # being told that --loose is wrong when only --bin was written sends
        # the reader looking for a mistake they did not make.
        flag = named[0]
        refuse(flag, "o=%d,a=%d" % (mods[flag]["overridden"], mods[flag]["added"]),
               "that leaves no mod at all, and nothing to measure")


def value_after(argv, i, flag):
    """The argument following a flag, or a clear refusal.

    Reaching past the end of argv is otherwise a traceback, which tells the
    user nothing about which option they left dangling.
    """
    if i + 1 >= len(argv):
        raise SystemExit("%s needs a value" % flag)
    return argv[i + 1]


# What a run was asked for: the build, the tree, the mod, the heap, and what
# it does to the SD. Every field is a premise the header states, and none of
# it is anything the harness decides once it is running.
#
# romfs and heap are None until the arguments are read, since a command line
# with no build named has nothing to say about either.
Premises = collections.namedtuple(
    "Premises", "build romfs heap patched update_hidden fresh_sd sdmc")


def parse_args(argv):
    opts = {"build": None, "romfs": None, "heap": None, "fresh_sd": False,
            "patched": False, "update_hidden": False, "sdmc": bench.SDMC_DIR}

    valued = {flag: None for flag in VALUED}
    positional = []
    seen = set()
    i = 1
    while i < len(argv):
        a = argv[i]

        # Refused rather than resolved: taking the last is a guess at which was
        # meant. --help is exempt, printing and exiting before a second matters.
        if a.startswith("-") and a not in HELP_FLAGS:
            if a in seen:
                raise SystemExit("%s given twice" % a)
            seen.add(a)

        if a in HELP_FLAGS:
            print(usage())
            raise SystemExit(0)

        elif a in VALUED:
            # The only options that step over an argument of their own. A
            # flag doing it too would eat what followed, and a swallowed
            # --fresh-sd runs warm without saying so.
            text = value_after(argv, i, a)
            if text in HELP_FLAGS:
                print(usage())
                raise SystemExit(0)
            valued[a] = text
            i += 1

        elif a == "--patched":
            opts["patched"] = True
        elif a == "--fresh-sd":
            opts["fresh_sd"] = True
        elif a.startswith("-"):
            raise SystemExit("unknown option %r" % a)
        else:
            positional.append(a)

        i += 1
    if len(positional) > 2:
        raise SystemExit("expected <ams_mitm.elf> <x>, got %d: %s"
                         % (len(positional), ", ".join(positional)))
    if positional and not os.path.isfile(positional[0]):
        raise SystemExit("no such ams_mitm.elf: %r" % positional[0])

    # Asked for rather than assumed: a run is only comparable against another
    # of the same size, and a default would leave that unsaid on both.
    if len(positional) == 1:
        raise SystemExit("<x> is required: how many files the base romfs holds")

    opts["build"] = positional[0] if positional else None
    if opts["build"] is None:
        return Premises(**opts)

    base = resolve_base(positional[1])

    # The default mod applies only when no delivery was named at all. Name one
    # and the other carries nothing: --bin alone means the mod is the packed
    # one, not the packed one plus a loose file nobody asked for.
    named = [f for f in MOD_DELIVERIES if valued[f] is not None]
    mods = {
        "--loose": resolve_mod("--loose", valued["--loose"],
                               NO_MOD if named else MOD_DEFAULT),
        "--bin": resolve_mod("--bin", valued["--bin"], NO_MOD),
    }
    opts["heap"] = resolve_dyn_heap(valued["--dyn-heap"])

    # Checked together, because what makes a mod legal is a fact about both
    # deliveries at once: either alone can be empty, but not both.
    check_mods(base, mods, named)
    opts["romfs"] = {"base": base, "loose": mods["--loose"], "bin": mods["--bin"]}

    # An update rewrites files of the game, and a mod overriding every one of
    # them serves its own file at every path, so the rewrite reaches nothing.
    # Dropped rather than honoured: the header states what was measured.
    if opts["patched"] and sum(m["overridden"] for m in mods.values()) == base:
        opts["patched"] = False
        opts["update_hidden"] = True
    return Premises(**opts)


def confirm():
    """A chance to stop, after the premises and before anything is touched.

    Only asked when someone is there to answer. A run whose output is piped or
    redirected -- a suite driving run.py, a report being captured -- has no one
    at the other end, and a question nobody sees would hang it.

    Anything but a plain no goes ahead, so the answer a reader gives by
    pressing enter is the one they wanted.

    The question is rubbed out once it is answered, so what stays on the
    terminal is what a piped run would have written -- a reader pasting the
    report has nothing to cut out of it. Up one line and clear it, the cursor
    having moved below the prompt with the enter.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    try:
        answer = input("  Continue? [Y/n] ").strip().lower()
    except EOFError:
        return
    if answer.startswith("n"):
        raise SystemExit(0)
    sys.stdout.write("\033[F\033[K")
    sys.stdout.flush()


def run_one(guest, premises):
    """Benchmark one ELF, or None if it failed -- in which case the failure has
    already been reported."""
    report.print_header(premises)
    confirm()

    # After the question, so the SD the header described is still the SD on
    # disk when the harness reads it.
    #
    # Named, not positional: patched and fresh_sd are both bools, and
    # transposing them would wipe the SD and report the run under a premise
    # it was not measured with.
    h = bench.Harness(guest, premises.romfs,
                      patched=premises.patched,
                      app_heap_mb=premises.heap["a"],
                      sys_heap_mb=premises.heap["s"],
                      sdmc_dir=premises.sdmc,
                      fresh_sd=premises.fresh_sd)

    try:
        h.launch()

    except bench.GUEST_FAILURES as e:
        # The build aborted or faulted, which is an answer about the build. The
        # lines printed above already say where; a traceback would name bench
        # and unicorn, which are only the path the failure travelled.
        report.print_failure(h.result(), e)
        return None

    except bench.MODULE_FAILURES as e:
        # A module refusing, which its message says in full: what it could not
        # use, or could not make. Nothing was measured, so nothing is reported.
        report.print_module_failure(e)
        return None

    except Exception as e:
        # Anything else is a bug rather than an answer, and says so with the
        # frames to go and read.
        report.print_bug(e)
        return None

    # Read back after the launch, never inside it: the walk is the length of
    # the romfs, and it is not what the build was charged for.
    try:
        described, corrupt = h.read_back(), None
    except bench.ROMFS_FAILURES as e:
        described, corrupt = None, e

    # One value, taken once. Everything below reads it and nothing reaches
    # back into the run -- which is also where the SD is flushed, so printing
    # cannot be what writes to an SD.
    result = h.result(described, corrupt)

    report.print_result(result)
    return None if corrupt is not None else True


def main():
    premises = parse_args(sys.argv)
    if not premises.build:
        print(usage())
        return 1

    # Load the binary first: confirm it is an ams.mitm, and let boundaries
    # derive a stub for every service call it makes that this sandbox does not
    # provide. Failing here says so plainly, before a 20-second run, instead of
    # dying inside a hook.
    try:
        guest = bench.load(premises.build)
    except bench.MODULE_FAILURES as e:
        report.print_module_failure(
            e, "could not parse %s: %s" % (premises.build, e))
        return 1

    # Reading the result back is outside the launch, and a module can refuse
    # there too. Caught here so it reads the same wherever it happened.
    try:
        finished = run_one(guest, premises)
    except bench.MODULE_FAILURES as e:
        report.print_module_failure(e)
        return 1

    return 0 if finished is not None else 1


if __name__ == "__main__":
    sys.exit(main())
