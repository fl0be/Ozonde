#!/usr/bin/env python3
"""Write a romfs image: the format, and nothing about what goes in it.

The writing half of a pair, kept apart from the reading half because the two
sit on opposite sides of the measurement: this writes the romfs the module is
given, that reads the one the module produced. What both must agree on -- the
entry layout, the hash, the bucket count -- is neither one's to own, and lives
in romfs_format.

Minimal: just enough of the format to be a valid input. The table layout, and
serving contents from a function rather than from bytes, is written for this
harness.

The read side only ever follows the directory/file table's
parent/child/sibling/file linked-list fields; the hash tables exist for a real
game's own RomFs parser to do name lookups. Both are emitted for real anyway: a
build sizes its own output from what it reads, so a zero-size hash table
shrinks an estimate that should have been large, and this once handed out
exactly that false result.

Contents are never materialized. A file is added by name and size alone, and
the partition behind the tables is produced on demand by whatever provider the
caller supplies.
"""
import array
import struct

from romfs_format import (DIR_ENTRY_SIZE, DIR_ENTRY_STRUCT,
                          DIR_HASH_LINK_OFFSET, EMPTY, FILE_DATA_ALIGN,
                          FILE_ENTRY_SIZE, FILE_ENTRY_STRUCT,
                          FILE_HASH_LINK_OFFSET, FILE_PARTITION_OFFSET,
                          HEADER_SIZE, HEADER_STRUCT, Header,
                          LINK_STRUCT, LINK_SIZE, SIBLING_LINK_OFFSET,
                          align_up, hash_table_size, path_hash)


class BadFakeRomfs(Exception):
    """What was handed here is not a romfs this can pack.

    A name spoken for twice is the whole of it today -- a directory holds one
    of each name, whether a file or a directory wears it -- and the class is
    wider than that on purpose: anything the format cannot express belongs
    here rather than in a second exception.

    Raised rather than asserted: an assert is stripped by -O, and this is the
    one fault that would leave an image quietly holding fewer files than the
    run believes it measured. A caller that generates paths can catch it;
    nothing here can do anything about it.
    """


class _Dir:
    __slots__ = ("name", "dirs", "files", "index", "first_file")

    def __init__(self, name):
        self.name = name
        self.dirs = {}       # name -> _Dir
        self.files = {}      # name -> size
        self.index = -1      # assigned in pack, in BFS order
        self.first_file = 0  # where this dir's files start in the flat arrays


class PackedFakeRomfs:
    """A fake romfs that has been packed: real tables over invented data.

    Named for what it holds rather than for what it is. The image itself is
    genuine -- the header, the entry tables and the hash buckets are the bytes
    switch-tools would write. What is fake is everything the format is
    describing: the paths, the sizes and the bytes behind them are generated,
    and no such game exists.

    So this is not a fake packed romfs. It is a packed fake romfs, and the
    distinction is why the tables are worth walking: an image that quietly
    stopped being readable would be one no tool would ever produce.

    Header, then the file partition at 0x200, then the tables past the end of it
    -- the order a real romfs uses. Only the header and the tables are ever
    materialised; everything between them is produced on demand, so an image of
    any size costs its tables.
    """

    def __init__(self, header, tables, tables_ofs, size, content):
        # The header is a copy and the size a number; the tables, where they
        # sit and what serves the partition are this image's own, since an
        # image whose tables a caller can edit is not the image it reports.
        self.header = bytes(header)
        self.size = size
        self._tables = tables
        self._tables_ofs = tables_ofs
        self._content = content

    def read(self, offset, size):
        out = bytearray()
        while size > 0 and offset < self.size:
            if offset < len(self.header):
                n = min(size, len(self.header) - offset)
                out += self.header[offset:offset + n]
            elif offset >= self._tables_ofs:
                i = offset - self._tables_ofs
                n = min(size, len(self._tables) - i)
                if n <= 0:
                    break
                out += self._tables[i:i + n]
            else:
                n = min(size, self._tables_ofs - offset)
                out += self._content(offset, n)
            offset += n
            size -= n
        return bytes(out)


class RomfsBuilder:
    """The tree a caller adds files to, and the image it packs into."""

    def __init__(self):
        self._root = _Dir("")

    def add_file_sparse(self, path, size):
        """Record a file's name and size without its contents. Enough to emit
        correct tables -- which is all the read path ever looks at."""
        parts = [p for p in path.strip("/").split("/") if p]
        node = self._root
        for part in parts[:-1]:
            node = node.dirs.setdefault(part, _Dir(part))

        # A name lands once in a directory. The dicts take a second silently,
        # so the image would hold fewer files than it was handed and every
        # figure after it would describe a tree nobody asked for.
        if parts[-1] in node.files:
            raise BadFakeRomfs("added twice: %s" % path)
        if parts[-1] in node.dirs:
            raise BadFakeRomfs("a directory of that name is here: %s" % path)
        node.files[parts[-1]] = size

    def pack(self, content=None):
        """The image these files make: header, tables, and a virtual
        partition behind them.

        The header and the tables are real bytes; the partition between them is
        never materialised, so a 17 GiB romfs costs what its tables cost.
        `content` is what serves that partition, and only a caller that reads
        past the tables needs to pass one -- an image packed to have its tables
        walked never reaches there.

        All four come back together, since a caller needs the tables, where
        they landed and how far the image runs to make sense of any of them.

        Per-file data lives in flat arrays indexed by a file number rather than
        in dictionaries keyed by (directory, name), which cuts the memory the
        build itself needs to a fraction: a dict with a tuple key per file costs
        many times the image it describes, and the gap widens with scale. It
        also retires id() as a key, which was only ever safe because every
        directory happened to stay alive.
        """
        # ---- directories: BFS from the root, each given an index and offset --
        dirs = [self._root]
        dir_offset = array.array("q")
        cursor = i = 0
        while i < len(dirs):
            d = dirs[i]
            d.index = i
            dir_offset.append(cursor)
            cursor += DIR_ENTRY_SIZE + align_up(len(d.name.encode()))
            dirs.extend(d.dirs.values())
            i += 1
        dir_table_size = cursor

        # ---- files: one flat record each ----
        f_name = []                 # the strings the tree already holds
        f_off = array.array("q")    # offset within the file table
        f_size = array.array("q")
        for d in dirs:
            d.first_file = len(f_name)
            for name, fsize in d.files.items():
                f_name.append(name)
                f_off.append(0)
                f_size.append(fsize)

        # Full paths, wanted by the table layout as well as by the partition.
        paths = [None] * len(f_name)

        def walk(d, prefix):
            base = d.first_file
            for k, name in enumerate(d.files):
                paths[base + k] = prefix + "/" + name
            for name, child in d.dirs.items():
                walk(child, prefix + "/" + name)

        walk(self._root, "")

        # Path order, which both of the loops below lay their subject out in.
        # One sort rather than two: the entries and the contents were ordered
        # separately, by the same key, under two names -- and two names for one
        # order are two orders as soon as somebody edits one of them. The cost
        # is not the argument: a sort of 305k paths is 0.066s of a 2.1s pack.
        #
        # Sorting the strings is equivalent to sorting their UTF-8, since byte
        # order and code point order agree in UTF-8.
        in_path_order = sorted(range(len(f_name)), key=paths.__getitem__)

        # Where each entry lands. The format does not say -- every link is an
        # explicit offset -- so any order makes a valid image; these land in
        # the order a walk meets them, which is path order.
        cursor = 0
        for j in in_path_order:
            f_off[j] = cursor
            cursor += FILE_ENTRY_SIZE + align_up(len(f_name[j].encode()))
        file_table_size = cursor

        # Contents back-to-back on FILE_DATA_ALIGN, in full-path order. A real
        # romfs is laid out that way, and a reader compacting runs of files
        # from one source depends on it: offsets that climb in directory-walk
        # order instead make an image it is right to reject.
        content_off = array.array("q", bytes(8 * len(f_name)))
        cursor = 0
        for j in in_path_order:
            cursor = (cursor + FILE_DATA_ALIGN - 1) & ~(FILE_DATA_ALIGN - 1)
            content_off[j] = cursor
            cursor += f_size[j]
        partition_size = cursor

        # Dropped before the tables below are allocated: at half a million
        # entries these strings are the largest thing alive, and nothing after
        # this needs them. Rebound rather than deleted, so the closure that
        # filled it still reads as bound.
        paths = None

        # ---- the tables, in their own buffer; offsets within it ----
        header_size = HEADER_SIZE
        num_dir_buckets = hash_table_size(len(dirs))
        num_file_buckets = hash_table_size(len(f_name))
        dir_hash_ofs = 0
        dir_hash_size = LINK_SIZE * num_dir_buckets
        dir_table_ofs = dir_hash_ofs + dir_hash_size
        file_hash_ofs = dir_table_ofs + dir_table_size
        file_hash_size = LINK_SIZE * num_file_buckets
        file_table_ofs = file_hash_ofs + file_hash_size
        tables_size = file_table_ofs + file_table_size

        # The partition sits at 0x200 and the tables follow it, so the header
        # records where they really landed.
        file_partition_ofs = FILE_PARTITION_OFFSET
        tables_ofs = align_up(file_partition_ofs + partition_size)

        out = bytearray(tables_size)
        header = bytearray(file_partition_ofs)
        struct.pack_into(HEADER_STRUCT, header, 0, *Header(
            size=header_size,
            dir_hash_offset=tables_ofs + dir_hash_ofs,
            dir_hash_size=dir_hash_size,
            dir_table_offset=tables_ofs + dir_table_ofs,
            dir_table_size=dir_table_size,
            file_hash_offset=tables_ofs + file_hash_ofs,
            file_hash_size=file_hash_size,
            file_table_offset=tables_ofs + file_table_ofs,
            file_table_size=file_table_size,
            file_data_offset=file_partition_ofs))

        # ---- directory table ----
        for d in dirs:
            off = dir_table_ofs + dir_offset[d.index]
            child_names = sorted(d.dirs)
            child = dir_offset[d.dirs[child_names[0]].index] if child_names else EMPTY
            if d.files:
                first = min(range(d.first_file, d.first_file + len(d.files)),
                            key=f_name.__getitem__)
                file_head = f_off[first]
            else:
                file_head = EMPTY
            name_b = d.name.encode()
            struct.pack_into(DIR_ENTRY_STRUCT, out, off,
                             EMPTY, EMPTY, child, file_head, 0, len(name_b))
            out[off + DIR_ENTRY_SIZE: off + DIR_ENTRY_SIZE + len(name_b)] = name_b

        # Sibling chains among a directory's children, in name order, plus the
        # parent field -- unused by the read path, but correct to set.
        for d in dirs:
            child_names = sorted(d.dirs)
            for a, b in zip(child_names, child_names[1:]):
                off = dir_table_ofs + dir_offset[d.dirs[a].index]
                struct.pack_into(LINK_STRUCT, out, off + SIBLING_LINK_OFFSET,
                                 dir_offset[d.dirs[b].index])
            for child in d.dirs.values():
                struct.pack_into(LINK_STRUCT, out,
                                 dir_table_ofs + dir_offset[child.index],
                                 dir_offset[d.index])

        # ---- file table, a directory at a time so siblings need no map ----
        for d in dirs:
            n = len(d.files)
            if not n:
                continue
            in_name_order = sorted(range(d.first_file, d.first_file + n),
                                   key=f_name.__getitem__)
            for pos, j in enumerate(in_name_order):
                sib = f_off[in_name_order[pos + 1]] if pos + 1 < n else EMPTY
                off = file_table_ofs + f_off[j]
                name_b = f_name[j].encode()
                struct.pack_into(FILE_ENTRY_STRUCT, out, off, dir_offset[d.index], sib,
                                 content_off[j], f_size[j], 0, len(name_b))
                out[off + FILE_ENTRY_SIZE: off + FILE_ENTRY_SIZE + len(name_b)] = name_b

        # ---- hash tables ----
        # A parser finds an entry by hashing (parent entry offset, name) into a
        # bucket and following the chain in the entries' own hash fields -- a
        # separate structure from the links walked above, so built separately.
        #
        # Inserted at the head, chain order being unobservable, and walked from
        # the parent, where both offsets are in hand. The root is in no bucket:
        # a parser reaches it by being at 0.
        dir_buckets = array.array("I", [EMPTY]) * num_dir_buckets
        for d in dirs:
            parent_off = dir_offset[d.index]
            for name, child in d.dirs.items():
                entry_off = dir_offset[child.index]
                b = path_hash(parent_off, name.encode()) % num_dir_buckets
                struct.pack_into(LINK_STRUCT, out,
                                 dir_table_ofs + entry_off + DIR_HASH_LINK_OFFSET,
                                 dir_buckets[b])
                dir_buckets[b] = entry_off
        out[dir_hash_ofs: dir_hash_ofs + dir_hash_size] = dir_buckets.tobytes()

        file_buckets = array.array("I", [EMPTY]) * num_file_buckets
        for d in dirs:
            parent_off = dir_offset[d.index]
            for k in range(len(d.files)):
                j = d.first_file + k
                entry_off = f_off[j]
                b = path_hash(parent_off, f_name[j].encode()) % num_file_buckets
                struct.pack_into(LINK_STRUCT, out,
                                 file_table_ofs + entry_off + FILE_HASH_LINK_OFFSET,
                                 file_buckets[b])
                file_buckets[b] = entry_off
        out[file_hash_ofs: file_hash_ofs + file_hash_size] = file_buckets.tobytes()

        return PackedFakeRomfs(header, out, tables_ofs,
                               tables_ofs + tables_size, content)
