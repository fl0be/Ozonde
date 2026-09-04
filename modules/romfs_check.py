#!/usr/bin/env python3
"""Read back the romfs a build produced, the way a game would.

Comparing `romfs_metadata.bin` byte for byte only works while the table
*layout* is unchanged: a build that lays its entries out differently and serves
exactly the same files would fail that test, and one that lays them out
identically while serving the wrong bytes would pass it.

So this asks the question that actually matters: mount the result and list what
is in it. Reads go through the module's own LayeredRomfsStorageImpl::Read, so
what is enumerated here is what a game would see, rather than what anything
outside believes was written.

Three things are checked, because a romfs can be wrong in three ways that do
not show in each other:

  * the entry links -- parent, sibling, child, file -- which is how the tables
    are walked, and what the digest is taken over, together with the arithmetic
    a walk trusts: extents inside the partition, names, and no entry twice;
  * the hash tables, which is how a real RomFs parser finds a file by name.
    Nothing in the link walk touches them, so a build could rebuild them wrongly
    and the digest would never notice;
  * the bytes behind a sample of the entries, since all of the above can be
    right while a file is wired to somebody else's data. Every file the mod
    delivers is in that sample, so this is also what says whether each
    delivery arrived at all.
"""
import itertools
import operator
import hashlib
import struct

from romfs_format import (DIR_ENTRY_SIZE, DIR_ENTRY_STRUCT,
                          DIR_HASH_LINK_OFFSET, EMPTY, FILE_DATA_ALIGN,
                          FILE_ENTRY_SIZE, FILE_ENTRY_STRUCT,
                          FILE_HASH_LINK_OFFSET, HEADER_SIZE,
                          HEADER_STRUCT, Header, LINK_STRUCT,
                          LINK_SIZE, PARENT_LINK_OFFSET,
                          SIBLING_LINK_OFFSET, DIR_CHILD_LINK_OFFSET,
                          DIR_FIRST_FILE_LINK_OFFSET, ROOT_ENTRY_OFFSET,
                          TABLE_ALIGN, align_up, path_hash)


class CheckResult:
    """What a check counted, so a caller can see it had something to check.

    files and dirs are what the hash tables led to. The other three are where
    a sampled file's bytes came from: mod_loose, matched against what the SD
    holds at that path; mod_bin, which carries the packed mod's tag; and base,
    the game underneath. Both mods are the mod -- one delivery arrives as files
    on the SD, the other packed into romfs.bin -- so both are named for it.

    Fields rather than a tuple, for the reason _Entry has them: every one of
    these is a plausible-looking number at every other position, so an index
    off by one reads as a smaller count rather than as a mistake.
    """

    __slots__ = ("files", "dirs", "mod_loose", "mod_bin", "base")

    def __init__(self, files, dirs, mod_loose, mod_bin, base):
        self.files = files
        self.dirs = dirs
        self.mod_loose = mod_loose
        self.mod_bin = mod_bin
        self.base = base

    def __repr__(self):
        return ("CheckResult(files=%d, dirs=%d, mod_loose=%d, mod_bin=%d, "
                "base=%d)" % (self.files, self.dirs, self.mod_loose,
                              self.mod_bin, self.base))


class Description:
    """What a romfs is: what it holds, and two digests of it.

    The first hashes paths, sizes and offsets. The second drops the offsets, so
    it is the one that has to match stock across a build laying the file
    partition out differently while serving the same filesystem.

    Two of the four are digests and two are numbers, which is exactly the shape
    where a swapped pair prints something entirely reasonable.
    """

    __slots__ = ("files", "total_size", "digest", "content_digest")

    def __init__(self, files, total_size, digest, content_digest):
        self.files = files
        self.total_size = total_size
        self.digest = digest
        self.content_digest = content_digest

    def __repr__(self):
        return ("Description(files=%d, total_size=%d, digest=%s...)"
                % (self.files, self.total_size, self.digest[:12]))


class RomfsCorrupt(Exception):
    """The romfs a build produced is not one a game could read.

    Its own class so a caller can tell it from a bug of its own: figures taken
    over a romfs like this are not a measurement.
    """


def _dir_entry(table, off):
    (_parent, sibling, child, first_file,
     _link, name_len) = struct.unpack_from(DIR_ENTRY_STRUCT, table, off)
    name = table[off + DIR_ENTRY_SIZE: off + DIR_ENTRY_SIZE + name_len].decode("utf-8", "replace")
    return sibling, child, first_file, name


def _file_entry(table, off):
    (_parent, sibling, offset, size,
     _link, name_len) = struct.unpack_from(FILE_ENTRY_STRUCT, table, off)
    name = table[off + FILE_ENTRY_SIZE: off + FILE_ENTRY_SIZE + name_len].decode("utf-8", "replace")
    return sibling, offset, size, name


def _tables(romfs):
    """(header, dir table, file table) of the produced romfs."""
    header = Header(*struct.unpack(HEADER_STRUCT, romfs.read(0, HEADER_SIZE)))
    dirs = romfs.read(header.dir_table_offset, header.dir_table_size)
    files = romfs.read(header.file_table_offset, header.file_table_size)
    return header, dirs, files


def produced_header(romfs):
    """The header of the romfs that build produced.

    Its own layout, not one the harness chose, and only knowable by reading it
    back -- which is what makes it the thing to bucket the build's writes
    against.
    """
    return _tables(romfs)[0]


class _Entry:
    """One file as the romfs lists it: where it is, how big, and where it sits.

    __slots__ rather than a tuple because the fields have names worth using --
    e[1] says nothing -- and because it is smaller, which a romfs of this many
    entries is worth caring about.
    """

    __slots__ = ("path", "size", "offset")

    def __init__(self, path, size, offset):
        self.path = path
        self.size = size
        self.offset = offset

    def __repr__(self):
        return "_Entry(%r, %d, %d)" % (self.path, self.size, self.offset)


def _listing(romfs, loaded=None):
    """[_Entry] for every file in the produced romfs, sorted.

    Walks the entry links rather than scanning the tables, because the links
    are the part a game follows -- a table with correct entries and a broken
    chain would still be a broken romfs.
    """
    _header, dirs, files = loaded if loaded is not None else _tables(romfs)

    out = []
    stack = [(ROOT_ENTRY_OFFSET, "")]
    while stack:
        dir_off, prefix = stack.pop()
        _sibling, child, first_file, _name = _dir_entry(dirs, dir_off)

        # Where a chain has been, not how long it has run: a bound says a walk
        # is too long to be honest, a visited set says which entry it came back
        # to. Either stops the walk -- these tables are what is under test, and
        # an unbounded walk over a corrupt one never returns.
        f, seen = first_file, set()
        while f != EMPTY:
            if f in seen:
                raise RomfsCorrupt("the file chain under %r returns to 0x%x "
                                   "after %d entries"
                                   % (prefix or "/", f, len(seen)))
            seen.add(f)

            sib, offset, size, name = _file_entry(files, f)
            out.append(_Entry(prefix + "/" + name, size, offset))
            f = sib

        c, seen = child, set()
        while c != EMPTY:
            if c in seen:
                raise RomfsCorrupt("the directory chain under %r returns to "
                                   "0x%x after %d entries"
                                   % (prefix or "/", c, len(seen)))
            seen.add(c)

            c_sibling, _c_child, _c_file, c_name = _dir_entry(dirs, c)
            stack.append((c, prefix + "/" + c_name))
            c = c_sibling

    # Sorted here rather than by the class: an entry has no natural order, and
    # one that read as natural would be whatever the fields were declared in.
    #
    # By the path alone, not a tuple of every field -- that builds one tuple
    # per entry, nineteen megabytes of temporary at a full base. Paths are
    # unique, so the path is a total order by itself.
    out.sort(key=operator.attrgetter("path"))
    return out


def _check_structure(loaded, entries):
    """The arithmetic a reader trusts: regions, extents, names, and no repeats.

    Every entry, not a sample: this reads nothing back, so covering all of them
    costs what covering two hundred would. A file wired outside the partition,
    or over its neighbour, is invisible to a walk -- the links are intact and
    the listing is right, and the game gets someone else's bytes.
    """
    header, dirs, files = loaded

    if header.size != HEADER_SIZE:
        raise RomfsCorrupt("header says it is %d bytes, not %d"
                           % (header.size, HEADER_SIZE))

    # Where the data is, which every file offset is relative to. Read straight
    # out of the header and into a read, so it is worth one look on the way.
    if header.file_data_offset < HEADER_SIZE:
        raise RomfsCorrupt("the file data starts at %d, inside the header"
                           % header.file_data_offset)

    # Named regions, so a failure says which of the four is wrong.
    regions = [(header.dir_hash_offset, header.dir_hash_size,
                "directory hash table"),
               (header.dir_table_offset, header.dir_table_size,
                "directory table"),
               (header.file_hash_offset, header.file_hash_size,
                "file hash table"),
               (header.file_table_offset, header.file_table_size,
                "file table")]

    for off, size, name in regions:
        if off < HEADER_SIZE or size < 0:
            raise RomfsCorrupt("the %s starts at %d for %d bytes"
                               % (name, off, size))

    for (a_off, a_size, a_name), (b_off, b_size, b_name) in zip(
            sorted(regions), sorted(regions)[1:]):
        if a_off + a_size > b_off:
            raise RomfsCorrupt("the %s and the %s overlap" % (a_name, b_name))

    # A bucket count of zero divides by zero in every lookup a game makes.
    for off, size, name in regions[:1] + regions[2:3]:
        if size < LINK_SIZE or size % LINK_SIZE:
            raise RomfsCorrupt("the %s holds %d bytes, which is not whole "
                               "buckets" % (name, size))

    # File extents, in offset order: back to back is what the builder lays
    # down, so anything reaching into the next file is a wire crossed.
    # The entries themselves in offset order, not copies of their fields: a key
    # that builds a tuple builds one per file, and this runs while the listing
    # it is sorting is still held. Same reason islice rather than placed[1:],
    # which would be a second list of every file.
    placed = sorted((e for e in entries if e.size > 0),
                    key=operator.attrgetter("offset"))
    for a, b in zip(placed, itertools.islice(placed, 1, None)):
        if a.offset + a.size > b.offset:
            raise RomfsCorrupt("%r runs into %r in the file partition"
                               % (a.path, b.path))

    for e in entries:
        path, size, off = e.path, e.size, e.offset
        if off < 0 or size < 0:
            raise RomfsCorrupt("%r is %d bytes at offset %d" % (path, size, off))

        # A game splits a path on "/" and stops at NUL, so either inside a name
        # makes the entry unreachable under the name the listing shows.
        name = path.rsplit("/", 1)[-1]
        if not name or "\x00" in name:
            raise RomfsCorrupt("%r is not a name a path can carry" % path)

    # Both tables, entry by entry, as their own layout lays them out. A link
    # that leaves its table, or a name longer than what holds it, is otherwise
    # a struct error or a silently short name -- and a walk only visits the
    # entries a chain happens to reach.
    # parent, sibling and child index the directory table; a directory's first
    # file and a file's sibling index the file table.
    into_dirs, into_files = (dirs, DIR_ENTRY_SIZE), (files, FILE_ENTRY_SIZE)
    for table, name, size, links in (
            (dirs, "directory", DIR_ENTRY_SIZE,
             ((PARENT_LINK_OFFSET, into_dirs),
              (SIBLING_LINK_OFFSET, into_dirs),
              (DIR_CHILD_LINK_OFFSET, into_dirs),
              (DIR_FIRST_FILE_LINK_OFFSET, into_files))),
            (files, "file", FILE_ENTRY_SIZE,
             ((PARENT_LINK_OFFSET, into_dirs),
              (SIBLING_LINK_OFFSET, into_files)))):
        at = 0
        while at < len(table):
            if at % TABLE_ALIGN:
                raise RomfsCorrupt("a %s entry starts at 0x%x, off %d"
                                   % (name, at, TABLE_ALIGN))
            if at + size > len(table):
                raise RomfsCorrupt("a %s entry at 0x%x runs past its table"
                                   % (name, at))

            name_len = struct.unpack_from(LINK_STRUCT, table, at + size - LINK_SIZE)[0]
            if at + size + name_len > len(table):
                raise RomfsCorrupt("the name of the %s entry at 0x%x is %d "
                                   "bytes, past the end of its table"
                                   % (name, at, name_len))

            for field, (target, target_size) in links:
                link = struct.unpack_from(LINK_STRUCT, table, at + field)[0]
                if link != EMPTY and (link % TABLE_ALIGN
                                      or link + target_size > len(target)):
                    raise RomfsCorrupt("the %s entry at 0x%x links to 0x%x, "
                                       "which is not an entry" % (name, at, link))

            at += size + align_up(name_len)

        if at != len(table):
            raise RomfsCorrupt("the %s table ends %d bytes past its last entry"
                               % (name, at - len(table)))

    # Two entries for one path: the listing shows both and a lookup returns
    # whichever the chain reaches first, so which one a game gets is not
    # decided by the romfs at all.
    seen = set()
    for e in entries:
        path = e.path
        if path in seen:
            raise RomfsCorrupt("%r is in the romfs twice" % path)
        seen.add(path)


def _check_hash_tables(romfs, loaded=None):
    """Every file and directory must be findable by name, the way a parser does.

    A romfs is looked up by hashing (parent entry offset, name) into a bucket
    and following the chain in the entries' hash fields. That structure is
    entirely separate from the links the listing walks, so it needs its own
    check: rebuild what the bucket should be, follow the chain, and require the
    entry to be on it. Returns (files checked, directories checked).

    Then backwards: every entry a bucket chain reaches must be one the tree
    reaches too. An entry in a bucket and in no directory is a file a game can
    open and a walk cannot see, and one shadowing a real path is served
    instead of it.
    """
    header, dirs, files = loaded if loaded is not None else _tables(romfs)
    dir_hash = romfs.read(header.dir_hash_offset, header.dir_hash_size)
    file_hash = romfs.read(header.file_hash_offset, header.file_hash_size)
    num_dir_buckets = len(dir_hash) // LINK_SIZE
    num_file_buckets = len(file_hash) // LINK_SIZE

    def find(table, hash_table, num_buckets, entry_off, parent, name, hash_field_off):
        bucket = path_hash(parent, name.encode()) % num_buckets
        cur = struct.unpack_from(LINK_STRUCT, hash_table, bucket * LINK_SIZE)[0]
        seen = 0
        while cur != EMPTY:
            if cur == entry_off:
                return True
            cur = struct.unpack_from(LINK_STRUCT, table, cur + hash_field_off)[0]
            seen += 1
            if seen > num_buckets + len(table):
                raise RomfsCorrupt("hash chain loops at bucket %d" % bucket)
        return False

    n_files = n_dirs = 0
    walked_dirs, walked_files = {ROOT_ENTRY_OFFSET}, set()

    # (dir entry offset, parent entry offset). The root is its own parent here
    # only because it is never looked up -- it has no name to hash.
    stack = [(ROOT_ENTRY_OFFSET, ROOT_ENTRY_OFFSET)]
    while stack:
        dir_off, parent_off = stack.pop()
        sibling, child, first_file, name = _dir_entry(dirs, dir_off)
        if dir_off != ROOT_ENTRY_OFFSET:
            if not find(dirs, dir_hash, num_dir_buckets,
                        dir_off, parent_off, name, DIR_HASH_LINK_OFFSET):
                raise RomfsCorrupt("directory %r not reachable through its hash bucket" % name)
            n_dirs += 1

        f = first_file
        while f != EMPTY:
            f_sibling, _offset, _size, f_name = _file_entry(files, f)
            if not find(files, file_hash, num_file_buckets, f, dir_off, f_name,
                        FILE_HASH_LINK_OFFSET):
                raise RomfsCorrupt("file %r not reachable through its hash bucket" % f_name)
            walked_files.add(f)
            n_files += 1
            f = f_sibling

        c = child
        while c != EMPTY:
            c_sibling, _c_child, _c_file, _c_name = _dir_entry(dirs, c)

            # Visited rather than counted: a child pointing back at an ancestor
            # is a chain of finite length walked forever.
            if c in walked_dirs:
                raise RomfsCorrupt("the directory tree reaches 0x%x twice" % c)
            walked_dirs.add(c)

            stack.append((c, dir_off))
            c = c_sibling

    for table, hash_table, buckets, hash_off, walked, what in (
            (dirs, dir_hash, num_dir_buckets, DIR_HASH_LINK_OFFSET,
             walked_dirs, "directory"),
            (files, file_hash, num_file_buckets, FILE_HASH_LINK_OFFSET,
             walked_files, "file")):
        for b in range(buckets):
            cur, steps = struct.unpack_from(LINK_STRUCT, hash_table, b * LINK_SIZE)[0], 0
            while cur != EMPTY:
                if cur not in walked:
                    raise RomfsCorrupt(
                        "a %s entry at 0x%x is in bucket %d and in no directory"
                        % (what, cur, b))

                cur = struct.unpack_from(LINK_STRUCT, table, cur + hash_off)[0]
                steps += 1
                if steps > len(table):
                    raise RomfsCorrupt("%s bucket %d loops" % (what, b))

    return n_files, n_dirs


def _same_size_pairs(entries, limit):
    """Adjacent same-size entries, up to limit of them.

    Two files can swap their runs in the partition and each still read as one
    contiguous run of the right length -- only having both in the sample
    catches it, and a stride that takes one in fifteen hundred never does.
    """
    by_size = sorted((e for e in entries if e.size > 0), key=lambda e: e.size)
    out, seen = [], set()
    for a, b in zip(by_size, by_size[1:]):
        if a.size == b.size and a.path not in seen and b.path not in seen:
            out += [a, b]
            seen.update((a.path, b.path))
            if len(out) >= limit:
                break
    return out


def _check_contents(romfs, entries, partition_ofs, mod_paths=(), sample=200,
                    deep=16, deep_bytes=1 << 20):
    """Read a sample of files back and check the bytes belong to that file.

    Tables can describe the right files at the right offsets and still be
    wired to the wrong data. The generated game is made distinguishable --
    base data states its own storage offset in every 8-byte word, a mod file
    reads as a pattern from its path -- and this asks the merged image which
    it gets.

    A mod file is checked exactly. A base file cannot be, the offset the build
    should have chosen not being knowable here, so what is checked is that its
    bytes are one contiguous run of the right length and that no two files
    claim the same run -- what a mis-compacted source breaks.

    Three departures from a plain stride:

      * every file the mod delivers is sampled, a stride reaching one only by
        luck -- about 0.07% per file at a full base. The packed delivery has no
        other proof, its files being the game's own paths with only the tag in
        the bytes to say the .bin served them. A delivered path the listing
        does not hold at all is refused here as well;
      * `deep` of the picks are read whole rather than cut at 4 KB, and they
        are the biggest in the sample, that being where a mis-compacted run
        corrupts furthest in. Bytes read then grow with the game rather than
        with the file count;
      * same-size base files are sampled in pairs, two of them swapping runs
        being catchable only with both in the sample.
    """
    step = max(1, len(entries) // sample)
    picked = entries[::step]

    # One pass and a set, rather than a path-keyed dict of every entry: at a
    # full base that dict is the largest thing this walk would hold.
    delivered = set(mod_paths)
    listed = [e for e in entries if e.path in delivered]
    missing = delivered - {e.path for e in listed}
    if missing:
        raise RomfsCorrupt(
            "the mod delivers %d file(s) the merged romfs does not list, "
            "such as %r" % (len(missing), sorted(missing)[0]))

    # Strided like the rest: every pick is read back through the emulated read
    # path, so a mod of thousands would cost what the stride exists to bound.
    from_mod = listed[::max(1, len(listed) // sample)]

    # Same-size neighbours, so a swap between two of them collides below.
    # Sorting by size puts candidates adjacent; a handful of pairs is enough,
    # since the point is to make the hole reachable at all rather than to
    # cover every file.
    pairs = _same_size_pairs(entries, 2 * deep)

    # By path, because the stride may already have chosen one of a pair, or one
    # of the mod's: a file sampled twice claims its own run twice and trips the
    # duplicate rule below against itself.
    picked = list({e.path: e
                   for e in list(picked) + pairs + from_mod}.values())

    # The biggest picks get read in full, so a long file is not judged by its
    # first 4 KB. Everything else stays cheap.
    deep_paths = {e.path for e in sorted(picked, key=lambda e: -e.size)[:deep]}

    claimed = []
    checked_loose = checked_bin = checked_base = 0
    from_mod_paths = {e.path for e in from_mod}

    for e in picked:
        path, size, offset = e.path, e.size, e.offset
        if size <= 0:
            continue
        want = min(size, deep_bytes if path in deep_paths else 4096)

        # A file entry offset is relative to the file partition, not to the
        # image: read at offset 0 and the romfs header itself comes back.
        got = romfs.read(partition_ofs + offset, want)

        expected = romfs.mod_bytes(path, want)
        if expected is not None:
            if got != expected:
                raise RomfsCorrupt("mod file %r reads back as something else" % path)
            checked_loose += 1
            continue

        # Not loose, so it came from the game or from a packed romfs.bin, and
        # the bytes say which: every word states its own offset, and the .bin
        # adds BIN_TAG. Without that a packed mod counts as base data, and the
        # run reports that no mod file was checked at all.

        # A file too short to hold one word cannot state its offset. Real romfs
        # files do get this small, and unpacking a word from one raises out of
        # struct rather than reporting.
        if want < 8:
            checked_base += 1
            continue

        # Read as whole words from byte zero, which only says anything if the
        # file starts on one. FILE_DATA_ALIGN is what makes that true, and it is
        # asked rather than assumed: a partition or a placement that stopped
        # honouring it would still fail below, but as a file that is not one
        # contiguous run -- the symptom, several inferences from the cause.
        if (partition_ofs + offset) % 8:
            raise RomfsCorrupt(
                "file %r starts at %#x, off a word: file data must sit on "
                "FILE_DATA_ALIGN (%#x) within a partition that does too"
                % (path, partition_ofs + offset, FILE_DATA_ALIGN))
        head = struct.unpack_from("<Q", got, 0)[0]
        for i in range(0, (want // 8) * 8, 8):
            if struct.unpack_from("<Q", got, i)[0] != head + i:
                raise RomfsCorrupt("file %r is not one contiguous run" % path)
        claimed.append((head, min(size, want), path))
        if romfs.from_packed(head):
            checked_bin += 1
            continue

        # Data from the game, at a path the mod delivers: the merge did not
        # take that file. The loose half of this is caught above, the file
        # being on the SD to compare against; a packed one has nothing to
        # compare against and would otherwise be counted as base data and
        # pass, one file quieter than it should be.
        if path in from_mod_paths:
            raise RomfsCorrupt(
                "%r is delivered by the mod and reads back as the game's own "
                "data: the merge did not take it" % path)
        checked_base += 1

    # Nothing is checked about how many came from each delivery: every path
    # the mod delivers has been read back and shown to come from the mod, one
    # by one, which is the question the counts were standing in for.

    claimed.sort()
    for (a_off, _a_len, a_path), (b_off, _b_len, b_path) in zip(claimed, claimed[1:]):
        if a_off == b_off:
            raise RomfsCorrupt("%r and %r read from the same place in the game" % (a_path, b_path))

    return checked_loose, checked_bin, checked_base


def _digest(entries):
    """One line per file, hashed. Order-independent: the list is sorted.

    sha256 is not here for its cryptography -- nothing is defending against a
    forged romfs -- it is here because nothing cheaper is worth the collision
    headroom it gives up.
    """
    h = hashlib.sha256()
    for e in entries:
        h.update(("%s\t%d\t%d\n" % (e.path, e.size, e.offset)).encode("utf-8"))
    return h.hexdigest()


def _content_digest(entries):
    """The same, without the offset -- what the filesystem IS, not how it is laid out.

    Two builds can present an identical filesystem from different partitions,
    which the digest above cannot say: hashing the offset, it differs on every
    entry for a build that inherits a base layout rather than recompacting one.
    This is the part that must match stock.

    It says nothing about whether an offset points at the right bytes.
    """
    h = hashlib.sha256()
    for e in entries:
        h.update(("%s\t%d\n" % (e.path, e.size)).encode("utf-8"))
    return h.hexdigest()


class RomfsReader:
    """The romfs a build produced, read once and asked as often as wanted.

    verify and describe both need the tables loaded and walked, and a caller
    asking one asks the other.

    The saving is smaller than it looks, since verify spends its time reading
    samples back through the emulated read path rather than parsing tables.
    The reason to hold them is that two questions about one romfs should not
    each have to be told which romfs; the time saved is a bonus.

    Loaded on the first ask rather than in the constructor: a caller that only
    wants a digest should not pay for the hash tables it will not look at.
    """

    __slots__ = ("_romfs", "_loaded", "_entries")

    def __init__(self, romfs):
        self._romfs = romfs
        self._loaded = None
        self._entries = None

    def _tables_now(self):
        if self._loaded is None:
            self._loaded = _tables(self._romfs)
        return self._loaded

    def _listing_now(self):
        if self._entries is None:
            self._entries = _listing(self._romfs, self._tables_now())
        return self._entries

    def verify(self):
        """Both structural checks, raising on the first that does not hold.

        Returns a CheckResult, so a caller can see that a check had
        something to check -- a sample that read back neither delivery of the
        mod exercises nothing, and says so by counting zero.
        """
        loaded, entries = self._tables_now(), self._listing_now()
        _check_structure(loaded, entries)
        n_files, n_dirs = _check_hash_tables(self._romfs, loaded)
        n_loose, n_bin, n_base = _check_contents(
            self._romfs, entries, loaded[0].file_data_offset,
            self._romfs.mod_paths())
        return CheckResult(n_files, n_dirs, n_loose, n_bin, n_base)

    def describe(self):
        """A Description of the romfs: what it holds, and two digests of it.

        A description rather than a verdict: no rules about what a good romfs
        looks like, and no judgement on what it finds. verify is where the
        rules are, kept a separate question.

        It can still fail, which is not the same thing: the listing is walked
        out of the entry links, and a chain returning to itself has no end to
        walk to. So it raises RomfsCorrupt on a romfs that cannot be read at
        all, never on one that is merely wrong.
        """
        entries = self._listing_now()
        return Description(len(entries), sum(e.size for e in entries),
                           _digest(entries), _content_digest(entries))
