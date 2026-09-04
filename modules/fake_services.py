#!/usr/bin/env python3
"""Every service the module talks to: the libnx fs* C API backed by the fake
SD, the ncm answers that say what is installed, the settings items the SD
carries, and refusals for everything else this sandbox does not run.

Named for the role rather than the mechanism. All of it happens to be
installed as hooks, but what it *is* is the operating system the module wakes
up on.

Everything intercepts at the *symbol* boundary, so the real function body never
runs and this file is the entire environment the target sees. Struct layouts
for FsFile/FsDir/FsFileSystem are our own choice (an 8-byte handle id at offset
0) because every function that reads or writes them is hooked here too -- only
FsDirectoryEntry has to match libnx's real layout exactly, since unhooked,
genuinely-compiled code (WalkSdTree) reads its fields directly.

Calls across the fs* boundary are counted here, since that is where they
happen. Memory is not: it is read back from the module's own heaps
afterwards.
"""
import collections
import struct

FS_DIRENT_NAME_MAX = 0x301
FS_DIRENT_SIZE = 0x310  # name[0x301] + pad[3] + type(s8) + pad2[3] + file_size(s64)

# Exact rather than "some nonzero value": ams catches specific Results and
# converts them, so a made-up code takes the wrong branch. Module | desc<<9,
# with fs as module 2.
RESULT_PATH_NOT_FOUND = 2 | (1 << 9)
RESULT_PATH_ALREADY_EXISTS = 2 | (2 << 9)

# fs::ResultInvalidOpenMode. Horizon refuses a read through a write-only handle
# and vice versa. Let both through and a handle opened with the wrong mode
# behaves perfectly here while killing a game on hardware.
RESULT_INVALID_OPEN_MODE = 2 | (6072 << 9)

OPEN_MODE_READ = 1
OPEN_MODE_WRITE = 2

# The two the settings store is read through, and the whole of it: everything
# below them is where the values came from, which is this file's business
# rather than the target's.
SETTINGS_ITEM_VALUE = ("ams::settings::fwdbg::GetSettingsItemValue"
                       "(void*, unsigned long, char const*, char const*)")
SETTINGS_ITEM_SIZE = ("ams::settings::fwdbg::GetSettingsItemValueSize"
                      "(char const*, char const*)")

# What system_settings.ini spells a value with, and how wide each one is.
SETTING_WIDTH = {"u8": 1, "u16": 2, "u32": 4, "u64": 8}

# What a good one looks like, which is the whole of what a reader who typed a
# bad one needs. Whether the type was unknown, the number too wide for it or
# the digits not a whole number of bytes, the line to write is the same.
SETTING_FORM = "type!value -- u8!0x1, u16/u32/u64 likewise, str!text, hex!00ff"


def settings_from_ini(text):
    """{(name, key): "type!value"} for what a system_settings.ini says.

    A section is a settings name and the lines under it are its items, which
    is how the SD spells the store the entry points below are asked for.
    Values are carried across as written and refused where they are used, so
    an ini holding an item this run never asks for is not one this run
    refuses to start on.
    """
    store, name = {}, None
    for line in text.splitlines():
        line = line.split(";", 1)[0].split("#", 1)[0].strip()
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
        elif name and "=" in line:
            key, _, value = line.partition("=")
            store[(name, key.strip())] = value.strip()
    return store


def parse_setting_value(text):
    """The bytes a `type!value` stands for, spelled as the ini spells it.

    Taken in the ini's own syntax so a line can be copied off one unchanged
    -- an item is a width and a value, and a build reading one back expects
    exactly the bytes the store holds. A string carries its terminator,
    because that is what the store holds and what a caller reads.
    """
    kind, sep, value = text.partition("!")
    kind = kind.strip().lower()
    if sep and value:
        if kind in ("str", "string"):
            return value.encode("utf-8") + b"\x00"
        if kind in ("hex", "bytes"):
            if not len(value) % 2 and not value.strip("0123456789abcdefABCDEF"):
                return bytes.fromhex(value)
        elif kind in SETTING_WIDTH:
            try:
                return int(value, 0).to_bytes(SETTING_WIDTH[kind], "little")
            except (ValueError, OverflowError):
                pass
    raise ValueError("a value is %s" % SETTING_FORM)


# What a range names when the bytes did not come from a file: the game's own
# romfs, which fs hands over as a storage rather than as something on the SD.
BASE_ROMFS = "<base romfs>"

Range = collections.namedtuple("Range", "target offset size written")

# A call that named a file rather than moving bytes through it: opening one,
# closing it, making it, removing it. What it costs is the crossing itself.
Touch = collections.namedtuple("Touch", "call path")


class Stats:
    """Calls across the fs* boundary -- the ones that cross to fsp-srv on
    hardware.

    Three things, because they answer different questions. `total` is how many
    times the boundary was crossed; `calls` is which entry points did it, which
    is what says whether a cost is reads, writes or directory walking; `moved`
    is the bytes that actually went across, which is the one a console pays for
    in time rather than in call overhead.

    All three are snapshotted per phase, so anything reading the finished
    romfs back afterwards lands outside the totals the build is credited with.
    Nothing outside the hooks touches any of it while a phase is running.
    """

    def __init__(self):
        self.total = 0
        self.calls = {}
        self.moved = 0
        self.written_bytes = 0
        self.ranges = []
        self.touches = []
        self._current = None

    def fs_call(self, name):
        self.total += 1
        self.calls[name] = self.calls.get(name, 0) + 1
        self._current = name

    def at_path(self, path):
        """The file the call in progress is about.

        Named by the hook rather than passed down: which call it is was
        settled a line earlier, since fs_call is what every hook enters
        through.
        """
        self.touches.append(Touch(self._current, path))

    def crossed(self, nbytes, written=False, target=None, offset=None):
        """Bytes over the boundary, counted where they actually move.

        Writes are also counted on their own, because they are the only half
        that wears an SD: flash is erased and reprogrammed to take them,
        and a read costs it nothing. A build that reads more and writes less
        is gentler on the hardware however busy the call count looks.

        Given a target and an offset, the range is kept as well. A count says
        how busy a build was; the ranges say which part of a file it was busy
        with, and whether two of them cover the same bytes twice -- which is
        work the SD does and the build gets nothing for.
        """
        self.moved += nbytes
        if written:
            self.written_bytes += nbytes
        if target is not None:
            self.ranges.append(Range(target, offset, nbytes, written))


class _Handles:
    def __init__(self):
        self._next = 1
        self.dirs = {}   # id -> {"entries": [(name,is_dir,size)], "cursor": int}
        self.files = {}  # id -> {"path": str, "mode": int}

    def new_id(self):
        h = self._next
        self._next += 1
        return h


def install(guest, sd, stats, meta_db, settings=None):
    """Answer the three services this environment serves for real.

    They have little to do with each other: settings is where the SD tells it
    how to behave, ncm is what it asks about the game before deciding whether
    anything it saved earlier still applies, and fs is what the build is here
    to use. Everything else a build reaches is refused instead, by
    stub_service_boundary below. The allocator family is left alone, so
    memory can be measured rather than modelled.

    Once per guest, and a second call returns having done nothing: hooking
    twice would count every call twice and start a second handle table at one,
    so an id handed out by one is a live id in the other -- and none of that
    announces itself. Asking twice is not a mistake, though, so it is
    idempotent rather than refusing.

    Marked on the guest because that is whose fact it is: a guest has services
    or it does not, and it stops having them when it is gone. An attribute has
    that lifetime without anything arranging it.
    """
    if getattr(guest, "fake_services_installed", False):
        return
    guest.fake_services_installed = True

    # One handle table between them: both hand ids into guest memory, and two
    # tables would each start at 1, so a build holding an ncm handle and an fs
    # handle would be holding the same number for two different things.
    handles = _Handles()
    _install_settings(guest, settings or {})
    _install_ncm(guest, meta_db, handles)
    _install_fs(guest, sd, stats, handles)


def _install_settings(guest, settings):
    """{(name, key): bytes} as the items a build can ask for.

    Hooked at the ams entry point rather than at the key-value store beneath
    it: that store is an ini parser and an allocator over the SD, and a build
    is not here to exercise either. The items arrive already read -- what a
    value looks like is spelled below, but where they came from and which of
    them are worth keeping is the caller's to know.

    An item nobody set is absent rather than empty, and absent is nothing
    written and a length of zero -- which is what a caller tests before
    falling back to whatever it does without one. Refusing instead would make
    every unset item an abort, when not setting one is the normal case.
    """
    store = dict(settings)

    def value_for(guest, name_at, key_at):
        """The item those two arguments name, or None if nothing set it."""
        return store.get((guest.read_cstr(guest.arg(name_at)),
                          guest.read_cstr(guest.arg(key_at))))

    def hk_value(guest):
        dst = guest.arg(0)
        dst_size = guest.arg(1)
        value = value_for(guest, 2, 3)
        if value is None:
            guest.ret(0)
            return

        # Cut to the buffer rather than refused, as the store does, and the
        # length that comes back is what was written.
        value = value[:dst_size]
        guest.write(dst, value)
        guest.ret(len(value))

    def hk_size(guest):
        value = value_for(guest, 0, 1)
        guest.ret(0 if value is None else len(value))

    guest.hook(SETTINGS_ITEM_VALUE, hk_value, exact=True)
    guest.hook(SETTINGS_ITEM_SIZE, hk_size, exact=True)


def _install_ncm(guest, meta_db, h):
    """What the system reports as installed, asked at launch.

    Hooked at the libnx entry points rather than at Atmosphere's ncm client, so
    the client and its sf plumbing run for real and nothing here depends on how
    a given build lays those objects out.
    """

    def _ncm_hook(fn):
        guest.hook(fn.__name__, fn, exact=True)

    # Which storage each open handle was opened for, since a query names no
    # storage of its own.
    _OPENED_FOR = {}

    # Where a key keeps what kind of content it is: 0x0 id, 0x8 version, 0xC
    # type. Read off the caller's key rather than looked up, because what it
    # asks about is the key it is holding.
    META_TYPE_OFFSET = 0xC

    @_ncm_hook
    def ncmOpenContentMetaDatabase(guest):
        out = guest.arg(0)
        storage_id = guest.arg(1) & 0xFF
        hid = h.new_id()
        _OPENED_FOR[hid] = storage_id

        # libnx Service: session handle, own_handle, object_id,
        # pointer_buffer_size. The middle two stay zero so closing one does
        # nothing -- no handle to give back, no object to send a close to.
        guest.write(out, struct.pack("<IIIHH", hid, 0, 0, 0, 0))
        guest.ret(0)

    @_ncm_hook
    def ncmInitialize(guest):
        guest.ret(0)

    @_ncm_hook
    def ncmExit(guest):
        guest.ret(0)

    @_ncm_hook
    def ncmContentMetaDatabaseClose(guest):
        guest.ret(0)

    @_ncm_hook
    def ncmContentMetaDatabaseGetLatestContentMetaKey(guest):
        db = guest.arg(0)
        out = guest.arg(1)
        want = guest.arg(2)

        # A database is opened for a storage and answers only for
        # that one. Content is installed to internal user storage
        # here, so one opened for anything else holds nothing.
        storage_id = _OPENED_FOR.get(guest.u32(db), 0)
        key = (meta_db.get_latest(want)
               if storage_id == meta_db.BUILT_IN_USER else None)
        if key is None:
            guest.ret(meta_db.RESULT_CONTENT_META_NOT_FOUND)
            return
        guest.write(out, key.packed())
        guest.ret(0)

    @_ncm_hook
    def ncmContentMetaDatabaseGetPatchContentMetaId(guest):
        # (db, out_patch_id, key). The key names the title and says what kind it
        # is, and only two kinds carry a patch id at all -- a key of any other
        # kind is refused rather than answered.
        out = guest.arg(1)
        key = guest.arg(2)
        program_id = guest.u64(key)
        meta_type = guest.read(key + META_TYPE_OFFSET, 1)[0]

        patch_id = meta_db.get_patch_id(program_id, meta_type)
        if patch_id is None:
            guest.ret(meta_db.RESULT_INVALID_CONTENT_META_KEY)
            return
        guest.write(out, struct.pack("<Q", patch_id))
        guest.ret(0)

    @_ncm_hook
    def ncmContentMetaDatabaseList(guest):
        # (db, out_total, out_written, out_keys, count, meta_type, app_id,
        # id_min, id_max, install_type). Eight arrive in registers and the last
        # two on the stack, where a call leaves them: the hook runs at the entry,
        # before a prologue moves anything.
        #
        # All of them are filters, and total counts what passes them rather than
        # what fitted, so a caller comparing the two knows whether to ask again.
        db = guest.arg(0)
        out_total = guest.arg(1)
        out_written = guest.arg(2)
        out_keys = guest.arg(3)
        count = guest.arg(4) & 0xFFFFFFFF
        meta_type = guest.arg(5) & 0xFF
        application_id = guest.arg(6)
        id_min = guest.arg(7)
        sp = guest.sp()
        id_max = guest.u64(sp)
        install_type = guest.u32(sp + 8) & 0xFF

        storage_id = _OPENED_FOR.get(guest.u32(db), 0)
        keys = (meta_db.list_content_meta(meta_type, application_id, id_min,
                                          id_max, install_type)
                if storage_id == meta_db.BUILT_IN_USER else [])

        written = min(len(keys), count)
        guest.write(out_keys, b"".join(k.packed() for k in keys[:written]))
        guest.write(out_total, struct.pack("<i", len(keys)))
        guest.write(out_written, struct.pack("<i", written))
        guest.ret(0)


def _install_fs(guest, sd, stats, h):
    """The libnx fs* C API, backed by the fake SD and counted as it is crossed."""

    def get_handle(ptr):
        return guest.u64(ptr)

    def set_handle(ptr, hid):
        guest.write(ptr, struct.pack("<Q", hid))

    def fs_hook(fn):
        """Install a hook on the libnx entry point this function is named
        after, and count the crossing."""
        def counted(guest):
            stats.fs_call(fn.__name__)
            return fn(guest)
        guest.hook(fn.__name__, counted, exact=True)
        return fn

    # ---- FsStorage: the base game's romfs, as fs hands it over ----
    #
    # What the layered storage is given and reads the game through. Contents
    # live on the host, so a full-size game costs nothing here.

    @fs_hook
    def fsStorageRead(guest):
        off = guest.arg(1)
        buf = guest.arg(2)
        size = guest.arg(3)
        image = sd.base_storage

        # The image knows its own shape: header, then a file partition it
        # invents, then the tables past the end of that. bytes(), because
        # Unicorn's ctypes binding rejects a bytearray.
        chunk = image.read(off, size) if image is not None else b""
        stats.crossed(len(chunk), target=BASE_ROMFS, offset=off)
        guest.write(buf, bytes(chunk))
        guest.ret(0)

    @fs_hook
    def fsStorageGetSize(guest):
        # The image's own figure. len() of one is a TypeError -- what the SD
        # holds here is a romfs that serves itself, not a block of bytes.
        image = sd.base_storage
        guest.w64(guest.arg(1), image.size if image is not None else 0)
        guest.ret(0)

    @fs_hook
    def fsStorageClose(guest):
        guest.ret(None)

    # ---- the SD ----

    @fs_hook
    def fsOpenSdCardFileSystem(guest):
        out = guest.arg(0)

        # Non-zero Service.session, so real code's serviceIsActive() sees a
        # live one.
        guest.w32(out, 1)
        guest.ret(0)

    @fs_hook
    def fsFsClose(guest):
        guest.ret(None)

    @fs_hook
    def fsFsOpenDirectory(guest):
        path_ptr = guest.arg(1)
        mode = guest.arg(2)
        out_ptr = guest.arg(3)
        node = sd.find(guest.read_cstr(path_ptr))
        if node is None or not node.is_dir:
            guest.ret(RESULT_PATH_NOT_FOUND)
            return
        read_dirs = bool(mode & 1)   # FsDirOpenMode_ReadDirs
        read_files = bool(mode & 2)  # FsDirOpenMode_ReadFiles

        # By name, which is this file's choice rather than what an SD does: a
        # filesystem hands entries over in the order it holds them. Chosen so a
        # run is reproducible, and worth knowing when reading a build that
        # hashes a tree in the order it walks it -- what that costs on an SD
        # whose order differs is not a question a run can put.
        entries = []
        for name, child in sorted(node.children.items()):
            if child.is_dir and not read_dirs:
                continue
            if not child.is_dir and not read_files:
                continue
            entries.append((name, child.is_dir, 0 if child.is_dir else child.size))
        hid = h.new_id()
        h.dirs[hid] = {"entries": entries, "cursor": 0}
        set_handle(out_ptr, hid)
        guest.ret(0)

    @fs_hook
    def fsDirRead(guest):
        d_ptr = guest.arg(0)
        total_ptr = guest.arg(1)
        max_entries = guest.arg(2)
        buf_ptr = guest.arg(3)
        dh = h.dirs[get_handle(d_ptr)]
        chunk = dh["entries"][dh["cursor"]: dh["cursor"] + max_entries]
        dh["cursor"] += len(chunk)

        # Filled host-side and handed over in one write. The hottest hook
        # there is, and a field-at-a-time version cost five guest writes per
        # entry -- the only code of ours a profile of the run could see. A
        # zeroed buffer leaves the pad bytes and terminator already right.
        out = bytearray(len(chunk) * FS_DIRENT_SIZE)

        for i, (name, is_dir, size) in enumerate(chunk):
            base = i * FS_DIRENT_SIZE
            name_bytes = name.encode("utf-8")[:FS_DIRENT_NAME_MAX - 1]
            out[base: base + len(name_bytes)] = name_bytes
            out[base + 0x304] = 0 if is_dir else 1   # FsDirEntryType_Dir / _File
            out[base + 0x308: base + 0x310] = struct.pack("<q", size)
        guest.write(buf_ptr, bytes(out))
        guest.w64(total_ptr, len(chunk))
        guest.ret(0)

    @fs_hook
    def fsDirClose(guest):
        guest.ret(None)

    @fs_hook
    def fsDirGetEntryCount(guest):
        d_ptr = guest.arg(0)
        out_ptr = guest.arg(1)
        dh = h.dirs[get_handle(d_ptr)]

        # libnx semantics: total entries for the handle, independent of cursor.
        guest.w64(out_ptr, len(dh["entries"]))
        guest.ret(0)

    @fs_hook
    def fsFsCreateDirectory(guest):
        path = guest.read_cstr(guest.arg(1))
        if sd.find(path) is not None:
            guest.ret(RESULT_PATH_ALREADY_EXISTS)
            return
        sd.ensure_dir(path)
        guest.ret(0)

    # ---- files ----

    @fs_hook
    def fsFsOpenFile(guest):
        path_ptr = guest.arg(1)
        out_ptr = guest.arg(3)
        mode = guest.arg(2)
        path = guest.read_cstr(path_ptr)
        node = sd.find(path)

        # Recorded whether or not it is there: asking for a file a console
        # does not have costs the same crossing as asking for one it does.
        stats.at_path(path)
        if node is None or node.is_dir:
            guest.ret(RESULT_PATH_NOT_FOUND)
            return
        hid = h.new_id()
        h.files[hid] = {"path": path, "mode": mode}
        set_handle(out_ptr, hid)
        guest.ret(0)

    @fs_hook
    def fsFsCreateFile(guest):
        path_ptr = guest.arg(1)
        size = guest.arg(2)
        path = guest.read_cstr(path_ptr)
        sd.put_file(path, b"\x00" * size)
        sd.note_written(path)
        stats.at_path(path)
        guest.ret(0)

    def file_call(mode):
        """(path, node, off, buf, size) for a file call, or None if refused.

        Both directions arrive the same way -- handle, offset, buffer, length in
        x0..x3 -- and both have to be refused through a handle opened the other
        way. That check lives here rather than at each call site, where the two
        would have to agree and eventually would not.
        """
        entry = h.files[get_handle(guest.arg(0))]
        if not entry.get("mode", mode) & mode:
            return None
        path = entry["path"]
        return (path, sd.find(path), guest.arg(1),
                guest.arg(2), guest.arg(3))

    @fs_hook
    def fsFileRead(guest):
        got = file_call(OPEN_MODE_READ)
        if got is None:
            guest.ret(RESULT_INVALID_OPEN_MODE)
            return
        path, node, off, buf, size = got
        out_read = guest.arg(5)
        chunk = bytes(node.data[off: off + size])
        if len(chunk) < size and node.size > off + len(chunk):
            # Sparse: the SD knows the size but holds no bytes, so invent the
            # ones this path is meant to have.
            want = min(size, node.size - off) - len(chunk)
            chunk = chunk + node.bytes_past(path, off + len(chunk), want)
        stats.crossed(len(chunk), target=path, offset=off)
        guest.write(buf, chunk)
        guest.w64(out_read, len(chunk))
        guest.ret(0)

    @fs_hook
    def fsFileWrite(guest):
        got = file_call(OPEN_MODE_WRITE)
        if got is None:
            guest.ret(RESULT_INVALID_OPEN_MODE)
            return
        path, node, off, buf, size = got
        sd.note_written(path)
        if len(node.data) < off + size:
            node.data.extend(b"\x00" * (off + size - len(node.data)))
        node.data[off: off + size] = guest.read(buf, size)
        stats.crossed(size, written=True, target=path, offset=off)
        node.size = len(node.data)
        guest.ret(0)

    @fs_hook
    def fsFileFlush(guest):
        guest.ret(0)

    @fs_hook
    def fsFileGetSize(guest):
        f_ptr = guest.arg(0)
        out_ptr = guest.arg(1)
        node = sd.find(h.files[get_handle(f_ptr)]["path"])
        guest.w64(out_ptr, node.size)
        guest.ret(0)

    @fs_hook
    def fsFileSetSize(guest):
        f_ptr = guest.arg(0)
        size = guest.arg(1)
        node = sd.find(h.files[get_handle(f_ptr)]["path"])
        if size < len(node.data):
            del node.data[size:]
        else:
            node.data.extend(bytearray(size - len(node.data)))
        node.size = size
        guest.ret(0)

    @fs_hook
    def fsFileClose(guest):
        entry = h.files.get(get_handle(guest.arg(0)))
        if entry is not None:
            stats.at_path(entry["path"])
        guest.ret(None)

    @fs_hook
    def fsFsDeleteFile(guest):
        path_ptr = guest.arg(1)
        path = guest.read_cstr(path_ptr)
        stats.at_path(path)
        if sd.find(path) is None:
            guest.ret(RESULT_PATH_NOT_FOUND)
            return
        sd.remove(path)
        guest.ret(0)

    # ---- the access log ----
    #
    # fs can be told to log every call it serves to the SD. It is off unless
    # something turns it on, and nothing here does -- but these are bare libnx
    # names that no stub rule covers, so left alone they would reach fsp-srv
    # and be reported as a service the sandbox does not provide.

    @fs_hook
    def fsGetGlobalAccessLogMode(guest):
        # Off, which is what it is: there is no access log on this SD.
        guest.w32(guest.arg(0), 0)
        guest.ret(0)

    @fs_hook
    def fsSetGlobalAccessLogMode(guest):
        guest.ret(0)

    @fs_hook
    def fsOutputAccessLogToSdCard(guest):
        # Kept rather than dropped: a build that logs despite the mode being
        # off has something to say, and it says it here.
        guest.debug_output.append(
            guest.read_cstr(guest.arg(0)))
        guest.ret(0)


def stub_service_boundary(guest, boundaries):
    """Answer the system services this environment does not provide.

    `boundaries` is [(symbol, fills in its first argument)], from the ELF by
    whoever knows which namespaces are services. This file supplies the other
    half: what a refused call does. Deciding that a symbol leaves the process
    is a question about ARM64; deciding that the answer is a non-zero Result
    with a valid empty out-param is a question about Horizon, and only the
    second one belongs here.

    There is deliberately no hand-written override list: if a stub is wrong,
    the rule that produced it is wrong, and the rule is what should be fixed.

    Returns {symbol: times called}, so a caller can show which stubs actually
    fired rather than leaving that invisible.
    """
    fired = {}

    def install_stub(sym, fills_out_param):
        def hook(guest):
            fired[sym] = fired.get(sym, 0) + 1
            if fills_out_param:
                # The caller owns (and will destroy) whatever this fills in, so
                # a refused call still has to leave it in a valid, empty state.
                out = guest.arg(0)
                if out:
                    guest.write(out, bytes(0x10))
            guest.ret(1)  # non-zero Result: service unavailable
        guest.hook(sym, hook, exact=True)

    for sym, fills_out_param in boundaries:
        install_stub(sym, fills_out_param)
    return fired
