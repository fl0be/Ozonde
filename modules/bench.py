#!/usr/bin/env python3
"""What is known about ams.mitm, and the launch it measures.

Both live here on purpose. The symbol names, the SD path convention and what a
launch consists of are the description of the target, and the launch is written
straight against them -- there is no profile to write and nothing to configure.
Only stock symbols are named, so any ams_mitm.elf runs as-is.

Two things are measured, both exact rather than sampled: calls across the fs*
boundary, which reach fsp-srv on hardware, and the memory the module takes,
read back from its own heaps. A phase's duration is recorded alongside them,
but it is host CPU spent running the emulator rather than time on a console,
and is reported as such.

What the run came to leaves here as a Result: one value, taken once the run
is over, holding every figure and nothing that can still change. Whoever
prints it cannot reach back into the launch -- which is also why the SD is
flushed here, at the end of a run, rather than by whatever says so.
"""
import collections
import os
import struct
import time
from elftools.common.exceptions import ELFError

import boundaries
import fake_meta
import fake_romfs
import fake_services
import hos_memory
import progress
import romfs_check
import romfs_format
import romfs_pack
import accounting

from fake_sd import SETTINGS_INI, FakeSD
from guest import FAILURES, Guest

# Guest address space to allocate from, not a tuning knob: what a stock build
# asks for is flat across tree sizes, since the base romfs is served from the
# host rather than copied in. The mapping is lazy, so raising it costs no host
# RAM, and running out aborts rather than corrupting a measurement.
HEAP_MB = 512

# The two ways a launch fails that are the build's doing rather than ours,
# under the name a caller of this module looks for.
GUEST_FAILURES = FAILURES

# Where a console's SD would be. A run given this keeps what it wrote and
# finds it again next time, which is how a build that caches its own output
# has to be measured. Given None -- the default -- nothing is read or written,
# so a run that never asked for an SD cannot inherit somebody else's.
SDMC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sdmc")


# And the way reading the result back fails, named here for the same
# reason: a romfs the build could not have served is the build's
# answer, not a bug in whoever is printing it.
ROMFS_FAILURES = (romfs_check.RomfsCorrupt,)

# A module saying it cannot do what it was asked: an object handed to it that
# it cannot use, a file that is not the binary it was told to read, an object
# it cannot produce. Its message is the diagnosis, and a traceback would only
# say who noticed. Anything not named here is a bug rather than a refusal.
MODULE_FAILURES = (AssertionError, ELFError, FileNotFoundError, IndexError,
                   KeyError, MemoryError, ValueError, romfs_pack.BadFakeRomfs)


# What a run came to, once it is over: every figure a report needs and nothing
# that can still change. Assembled by the harness, since only it knows how the
# pieces are come by; read by whoever prints, which is why none of it is a
# live object.
#
# A launch that failed fills what it can -- the pools it touched and what
# reached the SD -- and leaves the rest None. There is no half-answer about a
# romfs that was never built.
Result = collections.namedtuple(
    "Result", "totals kinds pools written version described corrupt "
              "stubs assertions debug contents")


# ----------------------------------------------------------------------------
# What is known about ams.mitm
# ----------------------------------------------------------------------------

# Symbols that must all be present for this to be the binary we think it is.
IDENTITY = ("romfs::Builder::Build", "mitm::fs::", "OpenGlobalSdCardFileSystem")

# Everything under this prefix is the module under test, and is never stubbed:
# stubbing the target would measure the stand-ins instead of the build.
MODULE_PREFIX = "ams::mitm::"

# Namespaces that talk to other processes, and so get refused. The ones served
# for real are deliberately absent -- fs, os, mem, and ncm, whose answer about
# an installed update is a premise of the run rather than a call to refuse.
SERVICE_NAMESPACES = ("ams::pm::", "ams::spl::", "ams::sm::",
                      "ams::lr::", "ams::nim::", "ams::fatal::", "ams::updater::",
                      "ams::exosphere::", "ams::emummc::", "ams::settings::",
                      "ams::psc::", "ams::pgl::", "ams::erpt::")

# The entry points a launch is driven through. To call a function you must know
# its name and its arguments; that is the contract the module publishes, not an
# implementation detail, and benchmarking it without knowing it is impossible.
SYM = {
    "init_sd":        "OpenGlobalSdCardFileSystem",

    # The launch itself: GetLayeredRomfsStorage is what a game asking for its
    # romfs reaches, InitializeImpl the work that follows. The hand-off to the
    # worker thread is hooked rather than called, since its argument is the
    # storage to drive. Two names for it because which survives depends on
    # inlining; whichever exists carries the same pointer.
    "get_layered":    "fs::GetLayeredRomfsStorage(ams::ncm::ProgramId, FsStorage&, bool)",
    "begin_init":     "fs::LayeredRomfsStorageImpl::BeginInitialize()",
    "request_init":   "fs::(anonymous namespace)::RequestInitializeStorage",
    "initialize":     "fs::LayeredRomfsStorageImpl::InitializeImpl()",

    # Reading the finished romfs back, the way a game does. Used by the
    # verifier, never by a measured phase.
    "layered_read":   "fs::LayeredRomfsStorageImpl::Read(long, void*, unsigned long)",

    # What the module's own startup would have initialised before any of the
    # above is reachable. Optional: a build that does not use the service has
    # no such symbol, and nothing here needs it to exist.
    "ncm_init":       "ams::ncm::Initialize()",

    "sd_ready":       "mitm::IsInitialized()",

    # Disabled at startup on a console, so an fs Result is returned rather
    # than aborted on.
    "fs_auto_abort":  "fs::SetEnabledAutoAbort(bool)",
    "configure_heap": "romfs::ConfigureDynamicHeap",
}


def phase_name(key):
    """The report's row for a phase: the function, without its namespace or
    arguments. Derived rather than restated, so a row is always the name of
    something this run actually called."""
    return SYM[key].split("(")[0].rsplit("::", 1)[-1]


# ----------------------------------------------------------------------------
# The title it measures
# ----------------------------------------------------------------------------

# The benchmark's own title, invented and fixed: it names the SD directory
# written to, so a log says these numbers came from a benchmark rather than
# from a game someone owns. It ties the heap table row, the SD's contents
# directory and ncm's answers to one game.
#
# Shaped as a game's id is: inside the range games are assigned from, and a
# multiple of 0x2000, which leaves the ids above it free for the update and
# the add-on content.
DEFAULT_PROGRAM_ID = 0x0100BADC0FFEE000

# And the release its update is at, when a run has one: what a run varies is
# whether the title is patched, never which patch, so the level is settled
# here rather than asked for.
DEFAULT_UPDATE_LEVEL = 6

# Written into ApplicationsWithDynamicHeap[] for our invented id, so a stock
# build grants it the heap it would give no unknown title -- without a row it
# gets only its static buffer, and aborts once a tree outgrows it.
#
# 22 MiB sits just above the largest real entry; 0 from system, because the
# application pool is the one a game competes for and taking from system would
# flatter a build that leans on it. Capped at 32: past that the result is about
# memory no console would have given.
HEAP_GRANT_DEFAULT_MB = 22
HEAP_GRANT_MAX_MB = 32

# Any row of the stock table, used only to find where the table sits. TOTK's,
# because the tree a run generates is shaped after that game.
_TABLE_PROBE_ID = 0x0100F2C0115B6000


def sd_romfs_root(program_id):
    return "/atmosphere/contents/%016x/romfs" % program_id


# Read in pieces rather than whole: the image is tens of megabytes and this
# needs eight bytes of it -- a copy of the whole thing on the host was the
# harness's largest single allocation, for a search that does not need one.
def grant_dynamic_heap(guest, program_id, app_mb=HEAP_GRANT_DEFAULT_MB,
                       sys_mb=0):
    """Put program_id in the module's own heap table, if it keeps one.

    The table is constexpr in an anonymous namespace, so it exports no symbol
    to hook -- but it is plain data, six rows of (program_id, application
    bytes, system bytes), read at launch to decide what to take. Writing a row
    is the only way to answer it, and the one place this edits the module
    rather than answering it. The run says so in its header.

    Returns the granted (application, system) bytes, or None for a build with
    no table.

    Asked twice, it answers the same, and it reads the answer back out of the
    guest rather than remembering it: writing the row is what removes the
    probe id it was found by, but the id written in its place is as findable
    as the one it replaced. Nothing here is kept beside the memory that holds
    it.

    sys_mb defaults to 0, as every entry in Atmosphere's own table does but
    TOTK's. The halves are separate regions, not one budget rearranged.
    """
    ours = guest.find_bytes(struct.pack("<Q", program_id))
    if ours is not None:
        app, sysmem = struct.unpack("<QQ", guest.read(ours + 8, 16))
        return app, sysmem

    at = guest.find_bytes(struct.pack("<Q", _TABLE_PROBE_ID))
    if at is None:
        return None

    app, sysmem = app_mb * 1024 * 1024, sys_mb * 1024 * 1024
    guest.write(at, struct.pack("<QQQ", program_id, app, sysmem))
    return app, sysmem


# ----------------------------------------------------------------------------
# Loading one
# ----------------------------------------------------------------------------

class ProducedRomfs:
    """The romfs a launch built, as something that can be read and compared.

    What verifies a romfs needs two things: its bytes, fetched the way a game
    fetches them, and what the SD holds at a path so the two can be checked
    against each other. Both are this module's business -- one is a call into
    the build, the other a lookup on the fake SD -- so the verifier is handed
    this rather than the harness they live in.
    """

    __slots__ = ("_harness",)

    def __init__(self, harness):
        self._harness = harness

    def read(self, offset, size, chunk=0x10000):
        """Bytes from the virtual romfs, fetched through the module itself.

        The chunk size barely matters: the cost is emulating the module read
        path per byte, not the number of times it is entered.
        """
        if size <= 0:
            return b""

        h = self._harness
        out = bytearray()
        buf = h.guest.alloc(min(size, chunk), align=0x10)
        while len(out) < size:
            n = min(chunk, size - len(out))
            h.call("layered_read", args=(h.layered, offset + len(out), buf, n))
            out += h.guest.read(buf, n)
        return bytes(out)

    def from_packed(self, word):
        """Was that word served by the packed mod rather than by the game?

        The verifier reads bytes back and has to say which source they came
        from; which sources there are is the run's business, so it asks here.
        """
        return self._harness.modded.from_packed(word)

    def mod_paths(self):
        """The romfs paths this run's mod delivers, by both deliveries.

        A verifier sampling its way to one almost never gets there: at a full
        base the mod is a handful of paths among three hundred thousand. Which
        paths they are is the run's business rather than something to be found,
        so it says, and they are checked because they are the mod's.
        """
        modded = self._harness.modded
        return [path for mod in (modded.loose, modded.packed)
                for path, _size in mod.files()]

    def mod_bytes(self, path, size):
        """What the SD holds at that romfs path, or None if it holds nothing.

        Asked of the node the lookup found, rather than of a generator that
        ought to match it -- which is what makes a check "read back what was
        written". It also answers for a file whose tail comes from a provider,
        which asking for sparse content directly would get wrong.
        """
        h = self._harness
        sd_path = h.sd_romfs_root() + path
        node = h.sd.find(sd_path)
        if node is None or node.is_dir:
            return None
        return node.bytes_past(sd_path, 0, size)


def require_target(guest):
    """Fail loudly, and early, if this is not the binary the harness expects.

    Worth doing before anything is hooked: every later step assumes these
    symbols exist, and a clear message here beats a confusing failure later.
    """
    missing = [w for w in IDENTITY if not guest.find(w)]
    if missing:
        raise SystemExit("%s is not an ams.mitm build: no %s"
                         % (guest.elf_path, ", ".join(missing)))


def load(elf_path):
    """A Guest for elf_path, confirmed to be the binary the target describes.

    Separate from Harness so a caller naming several ELFs can have them all
    rejected before any is run -- and so the loading work, which includes a
    subprocess for the symbol table, happens once per ELF.
    """
    guest = Guest(elf_path, heap_mb=HEAP_MB)
    require_target(guest)
    return guest


# ----------------------------------------------------------------------------
# A launch, and what it cost
# ----------------------------------------------------------------------------

class Phase:
    """One measured region of a run."""

    def __init__(self, name):
        self.name = name
        self.fs_calls = 0
        self.calls = {}         # entry point -> times called, this phase only
        self.read = 0           # bytes read across the fs boundary
        self.written = 0        # bytes written -- the half that wears an SD
        self.touched = 0        # heap pages the module has written by now
        self.regions = ()       # (label, size, touched) as this phase ended
        self.ranges = ()        # where this phase's bytes crossed
        self.touches = ()       # and which files it named without moving any
        self.seconds = 0.0      # wall clock, emulator not console (see report)


def _settings_on(sd):
    """{(name, key): bytes} for what the SD's system_settings.ini says.

    Read off the SD rather than the disk under it, so a build is answered from
    what the guest would find. The file is the whole store: a console seeds
    ams's own defaults first and nothing here does, so an item the file does
    not set is unset.

    An item nobody can read is dropped and the rest stand. A console is
    stricter -- its parser stops at the first bad line and the boot aborts --
    but a bad line here is a typo in a benchmark's own input.
    """
    node = sd.find(SETTINGS_INI)
    if node is None or node.is_dir:
        return {}

    out = {}
    for item, text in fake_services.settings_from_ini(
            bytes(node.data).decode("utf-8", "replace")).items():
        try:
            out[item] = fake_services.parse_setting_value(text)
        except ValueError:
            continue
    return out


class Harness:
    """A loaded ELF plus the fake environment it runs in."""

    # In the order a run is described: the build, how much romfs, whether it
    # is patched, what heap it may take, what SD it has. program_id comes
    # last, since a run rarely names it.
    def __init__(self, guest, romfs, patched=False,
                 app_heap_mb=HEAP_GRANT_DEFAULT_MB, sys_heap_mb=0,
                 sdmc_dir=None, fresh_sd=False, program_id=None):
        """guest comes from load(), which has already confirmed the binary.

        Order is part of the contract: install every piece of real behaviour,
        then derive stubs over what is left, since derivation skips what is
        already hooked. The game is built before either, because ncm has to be
        answering out of it by the time the hooks go in -- and it is one game,
        so what is reported installed and what is merged cannot disagree."""
        self.guest = guest
        self.program_id = DEFAULT_PROGRAM_ID if program_id is None else program_id

        # Before anything asks: ConfigureDynamicHeap reads this table at launch.
        self.granted = grant_dynamic_heap(guest, self.program_id, app_heap_mb,
                                          sys_heap_mb)

        # What the system reports installed, and what the ncm hooks are about
        # to be given. One per harness, as the guest and the SD are: two
        # harnesses are two consoles, and neither tells the other what is
        # installed.
        self.meta_db = fake_meta.FakeContentMetaDatabase()
        db_builder = fake_meta.FakeDbBuilder(self.meta_db)
        db_builder.add_game(self.program_id)
        if patched:
            db_builder.add_update(self.program_id, DEFAULT_UPDATE_LEVEL)

        # A patch changes the game, not just what ncm answers about it, so the
        # version it launches at is what the romfs is generated from -- asked
        # of the database, so the tree and the answer cannot come apart.
        self.modded = fake_romfs.FakeModdedGame(
            romfs["base"],
            (romfs["loose"]["overridden"], romfs["loose"]["added"]),
            (romfs["bin"]["overridden"], romfs["bin"]["added"]),
            self.launch_version() or 0)

        # An SD outlives the process that wrote to it, so a build looking for
        # what it left there finds it, as on a console.
        self.sd = FakeSD(sdmc_dir, fresh_sd)
        self.stats = fake_services.Stats()

        # A premise of the run, like the game and the mod: a build reads its
        # own switches out of the SD and takes a different path for them.
        self.settings = _settings_on(self.sd)
        fake_services.install(guest, self.sd, self.stats, self.meta_db,
                              self.settings)
        self.malloc_heap = hos_memory.RealMallocHeap(guest)
        self.hos_memory = hos_memory.HOSMemory(guest)

        # The module's static malloc buffer is memory it uses too: the
        # allocation cascade falls back there when the dynamic heaps are full,
        # and measuring only what was asked of the OS would miss that entirely.
        self.hos_memory.extra_ranges.append(
            (self.malloc_heap.buffer, self.malloc_heap.size,
             hos_memory.STATIC_BUFFER))

        # Which symbols leave the sandbox is a question about the binary; the
        # namespaces that make one a service are this module's policy. So the
        # call belongs here, not inside what installs the answers.
        found = boundaries.found_in(guest, MODULE_PREFIX,
                                    SERVICE_NAMESPACES,
                                    already_hooked=guest.hooked_symbols())
        self.stubbed = fake_services.stub_service_boundary(guest, found)

        # The produced romfs's header, once something asks: None until then,
        # False once an ask has failed, so a launch that built nothing readable
        # is not read again for every kind that carries bytes.
        self._header = None

        # From here the module runs in the order a boot runs it:
        # InitializeSystemModule brings up the service clients, and only then
        # does Startup hand the allocator its buffer. The harness drives entry
        # points rather than main, so it owes the module that startup. After
        # the stubs, deliberately: a client whose transport is stubbed still
        # initialises, it just answers nothing.
        if self.has("ncm_init"):
            self.call("ncm_init")

        # Off, as InitializeSystemModule turns it off. It defaults to on, and
        # left there an fs Result a console hands back would abort instead.
        if self.has("fs_auto_abort"):
            self.call("fs_auto_abort", args=(0,))

        # Second, as on hardware, where there is no heap until this runs --
        # __libnx_initheap is empty and __libnx_alloc aborts. A client that
        # allocated before this point would die at boot, and now does here.
        self.malloc_heap.startup()

        self.phases = []

        # Set by launch(), which is what decides whether the mod arrives packed.
        self.packed = None

        # Freeing does not clear, on hardware or here: a build reading
        # through a pointer it has released finds what it left, not the zeros
        # a freshly mapped page would hand it.
        try:
            _free = guest.find_one(
                "ams::mitm::fs::romfs::FreeTracked("
                "ams::mitm::fs::romfs::AllocationType, void*, unsigned long)")
        except Exception:
            _free = None

        if _free is not None:
            def _poison_freed(guest):
                ptr, n = guest.arg(1), guest.arg(2)
                if ptr and 0 < n <= (32 << 20):
                    guest.write(ptr, b"\xCC" * n)
            guest.watch(_free, _poison_freed)

        guest.call(guest.find_one(SYM["init_sd"]))

    def crossings(self):
        """Every range the measured phases crossed, in the order they did.

        Phases only: what the verifier reads afterwards is not the build's
        cost, and the report would otherwise charge it for the whole romfs.
        """
        return [r for p in self.phases for r in p.ranges]

    def handle_calls(self):
        """Every file a measured phase named without moving bytes through it.

        Opening, closing, making and removing: the calls whose whole cost is
        the crossing, and which say nothing in a total because one file
        opened seven times and seven files opened once cost the same.
        """
        return [c for p in self.phases for c in p.touches]

    def layouts(self, produced_header=None):
        """{target: [(name, first, last)]} for everything a crossing can land in.

        The two sources are the harness's own, so their regions are known from
        the headers it wrote. The metadata file is the build's, and its regions
        are only known once the produced romfs has been read back -- so a
        caller with that header gets them, and one without does not.

        Handed over as what a region is, rather than as the type the format
        keeps them in: whoever charges bytes to these needs where each one
        starts and stops, not a romfs_format import.
        """
        out = {}
        if self.sd.base_storage is not None:
            out[fake_services.BASE_ROMFS] = _bounds(romfs_format.regions(
                romfs_format.header_at(self.sd.base_storage.header), "base "))

        if self.packed is not None:
            path, image = self.packed
            out[path] = _bounds(romfs_format.regions(
                romfs_format.header_at(image.header), "romfs.bin "))

        if produced_header is not None:
            out[self.cache_path("romfs_metadata.bin")] = _bounds(
                _metadata_regions(produced_header, "metadata "))
        return out

    def contents_dir(self):
        """The title's own directory on the SD: its mod, and the build's
        cache of what it made of it."""
        return self.sd_romfs_root().rsplit("/", 1)[0]

    def cache_path(self, name):
        """Where the build leaves a file of its own for the next launch."""
        return "%s/%s" % (self.contents_dir(), name)

    def launch_version(self):
        """The version this run's title would launch at.

        Asked of the database, since a patch changes the game rather than only
        what ncm says about it -- so this is the version the tree was
        generated from, not a premise the caller passed in.
        """
        return self.meta_db.launch_version(self.program_id)

    def version_shown(self):
        """The same version, in the words a console would put it in."""
        return fake_meta.display_version(self.launch_version())

    def by_region(self, written):
        """[Row] for that half of the crossings, busiest region first.

        The layout the metadata file is bucketed against is only knowable once
        the produced romfs has been read back, so a romfs that cannot be read
        leaves those writes charged to the file rather than to a table.
        """
        return accounting.by_region(self.crossings(),
                                    self.layouts(self._produced_header()),
                                    written)

    def _produced_header(self):
        """The header of the romfs this launch built, or None if it has none.

        Read once and kept: this is asked for every kind that carries bytes --
        reads, then writes -- and it is one romfs with one header, fetched
        through the emulated read path.

        The failures named are the ones that mean "no such header": the guest
        gave up, what came back is not a romfs, or there were not enough bytes
        to unpack one from. Catching everything would let a mistake of this
        file's own read as a build whose output cannot be parsed.
        """
        if self._header is None:
            try:
                self._header = romfs_check.produced_header(
                    self.produced_romfs())
            except (ROMFS_FAILURES + GUEST_FAILURES
                    + MODULE_FAILURES + (struct.error,)):
                self._header = False
        return self._header or None

    def fs_usage(self, totals=None):
        """[Kind] -- every crossing, once, under what it was for."""
        return accounting.by_what_for(
            self.totals() if totals is None else totals,
            self.by_region, self.handle_calls())

    def result(self, described=None, corrupt=None):
        """Everything about the run that is over, in one value.

        The SD is flushed here, because this is the end of the run and a
        flush is the last thing a launch does -- not in whatever prints,
        where writing to an SD would be a side effect of saying so.

        described and corrupt come from the caller: reading the romfs back is
        its decision, taken after the figures are summed.
        """
        totals = self.totals()
        assertions, debug = self.what_it_said()
        return Result(
            totals=totals,
            kinds=self.fs_usage(totals) if self.phases else (),
            pools=self.memory_used(),
            written=[(self.sd.host_path(path), size)
                     for path, size, _data in self.write_sd()],
            version=self.version_shown(),
            described=described,
            corrupt=corrupt,
            stubs=self.stubs_fired(),
            assertions=assertions,
            debug=debug,
            contents=self.contents_dir())

    def produced_romfs(self):
        """What this launch built, for whoever reads it back."""
        return ProducedRomfs(self)

    def sd_romfs_root(self):
        """Where this run's mod files live on the SD."""
        return sd_romfs_root(self.program_id)

    # ---- calling into the module ----

    def has(self, key_or_symbol):
        """A build without the symbol skips that path, it never fails on it."""
        return len(self.guest.find(self.symbol(key_or_symbol))) > 0

    def symbol(self, key_or_symbol):
        return SYM.get(key_or_symbol, key_or_symbol)

    def call(self, key_or_symbol, args=(), indirect_result=None):
        """Call by target key or by symbol substring.

        indirect_result is the AAPCS64 x8 convention: a function returning a
        non-trivial type is handed the caller's storage rather than returning
        in a register.
        """
        return self.guest.call(self.guest.find_one(self.symbol(key_or_symbol)),
                               args=args, indirect_result=indirect_result)

    def configure_heaps(self):
        """Let the module take its heaps by its own rules; returns whatever it
        reported taking from elsewhere."""
        return hos_memory.configure_via_target(self.guest, self.program_id, SYM["configure_heap"])

    def totals(self):
        """What this run's phases add up to."""
        return accounting.totals(self.phases)

    def memory_used(self):
        """[Pool] -- what the module took, per place it can put a byte.

        Read back off the guest's own memory rather than tallied as it was
        asked for, so a byte the build wrote to and freed still counts.

        From the phase that peaked, which is the figure reported as the total:
        scanning again here would answer for the moment the report is written,
        after calls the build is not charged for have touched pages of their
        own, and the rows would not add up to the total beneath them.
        """
        peak = max(self.phases, key=lambda p: p.touched, default=None)
        return accounting.by_pool(
            peak.regions if peak else self.hos_memory.touched_by_region())

    def write_sd(self):
        """Leave on the SD what the build wrote, and say what that was.

        After the run, for the reason every write here is deferred: a file per
        write would cost more than the build being measured. Written on a
        failed launch as much as a finished one -- a console keeps what it was
        given.
        """
        written = self.sd.written_files()
        self.sd.flush()
        return written

    def read_back(self):
        """The romfs the run produced: verified, then described.

        After totals(), never inside launch(): the walk is the length of the
        romfs, and would land in the figures the run exists to report.

        Verified, not merely described -- a digest over a romfs a game could
        not have read is a number for something that was never built. Raises
        ROMFS_FAILURES when it is one.
        """
        reader = romfs_check.RomfsReader(self.produced_romfs())
        reader.verify()
        return reader.describe()

    def what_it_said(self):
        """(assertions, debug lines) -- what the build itself said on the way.

        Gathered here because both are recorded on the guest by blackbox,
        which is not somewhere a caller should have to know about.
        """
        guest = self.guest
        return (guest.assertions,
                [line for chunk in guest.debug_output for line in chunk.splitlines()])

    def stubs_fired(self):
        """(name, count) per call the sandbox answered in the target's place.

        The signature is dropped: the qualified name says which call it was, and
        keeping the parameters pushed the count off the end of the line.
        """
        out = []
        for name, n in sorted(self.stubbed.items()):
            out.append((name.split("(", 1)[0], n))
        return out

    def launch(self):
        """What a launch costs: the game's romfs, the mod on top, and whatever
        the build does about it.

        Entered where the service enters -- GetLayeredRomfsStorage, then
        InitializeImpl.

        The base romfs arrives as an FsStorage, as fs hands it over. The mod
        arrives the ways LayeredFS accepts one: loose files on the SD, packed
        into romfs.bin -- merged as a second source rather than overlaid -- or
        both at once, which is an ordinary SD. romfs["loose"] and romfs["bin"]
        say how much arrives each way.
        """
        modded = self.modded
        self.packed = None
        self.sd.base_storage = _base_storage(modded.game)
        root = self.sd_romfs_root()

        if len(modded.packed):
            # Packed, and sitting beside the romfs directory rather than inside it.
            image = packed_mod(modded.packed)
            self.packed = (root.rsplit("/", 1)[0] + "/romfs.bin", image)
            self.sd.put_file_virtual(self.packed[0], b"", image.size,
                                     image.read)

        for _n, (path, size) in zip(
                progress.ticking("writing the mod's loose files",
                                 len(modded.loose)),
                modded.loose.files(root)):
            self.sd.put_file_sparse(path, size)

        # The SD really is mounted and served here, so this is the truthful answer
        # rather than a stub: without it the module skips its own SD walk.
        self.guest.hook(self.symbol("sd_ready"), lambda guest: guest.ret(1))

        # BeginInitialize hands the storage to a worker thread the harness does not
        # run. Capture `this` and stop there; the initialize below is the phase we
        # actually want to measure.
        impl = []

        def on_begin(guest):
            impl.append(guest.arg(0))
            guest.ret(None)

        hooked = [key for key in ("begin_init", "request_init") if self.has(key)]
        for key in hooked:
            self.guest.hook(self.symbol(key), on_begin)
        if not hooked:
            raise RuntimeError("no way to intercept the hand-off to the initializer "
                               "thread: neither BeginInitialize nor "
                               "RequestInitializeStorage is present")

        # Measured, not just called: the call may do any amount of work before it
        # answers, and work left outside a phase is work the report does not
        # charge for. The return value is ignored -- what was taken is measured
        # from the blocks asked for.
        self.phase("configure_heap", self.configure_heaps)

        # The FsStorage handle fs would have returned. Contents come from
        # _base_storage through the hooked fsStorage* calls.
        fs_storage = self.guest.alloc(0x20)
        self.guest.w32(fs_storage, 1)          # non-zero session, so it reads as live
        out = self.guest.alloc(0x20)

        self.phase("get_layered",
                   lambda: self.call("get_layered",
                                     args=(self.program_id, fs_storage, 1),
                                     indirect_result=out))
        if not impl:
            raise RuntimeError("BeginInitialize was never reached: no storage to initialize")

        self.phase("initialize", lambda: self.call("initialize", args=(impl[0],)))

        # Kept so the result can be read back afterwards; nothing measured uses it.
        self.layered = impl[0]

        # Nothing is returned: what the module took is in the result, one row
        # per place it can put a byte and how much of each it used, measured
        # off the pages at the end of every phase.

    def phase(self, key, fn):
        """Run fn() with counters snapshotted around it; record and return.

        Named for the symbol it drives, so the report cannot name a function
        the run did not call.
        """
        name = phase_name(key)
        p = Phase(name)
        before = self.stats.total
        before_ranges = len(self.stats.ranges)
        before_touches = len(self.stats.touches)
        before_calls = dict(self.stats.calls)
        before_moved = self.stats.moved
        before_written = self.stats.written_bytes

        # CPU time, not wall: time.time() went negative once on an adjusted
        # host clock, and monotonic reported phases as outlasting the process.
        # Nothing inside a phase blocks, so CPU time is the elapsed time.
        started = time.process_time()
        result = fn()
        p.seconds = time.process_time() - started

        p.fs_calls = self.stats.total - before
        p.calls = {k: v - before_calls.get(k, 0)
                   for k, v in self.stats.calls.items()
                   if v - before_calls.get(k, 0) > 0}
        p.read = ((self.stats.moved - before_moved)
                  - (self.stats.written_bytes - before_written))
        p.written = self.stats.written_bytes - before_written

        # Measured rather than tallied: the pages a phase wrote to are read
        # back, so nothing has to be counted as it happens. Releasing a heap
        # does not re-zero it, so a phase that builds and frees inside itself
        # still shows what it cost.
        #
        # Kept per region as well as summed, because the peak is reported both
        # ways and one scan is what makes the two agree.
        p.regions = tuple(self.hos_memory.touched_by_region())
        p.touched = sum(used for _label, _size, used in p.regions)

        # This phase's own crossings. Kept per phase for the reason the
        # counters are: reading the romfs back afterwards crosses the boundary
        # too, and it is not what the build was charged for.
        p.ranges = tuple(self.stats.ranges[before_ranges:])
        p.touches = tuple(self.stats.touches[before_touches:])
        self.phases.append(p)
        return result


# ----------------------------------------------------------------------------
# The romfs a launch is given
#
# Walking the index lives here, not in fake_romfs, so it lives in one place --
# the loose mod below walks the same one. That leaves fake_romfs pure functions
# of an index, with nothing to say to a terminal.
# ----------------------------------------------------------------------------

def _bounds(regions):
    """[(name, first, last)] -- a region said as where it starts and stops."""
    return [(region.name, region.start, region.end) for region in regions]


def _metadata_regions(header, prefix=""):
    """The regions of the metadata file a build leaves for its next launch.

    Its layout is the build's, not the format's: the four tables the produced
    romfs has, end to end from zero, in the order they appear in it. Which is
    what the writes land on -- known only because the romfs is read back
    afterwards, and its header says what the tables turned out to be.
    """
    sizes = ((prefix + "dir hash", header.dir_hash_size),
             (prefix + "dir table", header.dir_table_size),
             (prefix + "file hash", header.file_hash_size),
             (prefix + "file table", header.file_table_size))
    out, at = [], 0
    for name, size in sizes:
        if size > 0:
            out.append(romfs_format.Region(name, at, at + size))
        at += size
    return out


def _packed(rb, label, content_at):
    """A filled builder, packed and wrapped as the storage a launch reads.

    One call, so there is nothing to count through: a label is all there is to
    say, and a spinner would draw from another thread over the report.
    """
    progress.status(label)
    image = rb.pack(content_at)
    progress.clear()
    return image


def packed_mod(mod):
    """The romfs.bin image carrying a mod.

    The mod says which files; this says how they arrive. A loose tree of the
    same FakeMod delivers the same paths at the same sizes, so delivery is the
    only difference -- which is what makes comparing them mean anything.
    """
    rb = romfs_pack.RomfsBuilder()
    for _n, (path, size) in zip(
            progress.ticking("generating the romfs.bin mod", len(mod)),
            mod.files()):
        rb.add_file_sparse(path, size)
    return _packed(rb, "packing the romfs.bin mod", mod.content_at)


def _base_storage(game):
    """The packed image of a game, ready to serve as its romfs.

    Patched, it is the same tree with the update's files grown -- a different
    romfs, as a patched game genuinely has.
    """
    rb = romfs_pack.RomfsBuilder()
    for i in progress.ticking("generating the base romfs", game.count):
        rb.add_file_sparse(game.path(i), game.size(i))
    return _packed(rb, "packing the base romfs", game.content_at)
