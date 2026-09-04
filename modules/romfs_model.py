#!/usr/bin/env python3
"""A romfs shaped like TOTK's, at any size: what its files are named, where
they sit, how big they are and what is in them -- each a pure function of an
index.

The goal is the shape rather than the contents. A build reads a tree, not a
game, so what has to be right is what TOTK's tree is like -- how deep its
files sit, how much the largest directory holds, what the size curve does at
both ends -- and those are measured figures the tables carry. Nothing is
stored: the same count always gives the same romfs.

Contents are invented per read by content_at: every aligned word states its own
offset, so a wrong mapping returns bytes that say so. Bytes served from a
packed mod add BIN_TAG, which is what tells a read of romfs.bin from one of
the game.
"""
import bisect
import struct
import types

# ----------------------------------------------------------------------------
# The words a game names things with
# ----------------------------------------------------------------------------


_SYN_WORDS = ("Actor", "Enemy", "Player", "Weapon", "Armor", "Field", "Dungeon",
              "Effect", "Sound", "Terrain", "Material", "Animation", "Texture",
              "Creature", "Predator", "Sentinel", "Temple", "Sky", "Cavern",
              "Junior", "Senior", "Gold", "Silver", "Blue", "Elite", "Model",
              "Config", "Layout", "Anim", "Physics", "Collision", "Preset")


_SYN_EXTS = ("bmesh", "sbmesh", "bsnd", "bcamdata", "bcollide", "bconf",
             "banims", "bmodel", "pack", "bcfg")


# The shape below is measured: TOTK's romfs -- 304k+ files in 2.6k+
# directories, 15.9 GiB -- walked once.


# ----------------------------------------------------------------------------
# Similar directories TOTK has, and the tail that fills out the rest
# ----------------------------------------------------------------------------


# The three hundred directories holding most of the files, with the shape of
# their real paths. That is what makes the depth histogram come out right
# without modelling depth at all: a path that sits one level down and holds a
# tenth of the game puts a tenth of the files one level down. Shares are per
# million of *all* files, so they divide a count of any size -- and so the
# largest directory is a quarter of the game rather than a quarter of this
# table.
#
# Three hundred rather than a couple of dozen because a directory exists only
# once a file lands in it: the head then grows with the base instead of
# standing there whole from the first few thousand files.
#
# Names that identify the game are stand-ins of exactly the same length, since
# length is what a path costs to store and to walk. The vocabulary any romfs
# would use -- Sound, Resource, Model, Actor -- is left alone: a tree built
# from invented words would not sort or walk like one built from real ones.
_PLAIN_DIRS = (
    ("Zone/MainField/Merged", 249603),
    ("Zone/UnderField/Merged", 196570),
    ("TexPool", 96489),
    ("Sound/Resource/Stream", 71150),
    ("Pack/Actor", 49523),
    ("Model", 45608),
    ("Bake/Scene", 34361),
    ("TerrainArc/MainField", 30183),
    ("Solid/Shape/Aux", 23442),
    ("UI/Tex/Icon", 22650),
    ("UI/Map/MainField", 21200),
    ("Logic", 10524),
    ("Event/EventFlow", 6935),
    ("Sound/Resource", 5354),
    ("Effect", 5236),
    ("GrassStats/MainField", 4247),
    ("Solid/StaticCompoundBody/MainField", 4210),
    ("GrassStats/UnderField", 4207),
    ("Solid/StaticCompoundBody/UnderField", 4207),
    ("Solid/StitchedNavMesh/MainField", 4207),
    ("Solid/StitchedNavMesh/UnderField", 4207),
    ("Cave/cave017/UnderField/Full/C.crbin.517a15eb", 3951),
    ("TerrainArc/UnderField", 3658),
    ("AS", 3399),
    ("UI/Tex/PictureBook", 3366),
    ("Component/AIScheduleParam", 3116),
    ("AnimationEvent/Animation", 1670),
    ("Bake/Model", 1604),
    ("AnimationEvent/AsNode", 1357),
    ("Zone/MainField/Cave", 1328),
    ("Zone/SmallDungeon/Merged", 1272),
    ("Effect/Blur", 1256),
    ("UI/Map/HeightMap", 1183),
    ("Cull/MainField", 1052),
    ("Cull/UnderField", 1052),
    ("VolumeStats/MainField", 1052),
    ("Zone/SmallDungeon", 999),
    ("Game/DestructiblePiece", 976),
    ("CaveBounding/MainField", 720),
    ("Cull/Cave", 697),
    ("Event/EventSetting/EventSettingComponentList", 664),
    ("Solid/StaticCompoundBody/MainField/Cave", 664),
    ("Sequence", 648),
    ("CameraAnimation", 634),
    ("Preload", 575),
    ("Zone/GameZoneParam", 536),
    ("Sound/Shape", 536),
    ("Zone/MainField", 532),
    ("Zone/UnderField", 532),
    ("Solid/StaticCompoundBody/SmallDungeon", 500),
    ("Scene/Component/SmallDungeonParam", 500),
    ("TerrainArc/SpiritualField", 447),
    ("System/CombinationDataTableData", 424),
    ("Zone/MainField/Sky", 421),
    ("TerrainArc/StartIsland", 388),
    ("Sound/GroundControlActorSoundPlaySetting", 381),
    ("Event/EventSetting/EventSettingComponent/GameEventBaseSetting", 299),
    ("Event/EventSetting/EventSettingComponent/ExceptionalActorSetting", 273),
    ("Zone/MainField/DeepHole", 270),
    ("GrassStats/StartIsland", 214),
    ("Solid/StaticCompoundBody/MainField/Sky", 210),
    ("UI/Tex/CharaDirectory", 210),
    ("AI", 204),
    ("UI/Map/LargeDungeon", 197),
    ("UI/Tex/StaffRoll", 184),
    ("TerrainArc/SmallRelicRuinsIsland", 171),
    ("Game/BluePrint/CombinedActorInfo", 151),
    ("Game/BluePrint/Texture", 151),
    ("Solid/NavMesh", 145),
    ("Solid/StaticCompoundBody/MainField/DeepHole", 135),
    ("TerrainArc/MeadowCastlePlatform", 131),
    ("UI/Map/MainFieldOpenMask", 128),
    ("UI/Tex/HeroHouse", 118),
    ("UI/Tex/Horse", 105),
    ("Solid/Ragdoll/Reaction", 99),
    ("Event/CutInfo", 92),
    ("Event/Movie", 92),
    ("UI/Tex/SpecialParts", 89),
    ("Component/Blackboard/BlackboardParamTable", 82),
    ("Zone/BossVehicle", 79),
)


_LANGUAGES = ('JPja', 'EUde', 'EUes', 'EUfr', 'EUit', 'EUru', 'USen', 'USes')


_LANG_DIRS = (
    ("Voice/Resource/<lang>/EventFlowMsg", _LANGUAGES,
     (381, 375, 375, 375, 375, 375, 375, 375)),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QL_0007_Stream", _LANGUAGES,
     (260, 247, 247, 247, 247, 243, 247, 247)),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_ZN_0033_Stream", _LANGUAGES, 181),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0007_Stream", _LANGUAGES, 177),
    ("Voice/Resource/<lang>/ShoutMsg/ShoutVoice_Amber_Stream", _LANGUAGES, 171),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0005_Stream", _LANGUAGES, 148),
    ("Voice/Resource/<lang>/ShoutMsg/ShoutVoice_Boron_Stream", _LANGUAGES, 141),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0006_Stream", _LANGUAGES, 138),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QA_Birth_Stream", _LANGUAGES, 131),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QK_0020_Stream", _LANGUAGES, 131),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0004_Stream", _LANGUAGES, 122),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QK_BeastWakeUp_Stream", ('JPja',), 122),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_ZN_0039_Stream", _LANGUAGES, 115),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0008_Stream", _LANGUAGES, 102),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QA_LieServant_Stream", _LANGUAGES, 99),
    ("Voice/Resource/<lang>/ShoutMsg/ShoutVoice_Nakor_Stream", _LANGUAGES, 99),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QA_Meet_Stream", _LANGUAGES, 95),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QA_QueenDead_Stream", _LANGUAGES, 95),
    ("Voice/Resource/<lang>/ShoutMsg/ShoutVoice_Vane_Stream", _LANGUAGES, 95),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QB_0016_Stream", _LANGUAGES, 89),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QC_0025_Stream", _LANGUAGES, 85),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QG_GoBackBC_Stream", _LANGUAGES,
     (82, 76, 76, 76, 76, 76, 76, 76)),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QL_0004_Stream", _LANGUAGES, 79),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QD_0021_Stream", _LANGUAGES, 79),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QE_0022_Stream", _LANGUAGES, 79),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QF_0032_Stream", _LANGUAGES, 79),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QA_0010_Stream", _LANGUAGES, 76),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QA_Sandmaw_Stream", ('JPja', 'EUde', 'EUes'),
     (76, 72, 72)),
    ("Voice/Resource/<lang>/EventFlowMsg/Dm_QB_0017_Stream", ('JPja', 'EUde', 'EUes'),
     (76, 72, 72)),
    ("Voice/Resource/<lang>/EventFlowMsg/DmT_QG_BossIn_Stream", ('EUde', 'EUes', 'EUfr'), 72),
    ("Voice/Resource/<lang>/ShoutMsg/ShoutVoice_Sarel_Stream", ('EUde', 'EUes'), 72),
)

# A voice line exists once per language, so a voice directory exists once per
# language too: most of the entries here were the same few trees written out
# once each. They are patterns now, expanded on the way in.
#
# The tree that comes out is the same tree, share for share, including the
# five patterns where Japanese runs a little longer than the rest. It also
# makes the language set a knob -- though editing it gives a different romfs,
# since those directories are leaves and the leaf share is measured.
_REAL_DIRS = _PLAIN_DIRS + tuple(
    (pattern.replace("<lang>", lang), share)
    for pattern, langs, shares in _LANG_DIRS
    for lang, share in zip(langs, shares if isinstance(shares, tuple)
                           else (shares,) * len(langs)))


# The rest, spread over directories that are nearly all tiny -- most hold a
# single file. They are almost every directory in the game and a small share
# of its files, which is the ratio a per-directory cost gets paid at, and the
# one an evenly-spread tree gets most wrong.
_TAIL_BANDS = (   # (files in such a directory, how many such directories)
    (  1,  983), (  2,   81), (  3,  224), (  4,  181), (  5,  315),
    (  6,   69), (  7,   45), (  8,   68), (  9,   35), ( 10,   44),
    ( 11,   40), ( 12,   46), ( 13,   25), ( 14,   50), ( 15,   39),
    ( 16,   11), ( 17,    6), ( 18,    5), ( 19,    8), ( 21,    9),
    ( 22,   21),
)

# How many tail directories share a chain. Directories are not free -- each is a
# context the build allocates and an entry in the merged tables -- so how many
# hold nothing but other directories has to be right too, and in a real romfs
# that number is tiny: sixty-one against two and a half thousand that hold
# files. Chains are shared widely to keep it so, the lower level gathering a
# few hundred leaves and the upper ten of those.
_CHAIN_LOWER = 300
_CHAIN_UPPER = 3000


# How many tail directories in a thousand hang under the one before them rather
# than under their own chain. Such a directory leaves the one above it holding
# files and subdirectories at once, which is the shape a build gets wrong -- and
# it costs no new directory, since the one that moved already existed.
#
# The stride is coprime with a thousand and large enough that no two adjacent
# directories are ever both chosen -- one hanging under another that has itself
# moved would leave the path between them holding nothing.
#
# Per thousand rather than one in every so many: a divisor steps from 15.0% of
# the tree holding both to 17.9%, and the target sits between.
_REPARENT_PER_1000 = 180

# A golden-ratio stride, coprime to a million: consecutive indices land far
# apart in the share table, so any prefix of the tree is proportioned like the
# whole of it rather than like its first entries. Coprime alone is not enough
# -- 999983 is prime but steps by seventeen, which keeps a small run in one
# band at one depth.
_SPREAD = 618033


# ----------------------------------------------------------------------------
# What those tables come to, worked out once at import
# ----------------------------------------------------------------------------


# The directories the listed paths pass through, which hold nothing of their own
# unless they are given something. They are present from the smallest base --
# they arrive with the entries that imply them -- so what they hold is what a
# small tree is mostly made of.
#
# Most are given a share, which makes them directories holding files and
# subdirectories at once -- the shape a build gets wrong, and one the tail
# alone does not supply enough of until 140k files. A tenth keep only their
# subdirectories, fewer than the 2.3% a real romfs has: this is the constant
# that decides the spine share at every size, and a tenth is what holds it
# under three from eight thousand files up.
#
# The share is small enough not to disturb what the named directories hold.
_PARENT_SHARE = 200
_PARENT_SUBDIRS_ONLY = 10


def _implied_parents(listed):
    """Every directory a listed path passes through, that is not listed itself."""
    out = set()
    for d in listed:
        parts = d.split("/")
        for k in range(1, len(parts)):
            out.add("/".join(parts[:k]))
    return sorted(out - set(listed))


_LISTED = tuple(d for d, _s in _REAL_DIRS)
_REAL_DIRS = _REAL_DIRS + tuple(
    (p, _PARENT_SHARE) for k, p in enumerate(_implied_parents(_LISTED))
    if (k * 37) % 100 >= _PARENT_SUBDIRS_ONLY)


_BIG_SHARE = sum(share for _d, share in _REAL_DIRS)


# How many files per million reach the tail, which is what sets the rate
# directories appear at -- one per 114 files, the rate the real romfs has. The
# bands say how files are shared among tail directories; this says how many
# there are to share among.
#
# Not derived from _BIG_SHARE. The rest of the files go to the named
# directories in the proportions the share table gives them, rescaled, so this
# can be set to what the directory count needs without disturbing them.
_TAIL_SHARE = 32596


# Cumulative share, so a slot resolves with one bisect rather than a scan of
# three hundred.
_DIR_CUM = []
_DIR_NAME = []


_acc = 0
for _d, _share in _REAL_DIRS:
    _acc += _share
    _DIR_CUM.append(_acc)
    _DIR_NAME.append(_d)

# Frozen once built, here and below: these are the measurement, and a caller
# holding a different one would be describing a different game.
_DIR_CUM, _DIR_NAME = tuple(_DIR_CUM), tuple(_DIR_NAME)

# How many directories one cycle of the tail holds.
_TAIL_DIRS = sum(_n for _f, _n in _TAIL_BANDS)

# Every tail directory's size, in the order they are laid down. The table
# groups them smallest first, which would make the opening of a cycle one
# directory per file; stepping through it by a stride coprime with the count
# spreads the sizes so any prefix holds the mix the whole cycle does.
_TAIL_SIZES = []
for _f, _n in _TAIL_BANDS:
    _TAIL_SIZES.extend([_f] * _n)
_TAIL_SIZES = tuple(_TAIL_SIZES[(_k * 1597) % _TAIL_DIRS]
                    for _k in range(_TAIL_DIRS))


# Files held by the first k directories, so a file finds its directory by a
# bisect rather than a scan.
_TAIL_CUM = []
_c = 0
for _sz in _TAIL_SIZES:
    _c += _sz
    _TAIL_CUM.append(_c)
_TAIL_CUM = tuple(_TAIL_CUM)

# How long a cycle is, taken from the sizes that were actually laid out. Summing
# the band table instead would be the same number until something adjusts the
# sizes, and then a cycle would be longer than the table a file is found in.
_TAIL_FILES = _TAIL_CUM[-1]


def _tail_dir(nth):
    """Where the nth tail directory sits.

    Some hang under the directory before them rather than under their own
    chain, which leaves that one holding files and subdirectories at once.
    Nothing is created to do it -- the directory that moved already existed --
    so neither the count nor anyone else's depth changes.

    The stride can never choose two adjacent directories, and must not: one
    hanging under another that has itself moved leaves the path between them
    holding nothing.
    """
    if nth and (nth * 337) % 1000 < _REPARENT_PER_1000:
        return "%s/%s" % (_tail_chain(nth - 1), _leaf(nth))
    return _tail_chain(nth)


def _leaf(nth):
    return "%s%05d" % (_SYN_WORDS[nth % len(_SYN_WORDS)], nth)


def _tail_chain(nth):
    """The chain the nth tail directory hangs under.

    Shared, and widely: a directory holding nothing but directories is 2.3% of
    the real romfs, so each chain has to gather hundreds of leaves rather than
    a handful. The lower level gathers a few hundred; the upper gathers every
    lower one until the tail passes _CHAIN_UPPER directories, which takes
    about four hundred thousand files, so below that it is a single directory.

    Kept for the level rather than the fan-out: it is what leaves a file four
    deep, where the real romfs puts most of them, and it costs one directory.
    Dropping it holds every rule too, but spends the line's margin at its
    floor -- 2.96% of 3% at seven and a half thousand files -- to save that
    one.
    """
    w = _SYN_WORDS
    upper = nth // _CHAIN_UPPER
    lower = nth // _CHAIN_LOWER
    return "%s%02d/%s%03d/%s" % (w[upper % len(w)], upper % 100,
                                 w[lower % len(w)], lower % 1000, _leaf(nth))


# ----------------------------------------------------------------------------
# What a file is called
# ----------------------------------------------------------------------------


# One name in this many is built from many more tokens, so the tree reaches
# the lengths a real romfs has without its average moving.
_LONG_NAME_EVERY = 30
_LONG_TOKENS = 15


def _name(i):
    """The basename for entry i: compound tokens, as real assets have."""
    # A real romfs reaches 114 characters in a name while averaging 40, so a
    # flat two-to-six tokens gives the right mean only by having no long names
    # at all. A small share run much longer and the rest run a little shorter,
    # which is the same mean over a spread nearer the real one.
    count = _LONG_TOKENS if i % _LONG_NAME_EVERY == 0 else 2 + (i % 4)
    tokens = "_".join(_SYN_WORDS[(i * (k + 3)) % len(_SYN_WORDS)]
                      for k in range(count))

    # Tokens first, as a real asset name has them, and not the index: digits
    # sort below letters, so an index-first name puts every file below every
    # directory and no directory is ever split by its subdirectories. The tree
    # then cannot take the shape a real romfs has -- the shape that aborted a
    # build on hardware while passing everything here.
    return "%s_%07d.%s" % (tokens, i, _SYN_EXTS[i % len(_SYN_EXTS)])


# ----------------------------------------------------------------------------
# Where a file sits
# ----------------------------------------------------------------------------


# Two orders, and they differ. Table entries go in walk order, as a console
# has them, so a directory's own files are split by its subdirectories
# wherever the names interleave and no directory has one run to measure -- the
# property a build assumed it had, and aborted on hardware for. The file
# partition is in full-path order, which stock asserts on: same-source files
# must be at non-decreasing offsets to compact into one source entry.
def path(i):
    """Where entry i lives -- deterministic, and shaped like a real romfs.

    The share table decides which directory, so the fan-out, the depth spread
    and the one directory holding a quarter of the game all come out of the
    measurement rather than being modelled separately. The basename carries the
    index, so it is unique wherever it lands.
    """
    # Entry 0 lives at the root, which makes x=1 one file and no directory at
    # all -- the smallest romfs the format can express, and worth being able to
    # ask for. It also means the root's own file chain, where a walk starts,
    # is built and read at every size rather than only when a mod adds a file
    # there.
    if i == 0:
        return _name(0)

    # A file belongs to the tail exactly when it advances the tail counter.
    # Deciding that separately let the counter run past directories no file ever
    # claimed: a directory of one file owns one value of it, and every file that
    # could have taken that value was free to go to a big directory instead.
    if i * _TAIL_SHARE // 1000000 != (i - 1) * _TAIL_SHARE // 1000000:
        t = i * _TAIL_SHARE // 1000000 - 1
        cycle, off = divmod(t, _TAIL_FILES)
        nth = cycle * _TAIL_DIRS + bisect.bisect_right(_TAIL_CUM, off)
        return "%s/%s" % (_tail_dir(nth), _name(i))

    # The rest go to the named directories, in their measured proportions --
    # rescaled, the tail having already taken its share above.
    slot = (i * _SPREAD) % 1000000 * _BIG_SHARE // 1000000
    return "%s/%s" % (_DIR_NAME[bisect.bisect_right(_DIR_CUM, slot)], _name(i))


# ----------------------------------------------------------------------------
# What a file weighs
# ----------------------------------------------------------------------------


# What the real files weigh, at every twentieth percentile. A romfs is not
# spread evenly across three orders of magnitude: most of the files are tiny
# and the top twentieth carries most of the bytes. Sizes repeat freely here,
# which is how a real tree is too, so anything looking for files of equal
# length finds them without a rule inventing some.
_SIZE_CURVE = (7, 77, 133, 189, 250, 318, 401, 502, 650, 911,
               1315, 2036, 3633, 7113, 11768, 17946, 25915, 41451, 86432, 202311)

# The curve above covers the first ninety-five percent. Slots 95 to 98 are the
# mean of each measured percentile rather than a round number near it: three
# quarters of the game lives past this point, so a percent rounded up here
# costs more total than the whole bottom half weighs.
_SIZE_TOP = (233682, 308671, 472658, 717908)

# The top percent is not one size: it spans three orders of magnitude on its
# own and carries a large part of the game, so flattening it to a single value
# leaves the total short however exactly the rest fit -- and hides the reads a
# very large file provokes, which is the case a windowed reader exists
# for. Per thousand of that percent, and the mean of each measured band.
_SIZE_HUGE = ((500,   980000), (300,  1700000), (130,  3300000),
              ( 50,  6400000), ( 17, 20400000), (  2,  75000000),
              (  1, 196000000))


# Only this value modulo a thousand matters, and what it decides is how early
# the heavy bands turn up. That is the whole of how far the romfs's total can
# stray from a straight line: the rest of the curve is stratified and exact from
# two hundred files, while a band occurring twice per thousand either has
# arrived or has not. Of every coprime stride, measured against a line fitted
# from eight thousand files to five hundred thousand, this is the flattest:
# worst case 2.85%, where 33 gives 7.91%.
_HUGE_SPREAD = 241


_SIZE_SPREAD = 97


# What an update does to a romfs: rewrites a scattering of files and leaves the
# rest alone. Sparse enough to stay a patch rather than a different game, and
# anchored near the head of every stretch so even a hundred-file tree has one --
# otherwise a small x would build an updated romfs identical to the shipped
# one, and the two would agree on a digest where they should not.
_PATCH_EVERY = 512


# How many at that head. Two rather than one because a mod overrides from the
# first file up: at one, the only rewrite a tree smaller than the stride held
# was the file the mod replaced, and an updated romfs read as the shipped one
# again. A mod overriding two or more hides both, and wants an x past them.
_PATCH_RUN = 2


_PATCH_GROWTH = 64


# The version's low sixteen bits are a release step nothing sets, so they are
# dropped before the version reaches the sizes -- left in, every version a
# counter can hold would be a multiple of the stride above and the rewrite
# would land on the same files regardless.
_VERSION_STEP_BITS = 16


def size(i, version=0):
    """The weight of entry i, following the curve a real romfs has.

    version is which release of the title this is, packed as ncm packs one,
    and zero for the game as it shipped. It seeds the rewrite: the files an
    update touched are a little larger, by an amount the version decides, so
    two updates are two romfs rather than one wearing two numbers. A patch
    that changed nothing on disk would be a strange thing to have installed.
    """
    slot = (i * _SIZE_SPREAD) % 100
    if slot < 95:
        lo = _SIZE_CURVE[slot // 5]
        hi = _SIZE_CURVE[slot // 5 + 1]
        n = lo + (hi - lo) * (slot % 5) // 5
    elif slot < 99:
        n = _SIZE_TOP[slot - 95]
    else:
        # i // 100, not i: indices in this slot are a hundred apart, so
        # multiplying i visits only ten ranks of a thousand and the rarest band
        # never fires. Dividing the stride out first restores the full cycle.
        rank, n = ((i // 100) * _HUGE_SPREAD) % 1000, _SIZE_HUGE[-1][1]

        for share, size_ in _SIZE_HUGE:
            if rank < share:
                n = size_
                break
            rank -= share
    if not version or i % _PATCH_EVERY >= _PATCH_RUN:
        return n
    return n + _PATCH_GROWTH + (version >> _VERSION_STEP_BITS) % _PATCH_GROWTH


def mod_size(i):
    """The size of the mod's version of file i.

    Deliberately unlike size(i). An override that matched the length of what it
    replaces leaves the tables identical whether or not it was applied, and the
    digest cannot then tell a working override from one silently dropped.
    """
    return 256 + (i * 5651) % 65536


def added_size(j):
    """A mod's own files run larger than the average base entry -- replaced
    textures and models are what a mod ships."""
    return 4096 + (j * 7919) % 262144


# ----------------------------------------------------------------------------
# What a mod delivers
# ----------------------------------------------------------------------------


# Where the mod's own files sit in the index space: past any base a run would
# ask for, so an added path can never be one the game already holds. The bound
# is generous because colliding would turn an addition into an override
# silently, and a run that reached it would be measuring the wrong thing.
_ADDED_FROM = 100000000


def added_path(root, j):
    """Path for a mod file the base romfs does not contain.

    From path() like everything else, at an index past any base. A mod's tree is
    a romfs tree, so its additions have the game's directory distribution and
    its name lengths rather than a shape of their own -- and they land in the
    game's own directories, which is where a mod puts them.

    What decides the SD walk's cost is how many directories the mod spreads
    over. That now follows the same measurement the base does instead of a
    bucket size chosen by hand, so a mod cannot quietly be the wrong shape while
    the game it sits on is the right one.

    An override is already the base's shape, since it *is* a base path; this is
    what the additions were missing.
    """
    return "%s/%s" % (root, path(_ADDED_FROM + j))


# ----------------------------------------------------------------------------
# What a file contains
# ----------------------------------------------------------------------------


def _content(tag, offset, size):
    """Bytes that say where they came from, from `tag` upwards.

    Every aligned 8-byte word contains its own offset, so a run of bytes states
    where it came from. That turns "does a file read back correctly" into a
    question answerable without storing 17 GiB, or indeed anything: a wrong
    mapping returns bytes that say so.

    One body for both ranges: what a word says about itself is the invariant
    this module exists for, and two copies of it are two chances to change one.
    """
    first = offset & ~7
    words = ((offset + size) - first + 7) // 8
    raw = b"".join(struct.pack("<Q", tag + first + 8 * i) for i in range(words))
    return raw[offset - first: offset - first + size]


def content_at(offset, size):
    """The base game's file data, invented on demand."""
    return _content(0, offset, size)


# A romfs.bin mod is a whole packed image rather than a tree of loose files,
# and it reaches the build as a second source to merge rather than a set of
# overrides to apply. Its data is shaped like the game's -- every aligned word
# states its own offset -- but lifted out of the range the game can occupy, so a
# file served from the .bin is provably not that path served from the game.
BIN_TAG = 1 << 60


def from_packed(word):
    """Was that word served from the romfs.bin mod rather than from the game?

    Asked rather than tested, so the tag is one fact in one place: what a word
    means is this module's to say, since it is this module that invented it.
    """
    return bool(word & BIN_TAG)


def bin_content_at(offset, size):
    """The romfs.bin's file data, invented on demand. See content_at."""
    return _content(BIN_TAG, offset, size)


# ----------------------------------------------------------------------------
# What the whole tree comes to
# ----------------------------------------------------------------------------


def shape(count, version=0):
    """What the tree of `count` files looks like, measured rather than declared.

    Everything a rule asks about, in one walk, so a constant can be changed
    and its effect seen without writing a script to see it -- and so that no
    rule describes the tree a second time in its own terms. Nothing is kept
    per file.
    """
    holds_files, all_dirs, holds_dirs = {}, set(), set()
    name_length = 0
    for i in range(count):
        p = path(i)
        base = p.rsplit("/", 1)[-1]

        # The name, and only the name. The entry's own fields are the same
        # 32 bytes for every file, so a tree cannot be wrong about them, and
        # the padding is the format's business rather than this file's. What
        # a file weighs costs nothing at all -- the build reads the tables and
        # writes entries, and never touches the content they describe.
        name_length += len(base)

        # Which directories it puts on the map. How deep it sits is not
        # counted: a level costs a directory entry and the name on it, and
        # both of those are held -- by the line the count follows, and by the
        # share of directories holding files and subdirectories at once.
        cur = ""
        for part in p.split("/")[:-1]:
            holds_dirs.add(cur)
            cur = cur + "/" + part if cur else part
            all_dirs.add(cur)
        holds_files[cur] = holds_files.get(cur, 0) + 1

    # The root is not one of the directories being counted: a romfs holding one
    # file at its root is flat.
    with_files = set(holds_files) - {""}
    with_dirs = holds_dirs - {""}
    total = len(all_dirs) or 1
    biggest = max(holds_files, key=lambda d: holds_files[d]) if holds_files else ""

    # Handed out read-only: the dict goes out of scope here, so a caller has
    # no second way to reach a measurement it has nothing to say about.
    described = {
        "files": count,
        "dirs": total,
        "files only": 100.0 * len(with_files - with_dirs) / total,
        "subdirs only": 100.0 * len(with_dirs - with_files) / total,
        "both": 100.0 * len(with_files & with_dirs) / total,
        "biggest share": 100.0 * holds_files.get(biggest, 0) / count,
        "name length": name_length / float(count),
    }

    # A proxy is only as deep as what is under it, and the walk holds a list, a
    # histogram and a set that would leave this read-only in name only.
    assert all(isinstance(v, (int, float, str)) for v in described.values())
    return types.MappingProxyType(described)


# ----------------------------------------------------------------------------
# Whether it came out shaped like the game
#
# Every rule holds a figure a build is charged for -- what it reads, what it
# writes, what it takes -- so a tree that passes costs what the game costs.
#
# The bounds are stated rather than read out of the tables above: read from
# there, a rule follows wherever a table is edited to and catches only
# self-disagreement.
# ----------------------------------------------------------------------------

# The base every rule is read at, unless it names another.
FULL_BASE = 305000

# The directory count must fit y = ax + b, x files and y directories, within
# the margin. b is the head -- the listed directories and the parents their
# paths imply -- which does not vanish as the base shrinks; a is the tail's
# rate. The floor is where lines run out rather than where this one does:
# reaching down to six thousand files is 6% off at best, whatever a and b.
DIR_LINE_PER_MILLION = 7695
DIR_LINE_INTERCEPT = 315
DIR_LINE_FROM = 7500
DIR_LINE_BASES = (7500, 8000, 10000, 20000, 50000, 100000, 200000, FULL_BASE,
                  500000)
DIR_COUNT_MARGIN = 3

NAME_LENGTH = 40
NAME_LENGTH_MARGIN = 1

# What the tree is made of: a directory holds files, holds directories, or
# holds both.
#
# A directory holding both has its run of file entries split by its
# subdirectories -- the case a build gets wrong -- so that share has a floor;
# one holding only directories is one the tree invented, so that has a
# ceiling. Leaves need no bound of their own: the three come to a hundred.
#
# The ceiling is low because spine falls as the base grows, and it has to
# hold at the floor as well as at a full base.
DIR_SPINE_MAX = 3
DIR_BOTH_MIN = 16

# The share the largest directory holds: a quarter, give or take a point.
# Not a figure a tree can be tuned to a decimal of -- what the head's own
# parents hold comes out of the head, so the biggest entry no longer takes
# quite the whole of its share.
BIGGEST_SHARE = 25
BIGGEST_MARGIN = 1


def _thousands(n):
    """A file count in thousands, for a line meant to be read."""
    return ("%.1fk" % (n / 1000.0)).replace(".0k", "k") if n >= 1000 else str(n)


def test_shape():
    """[(rule, holds, what was measured)] -- is this tree still that one.

    Answered from the walk above rather than one of its own: a rule counting
    the tree itself would be a second description of it, free to disagree.

    At a full base, because that is the size the shares are shares of -- and
    the size a collision would appear at, the stride spacing these paths being
    coprime with a million.
    """
    out = []
    n = FULL_BASE
    got = shape(n)
    total = got["dirs"]

    length = got["name length"]
    out.append(("average characters by name",
                abs(length - NAME_LENGTH) <= NAME_LENGTH_MARGIN,
                "%.2f (wanted %d +/-%d)"
                % (length, NAME_LENGTH, NAME_LENGTH_MARGIN)))

    # A tree spreading its files evenly gets every per-directory cost wrong in
    # the same direction, so the concentration is the point. Held to the share
    # rather than to a floor, which would pass a directory that had quietly
    # lost a third of its files to a subdirectory.
    out.append(("files in the largest directory",
                abs(got["biggest share"] - BIGGEST_SHARE) <= BIGGEST_MARGIN,
                "%.2f%% (wanted %d%% +/-%d)"
                % (got["biggest share"], BIGGEST_SHARE, BIGGEST_MARGIN)))

    # Directories per file is what a walk pays, so it has to hold at any size
    # a run is asked for -- a count fixed to one base says nothing about
    # another. One walk per base, and n is not walked twice.
    at = {n: got}
    for x in DIR_LINE_BASES:
        at.setdefault(x, shape(x))

    worst_off = 0.0
    for x in DIR_LINE_BASES:
        assert x >= DIR_LINE_FROM
        wanted = x * DIR_LINE_PER_MILLION // 1000000 + DIR_LINE_INTERCEPT
        worst_off = max(worst_off,
                        100.0 * abs(at[x]["dirs"] - wanted) / wanted)
    full_want = n * DIR_LINE_PER_MILLION // 1000000 + DIR_LINE_INTERCEPT
    out.append(("directories against the line y = %dx/10^6 + %d"
                % (DIR_LINE_PER_MILLION, DIR_LINE_INTERCEPT),
                worst_off <= DIR_COUNT_MARGIN,
                "%d against %d; off by %.1f%% at worst (margin %d%%)"
                % (got["dirs"], full_want, worst_off, DIR_COUNT_MARGIN)))

    spine, both = got["subdirs only"], got["both"]
    out.append(("directories by what they hold (files/subdirs/both)",
                spine <= DIR_SPINE_MAX and both >= DIR_BOTH_MIN,
                "%.1f/%.1f/%.1f%% of %d directories; "
                "wanted subdirs<=%d%% both>=%d%%"
                % (got["files only"], spine, both, total, DIR_SPINE_MAX,
                   DIR_BOTH_MIN)))

    return out


# Run it to be told: every rule, and what it measured. The measurement is
# printed whether or not the rule holds -- these are figures nothing else
# shows, and a rule that only speaks when it fails is a rule nobody reads.
#
# It wraps short of the column the verdicts sit in, so a verdict has nothing
# but air beneath it and the eye can run down them. The verdict is right
# aligned in the width of the longer word, so a failing rule ends where a
# passing one does and no line grows.
if __name__ == "__main__":
    import sys
    import textwrap

    # What the rules amount to, said once: a reader who runs this is asking
    # whether the tree still resembles the game, and the answer is the whole
    # list rather than any line of it.
    print()
    for line in textwrap.wrap(
            "The figures a build pays for, taken off TOTK's romfs and off "
            "the tree this makes at %s files. Every rule should hold from %s "
            "up." % (_thousands(FULL_BASE), _thousands(DIR_LINE_FROM)), 68):
        print("  %s" % line)
    print("  " + "-" * 24)

    broken = 0
    for rule, holds, measured in test_shape():
        print()
        print("  %-62s %6s" % (rule, "ok" if holds else "FAILED"))

        # A clause at a time, so a break never lands inside a figure: the
        # semicolons are where a measurement already divides. Each is wide as
        # the verdicts allow -- they keep two clear columns to their left --
        # then as narrow as it goes without costing a line, since a block
        # with a two-word tail reads as though something were missing.
        clauses = measured.split("; ")
        for n, clause in enumerate(clauses):
            if n + 1 < len(clauses):
                clause += ";"
            width = 59
            lines = textwrap.wrap(clause, width)
            while len(textwrap.wrap(clause, width - 1)) <= len(lines):
                width -= 1
                lines = textwrap.wrap(clause, width)
            for line in lines:
                print("    %s" % line)
        broken += not holds
    sys.exit(1 if broken else 0)
