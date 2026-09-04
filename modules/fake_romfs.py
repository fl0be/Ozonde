#!/usr/bin/env python3
"""The romfs a run is measuring, and the mods delivered over it.

Three things a launch is handed: the game itself, a mod that overrides some of
it and adds more to it, and the pair of them read as one tree. What a file is
called and what is in it comes from romfs_model; what these add is which romfs
an index belongs to, so a walk of one cannot be measured against another.
"""
import romfs_model


class FakeRomfs:
    """The romfs a run is measuring: how many files, and which release.

    Those two are the whole of what tells one generated romfs from another.
    Everything under here is a pure function of an index, so two instances
    built the same way describe the same bytes -- what the instance adds is
    which romfs an index belongs to, and one outside it is caught rather than
    answered.
    """

    __slots__ = ("_count", "_version")

    def __init__(self, count, version=0):
        if count < 1:
            raise ValueError("a romfs holds at least one file, not %r" % (count,))
        self._count = count

        # Which release of the title this is, as ncm packs a version.
        # Zero is the game as it shipped, with no update over it.
        self._version = version

    # Read-only, both of them: one instance is shared by the modded game and by
    # each of its deliveries, so an assignment here would change what two mods
    # deliver and say nothing. A different tree is a different FakeRomfs.
    @property
    def count(self):
        """How many files the game holds."""
        return self._count

    @property
    def version(self):
        """Which release it is, as ncm packs a version."""
        return self._version

    def __repr__(self):
        return "FakeRomfs(%d, version=%#x)" % (self.count, self.version)

    def _entry(self, i):
        if not 0 <= i < self.count:
            raise IndexError("entry %d is not in a romfs of %d files"
                             % (i, self.count))
        return i

    def path(self, i):
        """Where entry i lives. See the module's own notes on the shape."""
        return romfs_model.path(self._entry(i))

    def size(self, i):
        """What entry i weighs, grown where this release rewrote it."""
        return romfs_model.size(self._entry(i), self.version)

    def content_at(self, offset, size):
        """The game's bytes at an offset, invented on demand."""
        return romfs_model.content_at(offset, size)


class FakeMod:
    """A mod of a game, and the files it delivers.

    One mod however it arrives. A loose tree under romfs/ and a romfs.bin
    carrying the same overrides and additions must merge to the same romfs,
    which is what comparing the two deliveries exists to show -- so they are
    one description here, and differ only in what consumes it.

    An override is a path the game already holds at a size it does not; an
    addition is neither, and is not bounded by the game's count, a mod's own
    files being by definition ones the game lacks.

    Where in the game's sequence it starts is the caller's: past another mod's
    files they are disjoint and their counts add up, at the same place every
    path is a shadowed one.
    """

    __slots__ = ("game", "overridden", "added", "first_override", "first_added")

    def __init__(self, game, overridden, added, first_override=0, first_added=0):
        if min(overridden, added, first_override, first_added) < 0:
            raise ValueError("a mod cannot hold a negative number of files")
        last = first_override + overridden
        if last > game.count:
            raise ValueError(
                "overriding %d files from %d wants a game of %d, not %d"
                % (overridden, first_override, last, game.count))
        self.game = game
        self.overridden = overridden
        self.added = added
        self.first_override = first_override
        self.first_added = first_added

    def __len__(self):
        return self.overridden + self.added

    def __repr__(self):
        return ("FakeMod(%r, overridden=%d, added=%d, first_override=%d, "
                "first_added=%d)"
                % (self.game, self.overridden, self.added,
                   self.first_override, self.first_added))

    def content_at(self, offset, size):
        """This mod's bytes, when it arrives packed.

        Tagged apart from the game's, because both describe the same paths
        at the same sizes: a packed mod that was never opened reads back
        exactly like one that was, and only the tag says which happened.
        """
        return romfs_model.bin_content_at(offset, size)

    def files(self, root=""):
        """(path, size) for each file the mod delivers, under root.

        Overrides first and additions after. Root is where the delivery puts
        them: the SD's romfs directory for a loose tree, nothing for an image,
        whose paths are already relative to itself.
        """
        game = self.game
        for i in range(self.first_override,
                       self.first_override + self.overridden):
            yield "%s/%s" % (root, game.path(i)), romfs_model.mod_size(i)
        for j in range(self.first_added, self.first_added + self.added):
            yield romfs_model.added_path(root, j), romfs_model.added_size(j)


class FakeModdedGame:
    """A game's romfs and the two deliveries a mod can arrive by.

    Which release the game is at arrives as a version rather than a flag:
    nothing here has to know what ncm is, and a caller wanting a different
    tree asks for a different version.

    The two deliveries draw from one sequence and must not share a stretch of
    it -- a loose path shadows the packed copy of itself, so an overlap stops
    the counts adding up. Which stretch each gets is settled here, being a
    fact about the pair rather than about writing them out.

    It also holds the rule neither delivery can check alone: between them they
    cannot override more files than the game has, so a combination that could
    not exist cannot be built.
    """

    __slots__ = ("game", "loose", "packed")

    def __init__(self, base, loose=(0, 0), packed=(0, 0), version=0):
        overridden = loose[0] + packed[0]
        if overridden > base:
            raise ValueError(
                "the deliveries override %d files between them, more than the "
                "game holds (%d)" % (overridden, base))
        if sum(loose) + sum(packed) == 0:
            raise ValueError("neither delivery carries a file, so there is no "
                             "mod to measure")
        self.game = FakeRomfs(base, version)
        self.loose = FakeMod(self.game, loose[0], loose[1])

        # Past the loose tree's files, which is the whole of what keeps the two
        # disjoint and their counts adding up.
        self.packed = FakeMod(self.game, packed[0], packed[1],
                              loose[0], loose[1])

    def from_packed(self, word):
        """Was that word served by the packed mod rather than by the game?

        Both sources state their own offset in every word, so bytes read back
        say which one served them. Asked of the pair, since telling them apart
        is a fact about the pair -- and answered by what invents the bytes.
        """
        return romfs_model.from_packed(word)

    def __repr__(self):
        return ("FakeModdedGame(%d, loose=(%d, %d), packed=(%d, %d), "
                "version=%#x)"
                % (self.game.count,
                   self.loose.overridden, self.loose.added,
                   self.packed.overridden, self.packed.added,
                   self.game.version))
