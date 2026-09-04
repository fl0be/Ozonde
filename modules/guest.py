#!/usr/bin/env python3
"""The guest: real compiled ARM64, running.

This is the one module here that executes anything. It maps a real, compiled
ams.mitm ELF under Unicorn Engine and calls into it, without emulating Horizon
OS at all -- nothing here processes svc/IPC for real. Every external dependency the
target code has is intercepted at its symbol address and answered by a Python
callback, so the real body never runs. Everything else -- the code actually
under test, and whatever it calls that is not hooked -- executes as genuine
compiled ARM64 instructions.

Which symbols get intercepted is not decided here: a caller names the entry
points and installs the environment behind them.

This deliberately skips crt0/_start, relocations for anything beyond
R_AARCH64_RELATIVE, and real syscalls; there is no Horizon kernel here. Nor are
instructions counted: that needs a Python callback per basic block, and the
workload is fs* round-trip bound on hardware anyway.
"""
import io
import struct
import subprocess
import types
from elftools.elf.elffile import ELFFile
from unicorn import (Uc, UcError, UC_ARCH_ARM64, UC_MODE_ARM,
                     UC_HOOK_CODE, UC_HOOK_INTR, UC_HOOK_MEM_INVALID,
                     UC_HOOK_MEM_WRITE,
                     UC_PROT_ALL, UC_PROT_EXEC, UC_PROT_READ)

# Named rather than star-imported: these two modules export 468 names between
# them and this file uses 26. Spelled out, the list doubles as the answer to
# "which registers are touched" -- x0-x8 because that is the AAPCS argument
# window, x30 and pc to unwind, sp and tpidrro_el0 because the module is
# entered without a crt0 having set them up.
from unicorn.arm64_const import (
    UC_ARM64_REG_LR, UC_ARM64_REG_PC, UC_ARM64_REG_SP,
    UC_ARM64_REG_TPIDRRO_EL0, UC_ARM64_REG_X0, UC_ARM64_REG_X1,
    UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X5,
    UC_ARM64_REG_X6, UC_ARM64_REG_X7, UC_ARM64_REG_X8, UC_ARM64_REG_X29,
    UC_ARM64_REG_X30)

import blackbox
from blackbox import GuestAbort  # re-exported: callers catch it from here

PAGE = 0x1000
R_AARCH64_RELATIVE = 1027

# Loaded where it was linked. Rebasing would leave the abort magic address
# unmapped, which is tempting, but the relative relocations live in .relr.dyn
# and nothing here decodes it: at base 0 every entry adds zero, anywhere else
# the image branches into nowhere. Rebase only after implementing RELR.
DEFAULT_BASE = 0x0


def _align_down(x):
    return x & ~(PAGE - 1)


def _align_up(x):
    return (x + PAGE - 1) & ~(PAGE - 1)


# svc #imm16 -- the syscall number lives in the immediate, so match the opcode
# with bits 20:5 masked out rather than looking for a bare `svc #0`. Stated
# wherever instructions are read: what an svc looks like is a fact about
# ARM64, and reading one should not take an import.
SVC_MASK, SVC_OP = 0xFFE0001F, 0xD4000001
SVC_IMM_SHIFT, SVC_IMM_MASK = 5, 0xFFFF


# The two ways a run ends that are the build's doing rather than ours: an
# abort it raised, or a fault it took. Named here so a caller can tell those
# from a bug on this side without knowing what runs the code.
FAILURES = (GuestAbort, UcError)


class Guest:
    # libnx ThreadVars: { u32 magic; Handle handle; Thread *thread_ptr;
    #                     void *reent; void *tls_tp; }, placed at the end of
    # the 0x200-byte thread-local region.
    THREAD_VARS_SIZE = 0x20
    THREAD_VARS_MAGIC = 0x21545624   # '$TV!'
    FAKE_THREAD_HANDLE = 0x0000DEAD  # any non-zero handle

    def __init__(self, elf_path, nm_tool="aarch64-none-elf-nm", base=DEFAULT_BASE,
                 trace_ring=False, heap_mb=256):
        self.elf_path = elf_path
        self.base = base
        self._uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        self._hook_stubs = {}

        with open(elf_path, "rb") as f:
            data = f.read()
        self._elf = ELFFile(io.BytesIO(data))

        self._map_segments()
        self._apply_relocations()
        self._symbols = self._load_symbols(nm_tool)

        # A dedicated page for the emulation-stop sentinel address (the LR we
        # give top-level calls so emu_start's "until" naturally fires).
        stub_region = _align_up(
            max(self._symbols.values(), default=0x100000)) + 0x100000
        self._uc.mem_map(stub_region, 0x10000, UC_PROT_ALL)
        self._sentinel = stub_region

        # Bump allocator for the guest memory handed out here. It never
        # frees, which holds for as long as a process measures one build; a
        # process measuring several would need a real allocator.
        self.heap_base = stub_region + 0x10000
        self.heap_size = heap_mb * 1024 * 1024
        self._uc.mem_map(self.heap_base, self.heap_size, UC_PROT_ALL)
        self._heap_cursor = self.heap_base

        self.stack_size = 4 * 1024 * 1024
        self.stack_base = self.heap_base + self.heap_size + PAGE
        self._uc.mem_map(self.stack_base, self.stack_size, UC_PROT_ALL)

        # Every Horizon thread has a thread-local region, and code reads it
        # through TPIDRRO_EL0. Left at 0 the guest dereferences address 0,
        # which now faults rather than returning image bytes -- but it still
        # has to point somewhere real, since the code behind it is genuine.
        self.tls_base = self.alloc(0x200)
        self._uc.mem_write(self.tls_base, bytes(0x200))
        self._uc.reg_write(UC_ARM64_REG_TPIDRRO_EL0, self.tls_base)

        # ams mutexes identify their owner by the handle in libnx's ThreadVars.
        # Left zero it matches a fresh mutex's owner tag, so every mutex looks
        # locked by the current thread and a debug build aborts on the first
        # lock. Any non-zero handle does; the magic is libnx's own check.
        tv = self.tls_base + 0x200 - self.THREAD_VARS_SIZE
        self._uc.mem_write(tv, struct.pack("<II", self.THREAD_VARS_MAGIC, self.FAKE_THREAD_HANDLE))

        # And separately by an os::ThreadType* at the start of the TLS block.
        # Null there matches a fresh ReaderWriterLock's owner, so a debug build
        # aborts taking a read lock. Both are needed: mutexes check the handle
        # above, reader-writer locks check this.
        tls_start = self._symbols.get("__tls_start")
        if tls_start is not None:
            self._fake_thread = self.alloc(0x200)
            self._uc.mem_write(self._fake_thread, bytes(0x200))
            self._uc.mem_write(tls_start, struct.pack("<Q", self._fake_thread))

        self._pending_exc = None

        # Anything the target printed via svcOutputDebugString.
        self.debug_output = []

        # Failing AMS_ASSERTs seen (debug builds only; see blackbox).
        self.assertions = []
        self._uc.hook_add(UC_HOOK_INTR, self._on_intr)
        self._uc.hook_add(UC_HOOK_MEM_INVALID, self._on_mem_invalid)
        blackbox.install(self)

        # Last-executed-PC ring for crash forensics. This is a per-INSTRUCTION
        # Python callback (brutally slow) -- opt-in only; benchmarks must run
        # without it.
        self.ring = []
        if trace_ring:
            def _ring(uc, address, size, user_data):
                self.ring.append(address)
                if len(self.ring) > 400:
                    del self.ring[:200]

            self._uc.hook_add(UC_HOOK_CODE, _ring, begin=self.load_lo, end=self.load_hi)

    # ------------------------------------------------------------------------
    # Loading it
    # ------------------------------------------------------------------------

    def _map_segments(self):
        segs = [s for s in self._elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        lo = min(self.base + s["p_vaddr"] for s in segs)
        hi = max(self.base + s["p_vaddr"] + s["p_memsz"] for s in segs)
        lo_a, hi_a = _align_down(lo), _align_up(hi)
        self._uc.mem_map(lo_a, hi_a - lo_a, UC_PROT_ALL)
        for s in segs:
            vaddr = self.base + s["p_vaddr"]
            self._uc.mem_write(vaddr, s.data())
        self.load_lo, self.load_hi = lo_a, hi_a

        # The parts that hold instructions, for anything reading the binary as
        # code: the whole mapped range is mostly .bss, and a constant pool read
        # as instructions invents call edges. Falls back to the whole range if
        # a build has no section headers left.
        ALLOC, EXECINSTR = 0x2, 0x4
        code_ranges = [(self.base + sec["sh_addr"], sec["sh_size"])
                       for sec in self._elf.iter_sections()
                       if sec["sh_size"]
                       and sec["sh_flags"] & ALLOC
                       and sec["sh_flags"] & EXECINSTR]
        if not code_ranges:
            code_ranges = [(lo_a, hi_a - lo_a)]
        self._code_ranges = tuple(code_ranges)

        # Code mapped read+execute once relocations are in place. A store into
        # .text once corrupted instructions and surfaced as a fault hundreds of
        # thousands later; now it stops there. It also catches aborts for free
        # -- an abort stores to address 8, and a writable page would need a
        # memory-write hook, which puts every guest store on Unicorn's slow
        # path. Whole pages only, so data sharing an edge page stays writable.
        protected = []
        for addr, size in self._code_ranges:
            lo, hi = _align_up(addr), _align_down(addr + size)
            if hi > lo:
                self._uc.mem_protect(lo, hi - lo, UC_PROT_READ | UC_PROT_EXEC)
                protected.append((lo, hi - lo))
        self._protected = tuple(protected)

    def _apply_relocations(self):
        for name in (".rela.dyn", ".rela.plt"):
            sect = self._elf.get_section_by_name(name)
            if sect is None:
                continue
            for r in sect.iter_relocations():
                if r["r_info_type"] != R_AARCH64_RELATIVE:
                    continue  # statically-linked homebrew: nothing else expected
                addr = self.base + r["r_offset"]
                value = (self.base + r["r_addend"]) & 0xFFFFFFFFFFFFFFFF
                self._uc.mem_write(addr, struct.pack("<Q", value))

    def _find_nm(self, nm_tool):
        """devkitA64's nm, or the host one -- both read this ELF identically.

        Listing symbols needs no architecture, so a plain binutils nm manages
        an AArch64 object: same addresses, same sizes, two symbols apart out of
        four thousand, and a launch driven by either produces the same digests.

        Each candidate is tried rather than merely located: one that exists
        but cannot read the file is no use, and finding that out here beats a
        parse that silently yields nothing.
        """
        import os
        import shutil
        dkp = os.environ.get("DEVKITPRO", "/opt/devkitpro")
        candidates = [nm_tool,
                      os.path.join(dkp, "devkitA64", "bin", nm_tool),
                      "nm"]

        tried = []
        for c in candidates:
            path = shutil.which(c) or (c if os.path.isfile(c) and
                                       os.access(c, os.X_OK) else None)
            if not path:
                continue
            probe = subprocess.run([path, "-S", "--demangle", self.elf_path],
                                   capture_output=True, text=True)

            # nm exits 0 whatever happens: a stripped binary gives no output, an
            # unreadable one says so on stderr. So the question is whether it
            # understood the format, not whether it found anything -- a file
            # with no symbols is a real answer, and require_target is what turns
            # it into a refusal that names the binary.
            if "format not recognized" not in probe.stderr:
                return path
            tried.append(os.path.basename(path))
        raise FileNotFoundError(
            "no nm can read %s. Tried: %s. Install binutils, or devkitPro "
            "devkitA64 (and put its bin on PATH or set DEVKITPRO)."
            % (self.elf_path, ", ".join(tried) or "none found"))

    def _load_symbols(self, nm_tool):
        # -S also prints each symbol's size, which is what makes a data table in
        # the binary readable: without it there is no way to know how many
        # entries an array holds.
        out = subprocess.run([self._find_nm(nm_tool), "-S", "--demangle",
                              self.elf_path],
                             capture_output=True, text=True, check=True).stdout
        syms = {}
        self._symbol_sizes = {}

        for line in out.splitlines():
            parts = line.split(maxsplit=3)
            if len(parts) < 3:
                continue

            # With -S a sized symbol has four fields; an unsized one has three,
            # and its name may itself contain spaces.
            size = None
            if len(parts) == 4 and len(parts[1]) == len(parts[0]):
                addr_s, size_s, kind, name = parts
                try:
                    size = int(size_s, 16)
                except ValueError:
                    size = None
            else:
                addr_s, kind, name = parts[0], parts[1], " ".join(parts[2:])

            try:
                addr = int(addr_s, 16)
            except ValueError:
                continue
            if kind.lower() == "u":
                continue  # undefined

            syms[name] = self.base + addr
            if size is not None:
                self._symbol_sizes[name] = size

        return syms

    @property
    def symbols(self):
        """{name: address} for everything the binary defines, read-only.

        A view rather than the map: what the ELF says is not a caller's to
        edit, and a symbol added here would be a symbol nothing in the guest
        has.
        """
        return types.MappingProxyType(self._symbols)

    @property
    def symbol_sizes(self):
        """{name: size} for the symbols nm sized, read-only for the same
        reason."""
        return types.MappingProxyType(self._symbol_sizes)

    @property
    def code_ranges(self):
        """((address, size), ...) for the executable sections."""
        return self._code_ranges

    @property
    def protected(self):
        """((address, size), ...) mapped without write permission."""
        return self._protected

    # ------------------------------------------------------------------------
    # Finding things in it
    # ------------------------------------------------------------------------

    # Read a megabyte at a time. Chunks overlap by one needle less a byte, so a
    # match lying across a boundary is still found -- the failure a naive
    # chunked search has, and the reason to write it out rather than assume it.
    SCAN_CHUNK = 1 << 20

    def find_bytes(self, needle):
        """Where `needle` first appears in the loaded image, or None.

        Bytes, not symbols -- find() above searches the symbol table. This is
        for data the linker gave no name to: a constexpr table in an anonymous
        namespace exports nothing, so the only way to reach it is to recognise
        something in it.
        """
        at = self.load_lo
        while at < self.load_hi:
            n = min(self.SCAN_CHUNK, self.load_hi - at)

            # What is left cannot hold the needle, and stepping by n - overlap
            # would advance by nothing once n reaches the overlap. Both are the
            # same off-by-one, and the second spins forever rather than failing.
            if n < len(needle):
                break
            found = bytes(self._uc.mem_read(at, n)).find(needle)
            if found >= 0:
                return at + found
            at += n - (len(needle) - 1)
        return None

    def find(self, substr):
        """Return {name: addr} for every symbol whose demangled name contains substr."""
        return {n: a for n, a in self._symbols.items() if substr in n}

    # Compiler-generated artifacts that share a function's name but are not it:
    # lambda bodies, outlined clones, constant-propagated specializations.
    _ARTIFACT_MARKERS = ("{lambda", "[clone", "::operator()", ".constprop", ".isra", ".part")

    def find_one(self, substr):
        matches = self.find(substr)
        if len(matches) > 1:
            primary = {n: a for n, a in matches.items()
                       if not any(m in n for m in self._ARTIFACT_MARKERS)}
            if len(primary) == 1:
                matches = primary
        if len(matches) != 1:
            raise KeyError("expected exactly 1 symbol matching %r, got %r"
                           % (substr, list(matches)))
        return next(iter(matches.values()))

    def find_exact(self, name):
        return self._symbols[name]

    @staticmethod
    def _collapse(name, width=79):
        """A demangled C++ name with its template and argument lists elided.

        A diagnostic has to say which function faulted, and a nested template
        buries that: the file-context set demangles to two thousand characters
        of std::_Rb_tree<std::unique_ptr<...TrackedAllocator...>>, printed twice
        for pc and lr. Eliding the groups leaves the names, which is the part
        that identifies the code. Truncated after that only if still absurd.
        """
        out, depth = [], 0
        for ch in name:
            if ch in "<(":
                depth += 1
                if depth == 1:
                    out.append(ch + "..." + (">" if ch == "<" else ")"))
            elif ch in ">)":
                depth -= 1
            elif depth == 0:
                out.append(ch)
        short = "".join(out)
        return short if len(short) <= width else short[:width - 3] + "..."

    def nearest_symbol(self, addr):
        if addr == self._sentinel:
            return "outside the guest (top-level call)"
        best = None
        for name, a in self._symbols.items():
            if a <= addr and (best is None or a > best[1]):
                best = (name, a)

        # Naming a symbol a megabyte away is worse than admitting we don't
        # know: it sends whoever reads the diagnostic to the wrong function.
        if best is None or addr - best[1] > 0x10000:
            return "0x%x (no symbol nearby)" % addr
        return "%s+0x%x" % (self._collapse(best[0]), addr - best[1])

    # ------------------------------------------------------------------------
    # Memory it hands out
    # ------------------------------------------------------------------------

    def alloc(self, size, align=16):
        cur = (self._heap_cursor + align - 1) & ~(align - 1)
        if cur + size > self.heap_base + self.heap_size:
            raise MemoryError("bump heap exhausted")
        self._heap_cursor = cur + size

        # No explicit zeroing: this allocator never reuses memory, and Unicorn
        # zero-fills freshly mapped pages, so every block handed out is already
        # zero. (Reintroduce a memset here if reuse is ever added.)
        return cur

    # ------------------------------------------------------------------------
    # Standing in for a function
    # ------------------------------------------------------------------------

    def hook(self, symbol_substr, callback, exact=False):
        """callback(guest) is invoked instead of the real function body.

        It reads what it was called with off the guest and answers through it,
        which is why it is handed nothing else. Responsible for calling
        guest.ret(...) to resume at LR.
        """
        addr = self.find_exact(symbol_substr) if exact else self.find_one(symbol_substr)
        return self.hook_at(addr, callback, label=symbol_substr)

    def hook_at(self, addr, callback, label=None):
        """Like hook(), but for an address the caller already has.

        Every caller passes guest.symbols[name] -- an exact symbol address,
        rather than the substring search hook() does.
        """
        def tramp(uc, address, size, user_data):
            try:
                if getattr(self, "trace", False):
                    lr = uc.reg_read(UC_ARM64_REG_X30)
                    print("HOOK %s at 0x%x (lr=0x%x %s)"
                          % (label or hex(addr), address, lr,
                             self.nearest_symbol(lr)))
                callback(self)
            except BaseException as e:
                # A guest abort is a result rather than a bug on this side:
                # its message is the whole diagnostic, and a Python traceback
                # would only say how it travelled.
                if not isinstance(e, GuestAbort):
                    import traceback
                    traceback.print_exc()
                self._pending_exc = e
                uc.emu_stop()

        self._uc.hook_add(UC_HOOK_CODE, tramp, begin=addr, end=addr)
        self._hook_stubs[addr] = (label, callback)
        return addr

    def watch(self, addr, callback):
        """callback(guest) each time the guest reaches addr, which then runs.

        A hook answers in the target's place; a watch only looks. Nothing is
        skipped and nothing is returned, so the body executes exactly as it
        would have.
        """
        def seen(uc, address, size, user_data):
            callback(self)
        self._uc.hook_add(UC_HOOK_CODE, seen, begin=addr, end=addr)

    def watch_writes(self, begin, end, callback):
        """callback(guest, value) for each write of that range.

        The one thing a watch cannot do by standing at an address: catch a
        store wherever it is made from.
        """
        def written(uc, access, address, size, value, user_data):
            return callback(self, value)
        self._uc.hook_add(UC_HOOK_MEM_WRITE, written, begin=begin, end=end)

    def hooked_symbols(self):
        """Symbols that already have behaviour installed, so nothing tries to
        stub over them."""
        by_addr = {}
        for name, addr in self._symbols.items():
            by_addr.setdefault(addr, []).append(name)
        out = set()
        for addr in self._hook_stubs:
            out.update(by_addr.get(addr, ()))
        return out

    # x0 to x7 carry the first eight arguments, and x0 the return value:
    # AAPCS64, which is what the module is compiled to.
    _ARG_REGS = (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
                 UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X5,
                 UC_ARM64_REG_X6, UC_ARM64_REG_X7)

    def arg(self, n):
        """The nth argument of the call being hooked.

        By position in the convention rather than by register name: a hook
        cares that it was handed a path and a mode, not that they arrived in
        x1 and x2.
        """
        return self._uc.reg_read(self._ARG_REGS[n])

    def pc(self):
        """Where the guest is executing."""
        return self._uc.reg_read(UC_ARM64_REG_PC)

    def lr(self):
        """Where the call being hooked will return to."""
        return self._uc.reg_read(UC_ARM64_REG_X30)

    def sp(self):
        """The stack pointer, which is where a ninth argument would be."""
        return self._uc.reg_read(UC_ARM64_REG_SP)

    # The three ways a callback gives control back: answer, halt, or halt
    # and raise once the call it was standing in for returns.
    def ret(self, x0=0):
        """Simulate a `mov x0, #imm; ret` for a hooked function."""
        if x0 is not None:
            self._uc.reg_write(UC_ARM64_REG_X0, x0 & 0xFFFFFFFFFFFFFFFF)
        lr = self._uc.reg_read(UC_ARM64_REG_X30)
        self._uc.reg_write(UC_ARM64_REG_PC, lr)

    def stop(self):
        """Stop the run here.

        For a hook that raises on its own account: the exception it throws
        would be swallowed by the emulator, so the run is stopped first and
        the raise unwinds through call().
        """
        self._uc.emu_stop()

    def fail(self, exc):
        """Stop the run, and raise this when the call it was in returns.

        A hook that finds the build has given up cannot raise where it stands
        -- it runs inside the emulator, which swallows what it is handed -- so
        it says so here and the guest raises it on the way out.
        """
        self._pending_exc = exc
        self._uc.emu_stop()

    # ------------------------------------------------------------------------
    # Calling into the real binary
    # ------------------------------------------------------------------------

    def call(self, addr, args=(), indirect_result=None):
        """Call a real function in the loaded ELF. args go in X0.. in order;
        indirect_result, if given, is written to X8 (AAPCS64 non-trivial
        return-value convention). Returns X0 after the call."""
        sp = (self.stack_base + self.stack_size - 0x100) & ~0xF  # AAPCS64: aligned
        self._uc.reg_write(UC_ARM64_REG_SP, sp)
        self._uc.reg_write(UC_ARM64_REG_LR, self._sentinel)
        if indirect_result is not None:
            self._uc.reg_write(UC_ARM64_REG_X8, indirect_result)

        xregs = [UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3,
                 UC_ARM64_REG_X4, UC_ARM64_REG_X5, UC_ARM64_REG_X6, UC_ARM64_REG_X7]
        for reg, val in zip(xregs, args):
            self._uc.reg_write(reg, val & 0xFFFFFFFFFFFFFFFF)

        self._pending_exc = None
        try:
            self._uc.emu_start(addr, self._sentinel)
        except UcError:
            # A fault already recognised -- an abort storing its magic into
            # write-protected code, say -- is reported as what it is. Unicorn
            # knows it only as a protection error and raises before call()
            # could look; anything unrecognised is a real failure.
            if self._pending_exc is None:
                raise

        if self._pending_exc is not None:
            raise self._pending_exc
        return self._uc.reg_read(UC_ARM64_REG_X0)

    # ------------------------------------------------------------------------
    # Reading and writing its memory
    # ------------------------------------------------------------------------

    def read(self, addr, size):
        """Bytes of guest memory, as bytes rather than a mutable view.

        Here so nothing outside reaches through to the emulator for them: what
        the guest holds is the guest's to hand over.
        """
        return bytes(self._uc.mem_read(addr, size))

    def write(self, addr, data):
        """Put bytes into guest memory: the other half of read().

        bytes(), because Unicorn's binding refuses a bytearray and a caller
        holding one should not have to know that.
        """
        self._uc.mem_write(addr, bytes(data))

    def read_cstr(self, addr, maxlen=1024, chunk=256):
        """Read a NUL-terminated string in chunks. Reading byte-by-byte costs
        one Python->C round trip per character, which dominated profiles of
        path-heavy runs."""
        out = bytearray()
        while len(out) < maxlen:
            want = min(chunk, maxlen - len(out))
            try:
                buf = bytes(self._uc.mem_read(addr + len(out), want))
            except UcError:
                # Near the end of a mapping: fall back to a short read so a
                # string that ends just before unmapped memory still works.
                if want == 1:
                    break
                chunk = max(1, want // 4)
                continue

            nul = buf.find(b"\x00")
            if nul >= 0:
                out += buf[:nul]
                break
            out += buf
        return bytes(out).decode("utf-8", "replace")

    def u32(self, addr):
        return struct.unpack("<I", bytes(self._uc.mem_read(addr, 4)))[0]

    def u64(self, addr):
        return struct.unpack("<Q", self._uc.mem_read(addr, 8))[0]

    def w32(self, addr, val):
        self._uc.mem_write(addr, struct.pack("<I", val & 0xFFFFFFFF))

    def w64(self, addr, val):
        """Write a 64-bit value into guest memory.

        Masked rather than packed signed: a negative packed as "<q" and its
        masked self packed as "<Q" are the same eight bytes, so a size and a
        count need one writer between them.
        """
        self._uc.mem_write(addr, struct.pack("<Q", val & 0xFFFFFFFFFFFFFFFF))

    # ------------------------------------------------------------------------
    # When it faults
    # ------------------------------------------------------------------------

    # Syscalls with nothing observable to do when only one thread exists: no
    # waiter to wake, no holder to contend with, so "done" is accurate rather
    # than a fudge. Anything that would genuinely block is absent -- reaching
    # one means the target expects concurrency, which should be an error.
    SINGLE_THREADED_NOOP_SVCS = {
        0x1a: "svcArbitrateLock",
        0x1b: "svcArbitrateUnlock",
        0x1d: "svcSignalProcessWideKey",

        # Nothing here hands out real handles, so there is nothing to give back.
        0x16: "svcCloseHandle",
    }

    # Answered with zero, in X1 where the value goes. A build asking the kernel
    # what it may hold gets a figure that only a console has: there is no
    # resource limit here, no pool, and no other process drawing on either. A
    # zero reads as "not measured", which is the truth; a plausible number
    # would read as a measurement.
    #
    # Resumed after the svc rather than at lr, unlike the no-ops above: those
    # sit in wrappers that do nothing but return, and this one is followed by
    # the instructions that pop the saved out-pointer and store through it.
    # Jumping to lr skipped both, leaving stack rubbish in the answer and the
    # stack pointer short by the sixteen bytes the wrapper had pushed.
    ZERO_ANSWER_SVCS = {
        0x29: "svcGetInfo",
        0x6f: "svcGetSystemInfo",
        0x30: "svcGetResourceLimitLimitValue",
        0x31: "svcGetResourceLimitCurrentValue",
    }

    def _on_intr(self, uc, intno, user_data):
        pc = uc.reg_read(UC_ARM64_REG_PC)
        lr = uc.reg_read(UC_ARM64_REG_X30)

        # Only a real svc carries a syscall number. Decode the instruction and
        # check it actually is one: an INTR can also be an undefined
        # instruction, a BRK, or an alignment fault, and reporting those as
        # "unhandled syscall" sends the reader hunting for a missing stub that
        # was never the problem.
        svc_number, insn, is_svc = None, None, False
        try:
            insn = struct.unpack("<I", bytes(uc.mem_read(pc - 4, 4)))[0]
            is_svc = (insn & SVC_MASK) == SVC_OP
            if is_svc:
                svc_number = (insn >> SVC_IMM_SHIFT) & SVC_IMM_MASK
        except UcError:
            pass

        if blackbox.try_handle_svc(self, svc_number):
            return

        if svc_number in self.SINGLE_THREADED_NOOP_SVCS:
            uc.reg_write(UC_ARM64_REG_X0, 0)   # Result: success
            uc.reg_write(UC_ARM64_REG_PC, lr)
            return

        if svc_number in self.ZERO_ANSWER_SVCS:
            uc.reg_write(UC_ARM64_REG_X0, 0)   # Result: success
            uc.reg_write(UC_ARM64_REG_X1, 0)   # the value asked for
            return

        regs = [uc.reg_read(r) for r in (UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
                                         UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X5)]

        # Split like the memory fault above, and for the same reason: two
        # demangled names and six registers behind them is a line no terminal
        # shows whole. intno first, since it says which kind of fault this is.
        print()
        print("  INTR intno=%d" % intno)
        print("    pc 0x%x  %s" % (pc, self.nearest_symbol(pc)))
        print("    lr 0x%x  %s" % (lr, self.nearest_symbol(lr)))
        print("    x0-5 %s" % ", ".join("0x%x" % r for r in regs))
        self._print_backtrace()

        if self.ring:
            print("last executed pcs:")
            for a in self.ring[-40:]:
                print("  0x%x %s" % (a, self.nearest_symbol(a)))
        else:
            print("(for a last-executed-pc ring, build the Guest with "
                  "trace_ring=True)")

        if is_svc:
            raise RuntimeError(
                "unhandled svc 0x%x at pc=0x%x (%s), called from %s -- a service the "
                "sandbox does not provide was reached, and nothing derived a stub for "
                "it. Usually the namespace it lives in is not among those the caller "
                "treats as services."
                % (svc_number, pc, self.nearest_symbol(pc), self.nearest_symbol(lr)))

        raise RuntimeError(
            "CPU exception (intno=%d) at pc=0x%x (%s), called from %s. The instruction "
            "before pc is 0x%08x, which is not an svc -- this is a fault, not a missing "
            "stub: an undefined instruction, a BRK, or a misaligned access."
            % (intno, pc, self.nearest_symbol(pc), self.nearest_symbol(lr),
               insn if insn is not None else 0))

    def _on_mem_invalid(self, uc, access, address, size, value, user_data):
        pc = uc.reg_read(UC_ARM64_REG_PC)
        lr = uc.reg_read(UC_ARM64_REG_X30)
        sym = self.nearest_symbol(pc)

        # Only reachable when the image is loaded at a base that leaves the
        # abort magic address unmapped; at base 0 the write hook catches it.
        if blackbox.is_abort_store(address, value):
            return blackbox.on_abort_store(self, value)

        # Three lines: a demangled C++ name is long even collapsed, and two of
        # them behind the numbers put the fault past the width of any terminal.
        print()
        print("  MEM_INVALID access=%d addr=0x%x size=%d"
              % (access, address, size))
        print("    pc 0x%x  %s" % (pc, sym))
        print("    lr 0x%x  %s" % (lr, self.nearest_symbol(lr)))

        self._print_backtrace()
        return False

    def backtrace(self, limit=24):
        """Return addresses from the frame pointer chain, innermost first."""
        out = []
        fp = self._uc.reg_read(UC_ARM64_REG_X29)

        while fp and len(out) < limit:
            try:
                caller_fp, ret = self.u64(fp), self.u64(fp + 8)
            except UcError:
                break
            if ret == self._sentinel:
                break

            out.append(ret)
            if caller_fp <= fp:
                break
            fp = caller_fp
        return out

    def _print_backtrace(self, limit=24):
        """How the build reached the fault, from the frame pointer chain.

        A frame that does not grow upwards ends the walk: a corrupt pointer
        would otherwise print whatever it landed on as if it were a call.
        """
        frames = self.backtrace(limit)

        print()
        print("    Backtrace:")
        for ret in frames:
            print("      0x%-10x %s" % (ret, self.nearest_symbol(ret)))
        if not frames:
            print("      (no frame pointer chain from here)")
