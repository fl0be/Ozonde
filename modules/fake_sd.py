#!/usr/bin/env python3
"""The SD it reads, and its mirror on disk.

In memory, keyed by the exact strings FormatAtmosphereSdPath() and its romfs
twin produce ("/atmosphere/contents/<16-hex>/romfs/..."). That formatting
runs for real inside the emulator, so nothing here reimplements the
convention: it records whatever path arrives at a hooked libnx call.
"""
import os


# What ams is set up with, as opposed to what a run leaves on the SD. A
# console reads its settings out of this file, and emptying the SD does not
# touch it.
SETTINGS_INI = "/atmosphere/config/system_settings.ini"


class Node:
    """A file node carries its size separately from its data, so a tree can be
    mirrored from a real romfs (hundreds of thousands of files, tens of GiB)
    by size alone. A node holding no data invents what it reads, so
    a file costs its size rather than its bytes however much of it is read."""

    __slots__ = ("is_dir", "children", "data", "size", "provider")

    def __init__(self, is_dir, size=0):
        self.is_dir = is_dir
        self.children = {} if is_dir else None
        self.data = None if is_dir else bytearray()
        self.size = size

        # Set for a file whose tail is produced rather than stored.
        self.provider = None

    def bytes_past(self, path, offset, size):
        """What this file reads as past the bytes it actually holds.

        The node's own question: only it knows whether a provider was set.
        """
        if self.provider is not None:
            return self.provider(offset, size)
        return self._sparse_content(path, offset, size)

    @staticmethod
    def _sparse_content(path, offset, size):
        """What a sparse file on this SD reads as: bytes derived from its path.

        A mod file that reads back as its own path proves the build resolved it
        to the right file, which zeros could never show.
        """
        seed = 0
        for ch in path.encode("utf-8"):
            seed = ((seed * 131) + ch) & 0xFFFFFFFF
        out = bytearray(size)
        for i in range(size):
            out[i] = (seed + offset + i * 7) & 0xFF
        return bytes(out)


class FakeSD:
    """The SD, and what a previous run may have left on it.

    Given a directory, it is read on the way in and written on the way out.
    Given None, the SD exists only in memory: nothing is read, nothing is
    written, and a run cannot be measured against files somebody else left.
    """

    def __init__(self, mirror_dir=None, fresh=False):
        self._root = Node(is_dir=True)
        self._mirror = mirror_dir

        if mirror_dir is not None:
            if fresh and os.path.isdir(mirror_dir):
                self._empty_but_settings()
            self._load()

        # Paths the *target* created or wrote during the run, as opposed to
        # the tree it was handed. A build being debugged writes its own files
        # to the SD -- a log, a dump, its own metadata -- and those are worth
        # surfacing rather than leaving buried in the node graph. Added to
        # through note_written, since only a hook knows a write happened.
        self._written = set()

        # The base game's romfs image, served through the FsStorage handle that
        # fs would hand over. Held on the host and answered per read, so a
        # full-size game costs its tables rather than its bytes.
        self.base_storage = None

    # ------------------------------------------------------------------------
    # The tree it holds
    # ------------------------------------------------------------------------

    @staticmethod
    def _split(path):
        return [p for p in path.strip("/").split("/") if p]

    def _place(self, path, node):
        """Put a node at path, creating whatever directories it needs.

        The three ways to add a file differ in how the node is built and not
        at all in where it goes.
        """
        parts = self._split(path)
        self.ensure_dir("/".join(parts[:-1])).children[parts[-1]] = node
        return node

    def ensure_dir(self, path):
        node = self._root
        for part in self._split(path):
            node = node.children.setdefault(part, Node(is_dir=True))
        return node

    def put_file(self, path, data):
        f = Node(is_dir=False)
        f.data = bytearray(data)
        f.size = len(f.data)
        return self._place(path, f)

    def put_file_virtual(self, path, head, total_size, provider):
        """A file whose first bytes are real and whose tail is invented.

        A packed romfs.bin mod is served this way: the tables it is read for
        exist, the file partition behind them is produced on demand, so an image
        of any size costs only its tables.
        """
        f = Node(is_dir=False, size=total_size)
        f.data = bytearray(head)
        f.provider = provider
        return self._place(path, f)

    def put_file_sparse(self, path, size):
        """Record a file's existence and size without its contents."""
        return self._place(path, Node(is_dir=False, size=size))

    def find(self, path):
        node = self._root
        for part in self._split(path):
            if not node.is_dir or part not in node.children:
                return None
            node = node.children[part]
        return node

    def remove(self, path):
        parts = self._split(path)
        parent = self.find("/".join(parts[:-1]))
        if parent is not None and parent.is_dir:
            parent.children.pop(parts[-1], None)

    # ------------------------------------------------------------------------
    # What the target wrote
    # ------------------------------------------------------------------------

    def note_written(self, path):
        """Record that the target wrote to a path."""
        self._written.add(path)

    def written_files(self):
        """[(path, size, data)] for what the target wrote, in path order.

        The data is the node's own buffer, not a copy: callers read it, write
        it out or measure it, and none keep it past the call.
        """
        out = []
        for path in sorted(self._written):
            node = self.find(path)
            if node is not None and not node.is_dir:
                out.append((path, node.size, node.data or b""))
        return out

    # ------------------------------------------------------------------------
    # The mirror on disk
    # ------------------------------------------------------------------------

    # The module formats its SD paths with %016lx, so a program id arrives
    # lowercase and is recorded that way. Only the host copy is capitalised,
    # for readability, and it is folded back on the way in.
    @staticmethod
    def host_path(guest_path):
        return "/".join(p.upper() if FakeSD._is_program_id(p) else p
                        for p in guest_path.split("/"))

    @staticmethod
    def _guest_path(host_path):
        return "/".join(p.lower() if FakeSD._is_program_id(p) else p
                        for p in host_path.split("/"))

    @staticmethod
    def _is_program_id(part):
        return len(part) == 16 and all(c in "0123456789abcdefABCDEF" for c in part)

    @staticmethod
    def _is_mod_input(path):
        """Is this a mod, rather than something the build produced?

        A mod is generated fresh every run and must never travel through the
        mirror in either direction: exporting 300k files would be pointless,
        and importing them would replace the tree being measured with an
        older run's.

        Matched by the shape Atmosphere defines -- atmosphere/contents/*/ then
        romfs/ or romfs.bin -- and by position rather than by the name turning
        up anywhere, so a romfs-something written *beside* those is output.
        Any directory passes for the program id: one that fails a 16-hex test
        is still a mod, and letting it through is the failure that matters.
        """
        parts = path.strip("/").split("/")
        return (len(parts) > 3 and parts[0] == "atmosphere"
                and parts[1] == "contents"
                and parts[3] in ("romfs", "romfs.bin"))

    def _empty_but_settings(self):
        """Everything a run left on the SD, keeping what ams is set up with.

        What a run wrote is a result and starts cold; the settings file is a
        premise and survives, as it does on a console -- clearing an SD's
        contents does not unset its settings.
        """
        for dirpath, _dirs, files in os.walk(self._mirror, topdown=False):
            for fn in files:
                host = os.path.join(dirpath, fn)
                rel = os.path.relpath(host, self._mirror).replace(os.sep, "/")
                if "/" + rel != SETTINGS_INI:
                    os.remove(host)
            if dirpath != self._mirror and not os.listdir(dirpath):
                os.rmdir(dirpath)

    def _load(self):
        """Seed the SD with whatever a previous run left there.

        An SD outlives the process that wrote to it. Whether a build looks for
        what it left is its own business, but it must be able to, or that path
        is never exercised. The romfs tree is generated fresh every run and
        never written out, so this restores state rather than supplying a mod.
        """
        for dirpath, _dirs, files in os.walk(self._mirror):
            for fn in files:
                host = os.path.join(dirpath, fn)
                rel = os.path.relpath(host, self._mirror).replace(os.sep, "/")
                guest = self._guest_path("/" + rel)
                if self._is_mod_input(guest):
                    continue
                with open(host, "rb") as f:
                    self.put_file(guest, f.read())

    def flush(self):
        """Write what the target produced into a real directory, so its
        output can be opened and the next run finds it as a console would.

        Done once at the end rather than per fsFileWrite: a file is written in
        many chunks, and re-serialising the whole of it per chunk would cost
        more than the build being measured. Deleted files never appear, which
        is what the SD would look like afterwards.
        """
        written = self.written_files()
        if not written or self._mirror is None:
            return []
        root = os.path.realpath(self._mirror)
        out = []
        for path, _size, data in written:
            if self._is_mod_input(path):
                continue

            # Checked where the write would land, not on the spelling. A
            # component like `a\..\..\b` survives normpath whole on Linux and
            # only becomes a traversal once the backslashes are translated --
            # and realpath rather than abspath, because a symlink under the
            # mirror is a traversal that no amount of normalising reveals.
            dest = os.path.realpath(os.path.join(
                root, self.host_path(path).replace("\\", "/").lstrip("/")))

            if dest != root and not dest.startswith(root + os.sep):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
            out.append(dest)
        return out
