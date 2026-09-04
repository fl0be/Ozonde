#!/usr/bin/env python3
"""The memory a sysmodule is given, and which pool each byte of it came from.

hos is Horizon, spelled as the code being measured spells it: ams::hos is
Atmosphere's own namespace.

Nothing here is faked in the way the SD and the game are. The pages are real
guest memory and the module allocates from them with its own allocator, so
what it does with them is measured rather than modelled; what this adds is
which of the OS's pools a byte was borrowed from, which the call that asked
for it decides. The pools have no size to run out of, and every request is
granted. malloc is left alone in either direction -- that one is the target's
own, on its own buffer, running for real.

Peak memory is read back from those pages afterwards rather than counted as it
is handed out -- see HOSMemory.touched_by_region. Poisoned pages that changed are
the only figure that means anything on hardware, because the module's own
allocator, its size classes and its free lists are then all irrelevant.
"""
import struct


def _hook_every(guest, pairs):
    """Install a hook on every symbol a caller could reach by that name.

    Every match rather than one, because a name can belong to more than one
    symbol and a real call site may reach either. A [clone ...] is the
    exception: the compiler outlines part of a function into one and only the
    parent calls it, which this has just hooked, so hooking the clone as well
    would install behaviour on a body nothing arrives at -- with arguments in
    whatever registers the compiler chose, since a clone keeps no ABI.
    """
    for name, hook in pairs:
        for sym in guest.find(name):
            if "clone" in sym:
                continue
            guest.hook(sym, hook, exact=True)


# Four places a build can put a byte, told apart by whose memory it is. Three
# are borrowed and given back when the build ends; only .bss is its own. App
# and sys are separate regions -- one allocation cannot span them -- so a split
# grant can be asked which half it leaned on.
BORROWED_APPLET = "Borrowed from applet pool"
BORROWED_APP = "Borrowed from app pool"
BORROWED_SYS = "Borrowed from sys pool"
STATIC_BUFFER = "Static buffer (.bss)"

# The order AllocateTracked tries them in, which is the order a console's own
# log reads, so the two compare row for row. All four are reported every run:
# a build that borrows nothing has a pool of zero, and that zero is a result.
REGION_ORDER = (BORROWED_APPLET, BORROWED_APP, BORROWED_SYS, STATIC_BUFFER)


class HOSMemory:
    """Satisfies the OS calls a sysmodule uses to obtain memory, and records
    what it handed out.

    Hooking here rather than inside the module keeps this ignorant of the thing
    it measures: how many heaps exist, how large they are, which
    allocation types may live in which heap, and in what order they are tried
    are all decisions the target makes for itself, exactly as on hardware.
    This only plays the kernel.
    """

    _PAGE = 0x1000

    # Handed-out memory is poisoned rather than left zero, so "has been used"
    # means "differs from the poison" instead of "is non-zero". Without this a
    # page the module writes zeros into is indistinguishable from one it never
    # touched, and the measurement quietly under-reports.
    _POISON = 0xCD

    # Poisoned and read back a megabyte at a time. A whole heap at once builds
    # a Python object its full size on every pass, to measure a guest that
    # may not have grown at all.
    _CHUNK = 1 << 20

    def __init__(self, guest):
        self._guest = guest
        self._blocks = []         # (address, size, kind) as requested

        # Memory the module uses without asking the OS for it -- a static
        # buffer in .bss is still memory it occupies, and its fallback path
        # spills there once the dynamic heaps are full.
        self.extra_ranges = []
        self._shared_memory = None
        self._shared_memory_live = False
        self._install()

    def _regions(self):
        """(address, size, label, poisoned) for everything the module can use.

        Poisoned regions are ones this class handed out; the static buffer was
        never ours to poison, so "used" there still means "not zero".
        """
        for addr, size, label in self._blocks:
            yield addr, size, label, True
        for addr, size, label in self.extra_ranges:
            yield addr, size, label, False

    def touched_by_region(self):
        """[(label, size, touched)] -- where the peak actually sits.

        A high-water figure by nature: freed memory is not zeroed again, so
        this is the most the module ever touched, not what it holds now, and
        the only figure available -- FreeTracked is inlined into its callers,
        so watching allocations instead would see every one of them and not a
        single release.
        """
        out = []
        poisoned_page = bytes([self._POISON]) * self._PAGE

        for addr, size, label, poisoned in self._regions():
            untouched = poisoned_page if poisoned else bytes(self._PAGE)
            whole = untouched * (self._CHUNK // self._PAGE)

            # Page by page, a chunk at a time.
            used = read = 0
            while read < size:
                n = min(self._CHUNK, size - read)
                data = self._guest.read(addr + read, n)

                # A megabyte nothing wrote to is the common case on a big
                # heap, and one comparison settles it.
                if n == self._CHUNK and data == whole:
                    read += n
                    continue

                for off in range(0, n, self._PAGE):
                    page = data[off:off + self._PAGE]

                    # By what the page holds, not by a whole page: a grant is
                    # whatever the build asked for and nothing here rounds it,
                    # so a region can end short of a page boundary. Compared
                    # against a full page that tail could never match, and
                    # counted as a full page it could put touched above size.
                    if page != untouched[:len(page)]:
                        used += len(page)
                read += n

            # One row per pool, however many blocks it was handed out in.
            merged = next((r for r in out if r[0] == label), None)
            if merged is None:
                out.append([label, size, used])
            else:
                merged[1] += size
                merged[2] += used

        # Every pool appears, in the order they are tried, so a run that
        # borrowed nothing from one still says so.
        for label in REGION_ORDER:
            if not any(r[0] == label for r in out):
                out.append([label, 0, 0])
        rank = {label: i for i, label in enumerate(REGION_ORDER)}
        out.sort(key=lambda r: rank.get(r[0], len(rank)))
        return [tuple(r) for r in out]

    def _give(self, size, kind):
        """Pages, poisoned so that writing zeros to them still counts."""
        buf = self._guest.alloc(size, align=0x1000)

        chunk = bytes([self._POISON]) * min(size, self._CHUNK)
        written = 0
        while written < size:
            n = min(len(chunk), size - written)
            self._guest.write(buf + written,
                              chunk if n == len(chunk) else chunk[:n])
            written += n
        self._blocks.append((buf, size, kind))
        return buf

    def _install(self):
        guest = self._guest

        def alloc_out_ptr(kind):
            """A hook for Result f(uintptr_t *out, size_t size)."""
            def hook(guest):
                out = guest.arg(0)
                size = guest.arg(1)

                buf = self._give(size, kind)
                guest.write(out, struct.pack("<Q", buf))
                guest.ret(0)
            return hook

        def ok(guest):
            guest.ret(0)

        def noop(guest):
            guest.ret(None)

        # Borrowing pages from a pool, and giving them back.
        _hook_every(guest, [
            # "Unsafe" is Horizon's word, not a judgement:
            # svcMapPhysicalMemoryUnsafe maps pages out of the *application*
            # pool -- the one the running game allocates from -- so every byte
            # taken here is a byte the game loses.
            ("os::AllocateUnsafeMemory(unsigned long*, unsigned long)",
             alloc_out_ptr(BORROWED_APP)),

            # The system half, reached through SetMemoryHeapSize rather than
            # svcMapPhysicalMemoryUnsafe -- which is why it is its own row.
            ("os::AllocateMemoryBlock(unsigned long*, unsigned long)",
             alloc_out_ptr(BORROWED_SYS)),
            ("os::SetMemoryHeapSize(unsigned long)", ok),
            ("os::FreeUnsafeMemory(unsigned long, unsigned long)", noop),
            ("os::FreeMemoryBlock(unsigned long, unsigned long)", noop),
        ])

        def hk_memlet_create(guest):
            """Result f(Handle *out, u64 *out_size, u64 size).

            memlet hands back what the applet pool can spare, which on a
            console dwarfs what a build asks for -- so the request is the
            grant, reported back the way a build expects.
            """
            out_handle = guest.arg(0)
            out_size = guest.arg(1)
            granted = guest.arg(2)

            self._shared_memory = self._give(granted, BORROWED_APPLET)
            self._shared_memory_live = True
            guest.w32(out_handle, 0xBEEF)
            guest.write(out_size, struct.pack("<Q", granted))
            guest.ret(0)

        def hk_map(guest):
            guest.ret(self._shared_memory or 0)

        def hk_destroy(guest):
            # The module checks the handle is gone afterwards, so it must be.
            self._shared_memory_live = False
            guest.ret(None)

        def hk_get_handle(guest):
            guest.ret(0xBEEF if self._shared_memory_live else 0)

        # The applet's shared memory, which is a handle rather than pages.
        _hook_every(guest, [
            ("memletInitialize", ok),
            ("memletCreateAppletSharedMemory", hk_memlet_create),
            ("os::AttachSharedMemory", noop),
            ("os::MapSharedMemory", hk_map),
            ("os::DestroySharedMemory", hk_destroy),
            ("os::GetSharedMemoryHandle", hk_get_handle),
        ])


def configure_via_target(guest, program_id, configure_heap_sym):
    """Let the module decide its own heap layout by calling its own
    heap-configuration entry point. Whatever it asks the OS for, it gets."""
    out = guest.alloc(8, align=8)

    # cfg::OverrideStatus: a non-zero keys/flags word is enough for
    # IsProgramSpecific() to hold; the module reads it, nothing here reads it.
    status = guest.alloc(0x20, align=8)
    guest.write(status, struct.pack("<QQ", 0xFFFFFFFFFFFFFFFF,
                                    0xFFFFFFFFFFFFFFFF))

    fn = guest.find_one(configure_heap_sym)
    guest.call(fn, args=(out, program_id, status, 1))
    return guest.u64(out)


class RealMallocHeap:
    """The module's own heap: its genuine g_malloc_buffer handed to its own init
    allocator, so malloc, operator new and the module's fallback path behave
    as on hardware, including running out."""

    _BUFFER = "ams::(anonymous namespace)::g_malloc_buffer"

    def __init__(self, guest):
        """Everything the allocator needs, short of starting it.

        Split from startup() because these have to be in place before the
        stubs are derived, while the call itself belongs where a boot makes
        it -- after the service clients, not before.
        """
        self._guest = guest
        self.buffer = guest.find_exact(self._BUFFER)

        # From the binary or not at all. A constant here would be right for
        # every build that agrees with it and silently wrong for one that
        # does not.
        self.size = guest.symbol_sizes.get(self._BUFFER)
        if not self.size:
            raise KeyError("%s carries no size: the memory figures would "
                           "describe some other build" % self._BUFFER)

        # A failing malloc sets errno through newlib's per-thread reent -- a
        # syscall we have no kernel for. Give errno somewhere real to live, so
        # running out returns null instead of trapping.
        scratch = guest.alloc(0x100, align=16)
        for sym in ("__errno", "__getreent"):
            if sym in guest.symbols:
                guest.hook(sym, (lambda a: lambda guest: guest.ret(a))(scratch), exact=True)

    def startup(self):
        """Hand the module its own buffer, as ams::init::Startup() does."""
        self._guest.call(
            self._guest.find_one("init::InitializeAllocator(void*, unsigned long)"),
            args=(self.buffer, self.size))
