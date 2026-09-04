#!/usr/bin/env python3
"""What ncm holds about installed titles, which is no property of the build.

Meta is ncm's own word: a launch asks for a ContentMetaKey, by ContentMetaType,
out of a ContentMetaDatabase. The build never reads one directly -- it asks
ncm, and ncm is answered out of what is here.
"""
import struct

# An update is a second title, at the game's id plus this.
PATCH_ID_OFFSET = 0x800

# A version is a release level times this, so a caller says which
# release and the number it is written as follows.
UPDATE_STEP = 0x10000


def display_version(version, absent="not installed"):
    """The words a console would show for a title at that version.

    Not something ncm answers: a console reads what a person sees out of the
    title's control.nacp, and there is no such file here. So a rule stands in
    for one -- a published game is 1.0.0 and each release above it raises the
    major, so the first update shows as 2.0.0.

    Here because it is the inverse of what installing a title does: a version
    is a release level times the step, and this is that read back.
    """
    if version is None:
        return absent
    return "%d.0.0" % ((version // UPDATE_STEP) + 1)


class FakeContentMetaKey:
    """nn::ncm::ContentMetaKey -- a title, its version, and what kind it is.

    The whole of what a launch gets back; the content meta behind it is never
    read here. https://switchbrew.org/wiki/NCM_services
    """

    # 0x0 program id, 0x8 version, 0xC type, 0xD install type, 0xE padding.
    STRUCT = "<QIBBH"
    SIZE = struct.calcsize(STRUCT)

    # nn::ncm::ContentMetaType. Unknown is what a caller passes to mean any.
    TYPE_UNKNOWN = 0x0
    APP = 0x80
    PATCH = 0x81
    ADD_ON_CONTENT = 0x82

    # nn::ncm::InvalidApplicationId, which is what a caller passes to ask about
    # every title rather than one -- an id nothing is installed at.
    INVALID_APPLICATION_ID = 0

    # nn::ncm::ContentInstallType. Unknown is what a caller passes to mean any.
    INSTALL_TYPE_FULL = 0
    INSTALL_TYPE_FRAGMENT_ONLY = 1
    INSTALL_TYPE_UNKNOWN = 7

    __slots__ = ("program_id", "version", "type", "install_type")

    def __init__(self, program_id, version, type,
                 install_type=INSTALL_TYPE_FULL):
        for name, value in (("program_id", program_id), ("version", version),
                            ("type", type), ("install_type", install_type)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        """A key is a value, and hashes like one, so it does not change."""
        raise AttributeError("a content meta key does not change: %s" % name)

    def __repr__(self):
        return "FakeContentMetaKey(%#x, version=%d, type=%#x)" % (
            self.program_id, self.version, self.type)

    def __eq__(self, other):
        """All four fields, as ncm ties a key."""
        return (isinstance(other, FakeContentMetaKey)
                and self.program_id == other.program_id
                and self.version == other.version
                and self.type == other.type
                and self.install_type == other.install_type)

    def __ne__(self, other):
        return not self == other

    def __hash__(self):
        return hash((self.program_id, self.version, self.type,
                     self.install_type))

    def packed(self):
        """The bytes ncm writes into the caller's buffer."""
        return struct.pack(self.STRUCT, self.program_id, self.version,
                           self.type, self.install_type, 0)


class FakeMetaExtendedHeader:
    """What a title records about itself, past the key that names it.

    A console keeps a header, then this, then the content. Only this is here,
    and only the two fields anything asks for: an update's id, kept by a game,
    and an owning application's id, kept by an update and by add-on content.

    Which fields a kind may hold is from_key's to say, so one is made there.
    A kind holding neither holds an empty one -- no owner is the invalid
    application id, no update of its own is zero.
    """

    __slots__ = ("application_id", "patch_id")

    def __init__(self, application_id=FakeContentMetaKey.INVALID_APPLICATION_ID,
                 patch_id=0):
        self.application_id = application_id
        self.patch_id = patch_id

    def __repr__(self):
        return "FakeMetaExtendedHeader(application_id=%#x, patch_id=%#x)" % (
            self.application_id, self.patch_id)

    @staticmethod
    def from_key(key,
                 application_id=FakeContentMetaKey.INVALID_APPLICATION_ID):
        """What installing that title would have written.

        A game belongs to itself and keeps where its update will go; an update
        belongs to the game below its offset and keeps no update of its own;
        anything else belongs to whoever it was said to, which is what add-on
        content is. A caller naming nobody leaves the invalid application id.

        ncm answers a game's own id out of the key instead. Kept here anyway,
        so whose a title is reads the same way for every kind.
        """
        if key.type == FakeContentMetaKey.APP:
            return FakeMetaExtendedHeader(
                application_id=key.program_id,
                patch_id=key.program_id + PATCH_ID_OFFSET)
        if key.type == FakeContentMetaKey.PATCH:
            return FakeMetaExtendedHeader(
                application_id=key.program_id - PATCH_ID_OFFSET)
        return FakeMetaExtendedHeader(application_id=application_id)


class FakeContentMetaDatabase:
    """nn::ncm::IContentMetaDatabase -- what a storage holds, by title.

    Keys of any title, at any id. A launch asks it for the latest content meta
    of the game and of its update, so installing one invalidates anything
    cached against the game alone.
    """

    # nn::ncm::StorageId. Content is installed to internal user storage here.
    BUILT_IN_USER = 4

    # A Result is its module with its description above it: value = module |
    # (description << 9). ncm is module 5.
    #
    # ncm::ResultContentMetaNotFound (description 7), for a key that is not
    # installed, and ncm::ResultInvalidContentMetaKey (240), for one that cannot
    # be asked what it was asked.
    RESULT_CONTENT_META_NOT_FOUND = 5 | (7 << 9)
    RESULT_INVALID_CONTENT_META_KEY = 5 | (240 << 9)

    __slots__ = ("_entries",)

    def __init__(self):
        # A key to what that title recorded, as ncm keeps it: one store, so
        # neither half can be there without the other. Empty, as a console is
        # before anything is installed on it.
        self._entries = {}

    def __repr__(self):
        return "FakeContentMetaDatabase (%d installed)" % len(self._entries)

    def set(self, key, header):
        """Put a title in, replacing whatever was at that key.

        A key is all four of its fields, so another version of one title is
        another entry -- which is what makes GetLatest worth asking. What the
        title records arrives with it: a store works nothing out.

        Nothing in a build installs content. This is how a console's state is
        put in place before one starts.
        """
        self._entries[key] = header
        return 0

    def get_latest(self, program_id):
        """The key with the highest version at that id, or None.

        What GetLatestContentMetaKey is answered out of. It takes no storage,
        because a database is opened for one. Only whole installs count: a
        fragment is half an update, and no version of anything.
        """
        here = [k for k in self._entries
                if k.program_id == program_id
                and k.install_type == FakeContentMetaKey.INSTALL_TYPE_FULL]
        return max(here, key=lambda k: k.version) if here else None

    def list_content_meta(self, meta_type, application_id, id_min=0,
                          id_max=0xFFFFFFFFFFFFFFFF,
                          install_type=FakeContentMetaKey.INSTALL_TYPE_FULL):
        """Every key a caller's filters admit, lowest id first.

        What List is answered out of, filtered as ncm filters: by kind, by the
        id bounds, by how it was installed, and by which title it belongs to --
        each passed as unknown, or as no application at all, meaning any.

        Ordered by id, which is this module's choice and not a promise ncm
        makes: a database that answers out of several storages at once has no
        one order to give, and a caller that reads the same set as a different
        console is a caller with a bug -- one this cannot show it, since the
        order here never varies. Chosen so a run is reproducible; asked of a
        build by handing it another order.

        The bounds are its page: a title with more content than one answer
        holds is read by asking again from past the last id seen.
        """
        def admits(k, m):
            if meta_type != FakeContentMetaKey.TYPE_UNKNOWN and k.type != meta_type:
                return False
            if not id_min <= k.program_id <= id_max:
                return False
            if (install_type != FakeContentMetaKey.INSTALL_TYPE_UNKNOWN
                    and k.install_type != install_type):
                return False
            if application_id == FakeContentMetaKey.INVALID_APPLICATION_ID:
                return True
            # A title naming nobody is not excluded for it: a list excludes
            # only one naming somebody else.
            return m.application_id in (
                FakeContentMetaKey.INVALID_APPLICATION_ID, application_id)

        return sorted((k for k, m in self._entries.items() if admits(k, m)),
                      key=lambda k: k.program_id)

    def get_patch_id(self, program_id, meta_type):
        """The id a title's update is installed at, or None if it can have none.

        A title carries this rather than deriving it, so ncm reads it back and
        refuses a kind that holds no such field -- only a game and its add-on
        content do. So this reads back what installing wrote, and refuses a key
        that is neither, or a title that is not installed.

        What a DLC holds there is a data patch id, not an update of itself.
        Nothing here installs one, so it reads zero; the older header has no
        such field, and that refusal is not reproduced.
        """
        if meta_type not in (FakeContentMetaKey.APP,
                             FakeContentMetaKey.ADD_ON_CONTENT):
            return None

        for key, header in self._entries.items():
            if key.program_id == program_id and key.type == meta_type:
                return header.patch_id
        return None

    # The four above answer a command. The one below asks in terms of them.

    def launch_version(self, program_id):
        """The version that title would launch at, or None if not installed.

        The update's when one is installed, since a patch supersedes what it
        sits over, and the game's own otherwise -- two of the answers above,
        asked three times. Where the update is is asked, not worked out.
        """
        game = self.get_latest(program_id)
        if game is None:
            return None

        patch_id = self.get_patch_id(program_id, FakeContentMetaKey.APP)
        update = self.get_latest(patch_id) if patch_id is not None else None
        return update.version if update is not None else game.version


class FakeDbBuilder:
    """Holds a database and fills it, as ncm's own builder does.

    The real one builds from a storage or a package on disk; this builds from
    what a run says the console holds. Borrowed, not owned, and for no longer
    than the filling takes.

    Every id a console works out is worked out here: an update above a game,
    add-on content above that, each recording the title it belongs to. Nothing
    on the answering side works out anything.
    """

    # A game and everything shipped with it share an id but for the low bits:
    # the game at the base, its update at +0x800, its add-on content from
    # +0x1000 up and numbered from one. A game's id is a multiple of the
    # family, which is what leaves those free.
    FAMILY_SIZE = 0x2000
    ADD_ON_CONTENT_ID_OFFSET = 0x1000
    ADD_ON_CONTENT_COUNT_MAX = 0xFFF

    __slots__ = ("_db",)

    def __init__(self, db):
        self._db = db

    def __repr__(self):
        return "FakeDbBuilder(%r)" % (self._db,)

    def _game_id(self, program_id):
        """That id, if a game can have it.

        What a title ships with sits a fixed distance above the game's own id,
        which works only because that id is a multiple of the family: one that
        is not would land inside the title below.
        """
        if program_id % self.FAMILY_SIZE:
            raise ValueError(
                "%#x is no game's id: one is a multiple of %#x, and what ships"
                " with it takes the ids above"
                % (program_id, self.FAMILY_SIZE))
        return program_id

    def add_game(self, program_id, version_level=0):
        """A game, at its own id, as a console holds one."""
        key = FakeContentMetaKey(self._game_id(program_id),
                                 version_level * UPDATE_STEP,
                                 FakeContentMetaKey.APP)
        return self._db.set(key, FakeMetaExtendedHeader.from_key(key))

    def add_update(self, program_id, version_level):
        """A game's update, at the id a console installs one, at that release.

        Which release is asked for rather than assumed: an update is installed
        at some version, and there is no such thing as the usual one.
        """
        key = FakeContentMetaKey(self._game_id(program_id) + PATCH_ID_OFFSET,
                                 version_level * UPDATE_STEP,
                                 FakeContentMetaKey.PATCH)
        return self._db.set(key, FakeMetaExtendedHeader.from_key(key))

    def add_dlc(self, application_id, index, version_level):
        """One add-on content of a title, as a console holds one.

        Its id is the game's, plus where add-on content begins, plus the index
        -- numbered from one, up to where the family ends. What it records is
        whose it is, which is what a list reads and no id can be asked.
        """
        if not 1 <= index <= self.ADD_ON_CONTENT_COUNT_MAX:
            raise ValueError(
                "add-on content %d: they are numbered from 1, and a title's"
                " ids hold %d" % (index, self.ADD_ON_CONTENT_COUNT_MAX))

        key = FakeContentMetaKey(
            self._game_id(application_id)
            + self.ADD_ON_CONTENT_ID_OFFSET + index,
            version_level * UPDATE_STEP, FakeContentMetaKey.ADD_ON_CONTENT)
        return self._db.set(
            key, FakeMetaExtendedHeader.from_key(key, application_id))
